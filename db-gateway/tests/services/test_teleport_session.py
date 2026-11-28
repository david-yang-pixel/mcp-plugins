"""teleport_session: db-gateway's own Teleport session (gen-<N> directories + `current` symlink)."""
import asyncio
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from db_gateway.postgres import TeleportAuthExpired
from db_gateway.teleport_login import classify_login_output
from db_gateway.teleport_session import (
    SESSION,
    LoginFailed,
    LoginRejected,
    live_home,
    live_kubeconfig,
)
from tsh_helpers import install_session, push_login_ok, status_text

REJECTED = "ERROR: invalid credentials\n"


def logins(fake_tsh):
    return [c for c in fake_tsh.calls if c["argv"][:1] == ["login"]]


@pytest.fixture
def no_wait(monkeypatch):
    """Skip the wait for the next TOTP window; record how long it would have been."""
    waits: list[float] = []

    async def fake_sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(SESSION, "_sleep", fake_sleep)
    return waits


@pytest.fixture
def otps(monkeypatch):
    codes = iter(["111111", "222222", "333333", "444444"])
    monkeypatch.setattr(SESSION, "_totp", lambda secret: next(codes))


# --- renewal into a fresh generation --------------------------------------------------


async def test_renew_logs_into_a_fresh_generation_and_switches_current(cfg, fake_tsh):
    push_login_ok(fake_tsh)
    snap = await SESSION.renew(cfg, reason="test")

    sessions = Path(cfg.teleport.sessions_dir)
    assert snap.generation == 1 and snap.root == sessions / "gen-1"
    assert (sessions / "current").resolve() == snap.root
    assert live_home(cfg) == sessions / "current" / "home"
    assert live_kubeconfig(cfg) == sessions / "current" / "kubeconfig"
    assert (snap.home_dir / "current-profile").is_file()
    assert snap.valid_until == datetime(2030, 1, 1, tzinfo=timezone.utc)
    assert snap.roles == ("production-read-all", "support-lambda-logs")
    assert SESSION.current() is snap

    login, kube, apps, status = fake_tsh.calls
    assert login["argv"] == ["login", "--proxy=teleport.dify.dev:443", "--user=u"]
    assert login["entered"]["password"] == "test-password"
    assert login["entered"]["otp"].isdigit() and len(login["entered"]["otp"]) == 6
    assert kube["argv"] == ["kube", "login", "--all"]
    assert apps["argv"] == ["apps", "login", "--aws-role", "SupportLambdaLogsReadOnly", "aws-support-logs"]
    assert status["argv"] == ["status"]
    for call in fake_tsh.calls:   # everything happens inside gen-1, never in a shared home
        assert call["env"]["TELEPORT_HOME"] == str(snap.home_dir)
        assert call["env"]["TELEPORT_TOOLS_VERSION"] == "off"
        assert not any(k.lower() in ("http_proxy", "https_proxy", "all_proxy") for k in call["env"])
    assert kube["env"]["KUBECONFIG"] == str(snap.kubeconfig_path)


async def test_disabled_tools_skip_their_logins(cfg, fake_tsh):
    cfg.teleport_tools.kube.enabled = False
    cfg.teleport_tools.lambda_logs.enabled = False
    push_login_ok(fake_tsh, kube=False, apps=False)
    await SESSION.renew(cfg, reason="test")
    assert [c["argv"][0] for c in fake_tsh.calls] == ["login", "status"]


async def test_next_renewal_switches_atomically_and_removes_the_old_generation_later(cfg, fake_tsh, no_wait):
    cfg.teleport.session_retain_seconds = 0.2
    push_login_ok(fake_tsh)
    push_login_ok(fake_tsh)
    first = await SESSION.renew(cfg, reason="test")
    second = await SESSION.renew(cfg, reason="test")

    sessions = Path(cfg.teleport.sessions_dir)
    assert second.generation == 2
    assert os.readlink(sessions / "current") == "gen-2"
    assert first.root.exists()               # in-flight calls may still use it
    await asyncio.sleep(0.5)
    assert not first.root.exists()
    assert second.root.exists()


async def test_leftovers_from_before_a_restart_are_removed(cfg, fake_tsh):
    sessions = Path(cfg.teleport.sessions_dir)
    (sessions / "gen-7" / "home").mkdir(parents=True)
    os.symlink("gen-7", sessions / "current")
    push_login_ok(fake_tsh)

    snap = await SESSION.renew(cfg, reason="startup")

    assert snap.generation == 1
    assert not (sessions / "gen-7").exists()
    assert os.readlink(sessions / "current") == "gen-1"


async def test_leftover_cleanup_only_touches_what_db_gateway_created(cfg, fake_tsh):
    sessions = Path(cfg.teleport.sessions_dir)
    (sessions / "gen-3" / "home").mkdir(parents=True)
    os.symlink("gen-3", sessions / ".current-3.tmp")
    (sessions / "unrelated").mkdir()
    (sessions / "notes.txt").write_text("keep")
    push_login_ok(fake_tsh)

    await SESSION.renew(cfg, reason="startup")

    assert not (sessions / "gen-3").exists() and not (sessions / ".current-3.tmp").is_symlink()
    assert (sessions / "unrelated").is_dir() and (sessions / "notes.txt").read_text() == "keep"


async def test_on_demand_sublogins_stay_within_the_request_budget(cfg, monkeypatch, tmp_path):
    from db_gateway import teleport_cli

    deadlines: list[float] = []

    async def fake_run_tsh(args, *, cfg, deadline, **kwargs):
        deadlines.append(deadline)
        return teleport_cli.TshResult(exit_code=0, stdout="", stderr="", timed_out=False,
                                      truncated=False, elapsed_ms=0)

    monkeypatch.setattr(teleport_cli, "run_tsh", fake_run_tsh)
    now = time.monotonic()
    await SESSION._sublogins(cfg, tmp_path, deadline=now + 5)
    assert len(deadlines) == 2 and all(d <= now + 5 for d in deadlines)

    deadlines.clear()
    await SESSION._sublogins(cfg, tmp_path, deadline=time.monotonic() + 0.5)
    assert deadlines == []               # no budget left: done on demand by the tools later


async def test_subcredential_failure_does_not_fail_the_renewal(cfg, fake_tsh):
    fake_tsh.push(profile="cert")                                   # login
    fake_tsh.push(code=1, stderr="ERROR: kube cluster unavailable")  # kube login
    fake_tsh.push()                                                 # apps login
    fake_tsh.push(stdout=status_text())                             # status
    snap = await SESSION.renew(cfg, reason="test")
    assert snap.generation == 1 and SESSION.current() is snap


async def test_listeners_run_after_the_switch(cfg, fake_tsh):
    seen = []

    async def listener(_cfg, snapshot):
        seen.append((snapshot.generation, os.readlink(Path(cfg.teleport.sessions_dir) / "current")))

    SESSION.add_listener(listener)
    push_login_ok(fake_tsh)
    await SESSION.renew(cfg, reason="test")
    await asyncio.sleep(0.05)
    assert seen == [(1, "gen-1")]


# --- account-lock protection ----------------------------------------------------------


async def test_rejected_login_is_retried_once_in_the_next_totp_window(cfg, fake_tsh, no_wait, otps):
    fake_tsh.push(code=1, stderr=REJECTED)
    push_login_ok(fake_tsh)

    snap = await SESSION.renew(cfg, reason="test")

    assert snap.generation == 1
    assert [c["entered"]["otp"] for c in logins(fake_tsh)] == ["111111", "222222"]
    assert len(no_wait) == 1 and 1.5 <= no_wait[0] <= 31.5   # waited for a new window
    assert SESSION.status_info()["auth_failures"] == 0


async def test_two_rejected_logins_stop_automatic_login_until_a_manual_one(cfg, fake_tsh, no_wait, otps):
    fake_tsh.push(code=1, stderr=REJECTED)
    fake_tsh.push(code=1, stderr=REJECTED)
    with pytest.raises(LoginRejected):
        await SESSION.renew(cfg, reason="test")
    assert SESSION.blocked
    assert not list(Path(cfg.teleport.sessions_dir).glob("gen-*"))   # failed dir removed

    calls = len(fake_tsh.calls)
    with pytest.raises(TeleportAuthExpired, match="/teleport-auth"):
        await SESSION.ensure_session(cfg, asyncio.get_running_loop().time() + 30)
    with pytest.raises(LoginRejected):
        await SESSION.renew(cfg, reason="scheduled")
    assert len(fake_tsh.calls) == calls        # no further attempt against the account

    push_login_ok(fake_tsh)
    snap = await SESSION.renew(cfg, reason="manual_web", password="typed-pw", otp="555555")
    assert snap.generation >= 1 and not SESSION.blocked
    assert logins(fake_tsh)[-1]["entered"] == {"password": "typed-pw", "otp": "555555"}


async def test_a_rejected_code_is_never_submitted_again_in_the_same_totp_window(cfg, fake_tsh, monkeypatch):
    """Callers without time to wait for a new window fail without another attempt."""
    window_start = (time.time() // 30) * 30
    monkeypatch.setattr(SESSION, "_wall", lambda: window_start + 1)
    fake_tsh.push(code=1, stderr=REJECTED)
    deadline = asyncio.get_running_loop().time() + 20       # the next window is ~30 s away

    results = await asyncio.gather(*(SESSION.ensure_session(cfg, deadline) for _ in range(2)),
                                   return_exceptions=True)

    assert len(logins(fake_tsh)) == 1                       # the second caller did not reuse the code
    assert not SESSION.blocked and SESSION.status_info()["auth_failures"] == 1
    assert all(isinstance(r, LoginFailed) and not isinstance(r, LoginRejected) for r in results)


async def test_automatic_login_waits_for_a_new_window_after_a_manual_login(cfg, fake_tsh, no_wait, otps):
    push_login_ok(fake_tsh)
    push_login_ok(fake_tsh)
    await SESSION.renew(cfg, reason="manual_web", password="typed-pw", otp="555555")
    await SESSION.renew(cfg, reason="scheduled")
    assert len(no_wait) == 1          # the person's code is this window's code too


async def test_background_renewal_is_skipped_when_another_caller_renewed_meanwhile(cfg, fake_tsh):
    push_login_ok(fake_tsh)
    on_demand = asyncio.ensure_future(SESSION.ensure_session(cfg, asyncio.get_running_loop().time() + 30))
    await asyncio.sleep(0.05)                               # it holds the lock now

    snap = await SESSION.renew_if_unchanged(cfg, seen=None, reason="scheduled")

    assert snap is await on_demand
    assert len(logins(fake_tsh)) == 1


async def test_background_renewal_runs_when_nothing_changed(cfg, fake_tsh):
    first = install_session(cfg, generation=1, minutes_left=5)
    push_login_ok(fake_tsh)
    snap = await SESSION.renew_if_unchanged(cfg, seen=first, reason="scheduled")
    assert snap.generation == 2


async def test_locked_account_stops_after_one_attempt(cfg, fake_tsh, no_wait, otps):
    fake_tsh.push(code=1, stderr="ERROR: user u is temporarily locked until 2030-01-01\n")
    with pytest.raises(LoginRejected):
        await SESSION.renew(cfg, reason="test")
    assert len(logins(fake_tsh)) == 1 and SESSION.blocked and no_wait == []


async def test_network_failure_is_not_counted_as_a_rejection(cfg, fake_tsh, no_wait, otps):
    fake_tsh.push(code=1, stderr="ERROR: dial tcp: lookup teleport.dify.dev: i/o timeout\n")
    with pytest.raises(LoginFailed) as info:
        await SESSION.renew(cfg, reason="test")
    assert not isinstance(info.value, LoginRejected)
    assert SESSION.status_info()["auth_failures"] == 0 and not SESSION.blocked
    assert len(logins(fake_tsh)) == 1 and no_wait == []


async def test_manual_login_with_a_wrong_code_does_not_block_automatic_login(cfg, fake_tsh):
    fake_tsh.push(code=1, stderr=REJECTED)
    with pytest.raises(LoginRejected):
        await SESSION.renew(cfg, reason="manual_web", password="pw", otp="000000")
    assert not SESSION.blocked and len(logins(fake_tsh)) == 1   # no retry for a typed code


def test_login_output_classification():
    assert classify_login_output("ERROR: invalid credentials") == "auth"
    assert classify_login_output("ERROR: access denied to perform action") == "auth"
    assert classify_login_output("ERROR: user u is temporarily locked") == "locked"
    assert classify_login_output("ERROR: dial tcp 1.2.3.4:443: connect: connection refused") == "transient"
    assert classify_login_output("ERROR: lookup teleport.dify.dev: no such host") == "transient"
    assert classify_login_output("ERROR: something unexpected") == "auth"   # conservative


def test_login_output_is_cleaned_and_redacted_before_it_is_cut():
    from db_gateway.teleport_login import _tail

    raw = "\x1b]11;?\x1b\\\x1b[6n\x1b[31mERROR: \x1b[0mdial tcp: connection refused"
    assert _tail(raw) == "ERROR: dial tcp: connection refused"
    cut = _tail("x" * 700 + "hunter2", ("hunter2",))
    assert cut.endswith("<redacted>") and "hunter" not in cut


# --- ensure_session ---------------------------------------------------------------------


async def test_usable_session_is_returned_without_running_tsh(cfg, fake_tsh):
    snap = install_session(cfg)
    assert await SESSION.ensure_session(cfg, asyncio.get_running_loop().time() + 30) is snap
    assert fake_tsh.calls == []


async def test_concurrent_callers_share_one_on_demand_login(cfg, fake_tsh):
    push_login_ok(fake_tsh)
    deadline = asyncio.get_running_loop().time() + 30
    snaps = await asyncio.gather(*(SESSION.ensure_session(cfg, deadline) for _ in range(3)))
    assert len({s.generation for s in snaps}) == 1
    assert len(logins(fake_tsh)) == 1


async def test_expiring_session_is_renewed_on_demand(cfg, fake_tsh):
    install_session(cfg, generation=1, minutes_left=0.5)    # less than the safety margin
    push_login_ok(fake_tsh)
    snap = await SESSION.ensure_session(cfg, asyncio.get_running_loop().time() + 30)
    assert snap.generation == 2


async def test_without_credentials_there_is_no_automatic_login(cfg, fake_tsh):
    cfg.teleport.password = ""
    assert SESSION.can_auto_login(cfg) is False
    with pytest.raises(TeleportAuthExpired, match="/teleport-auth"):
        await SESSION.ensure_session(cfg, asyncio.get_running_loop().time() + 30)
    assert fake_tsh.calls == []


def test_status_info_never_contains_secrets(cfg):
    install_session(cfg)
    info = SESSION.status_info()
    assert info["generation"] == 1 and info["valid_until"] and info["blocked"] is None
    assert "test-password" not in str(info)
    assert (datetime.fromisoformat(info["valid_until"]) - datetime.now(timezone.utc)) > timedelta(minutes=50)
