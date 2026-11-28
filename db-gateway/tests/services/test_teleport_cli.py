"""teleport_cli: the tsh subprocess layer on db-gateway's own session."""
import asyncio
import time
from pathlib import Path

import pytest

from db_gateway.postgres import TeleportAuthExpired
from db_gateway.teleport_cli import (
    TeleportNotAuthorized,
    TshTimeout,
    run_tsh,
    run_tsh_with_login,
)
from db_gateway.teleport_session import live_home, live_kubeconfig
from tsh_helpers import install_session


def dl(seconds):
    return time.monotonic() + seconds


def _alive(pid):
    """False when the process is gone or a zombie."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat.rsplit(")", 1)[1].split()[0] != "Z"


async def test_run_tsh_env(cfg, fake_tsh, monkeypatch):
    for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY", "no_proxy"):
        monkeypatch.setenv(k, "http://127.0.0.1:7890")
    fake_tsh.push(stdout="ok\n")
    r = await run_tsh(["status"], cfg=cfg, deadline=dl(5))
    assert (r.exit_code, r.stdout, r.timed_out, r.truncated) == (0, "ok\n", False, False)
    call = fake_tsh.calls[0]
    assert call["argv"] == ["status"]
    assert call["env"]["TELEPORT_HOME"] == str(live_home(cfg))          # current/home
    assert call["env"]["KUBECONFIG"] == str(live_kubeconfig(cfg))
    assert call["env"]["TELEPORT_TOOLS_VERSION"] == "off"
    assert not [k for k in call["env"]
                if k.lower() in ("http_proxy", "https_proxy", "all_proxy", "no_proxy")]


async def test_run_tsh_can_pin_a_session_generation(cfg, fake_tsh):
    snap = install_session(cfg, generation=3)
    fake_tsh.push()
    await run_tsh(["kubectl", "get", "ns"], cfg=cfg, deadline=dl(5),
                  home=snap.home_dir, kubeconfig=snap.kubeconfig_path)
    env = fake_tsh.calls[0]["env"]
    assert env["TELEPORT_HOME"] == str(snap.home_dir) and env["KUBECONFIG"] == str(snap.kubeconfig_path)
    assert "gen-3" in env["TELEPORT_HOME"]


async def test_run_tsh_passes_shell_metacharacters_verbatim(cfg, fake_tsh):
    fake_tsh.push()
    await run_tsh(["aws", "--filter-pattern", '"ERROR"; rm -rf / $(x)'], cfg=cfg, deadline=dl(5))
    assert fake_tsh.calls[0]["argv"][2] == '"ERROR"; rm -rf / $(x)'


async def test_run_tsh_caps_stdout_and_decodes_bad_bytes(cfg, fake_tsh):
    fake_tsh.push(stdout_bytes=b"\xff" + b"a" * 5000)
    r = await run_tsh(["x"], cfg=cfg, deadline=dl(5), max_output_bytes=1000)
    assert r.truncated is True and len(r.stdout.encode()) <= 1002 and r.exit_code == 0


async def test_run_tsh_strips_ansi(cfg, fake_tsh):
    fake_tsh.push(stderr="\x1b[31mERROR: \x1b[0mexit status 255\n", code=1)
    r = await run_tsh(["x"], cfg=cfg, deadline=dl(5))
    assert (r.exit_code, r.stderr) == (1, "ERROR: exit status 255\n")


async def test_deadline_returns_partial(cfg, fake_tsh):
    fake_tsh.push(stdout="first\n", sleep=30)
    r = await run_tsh(["x"], cfg=cfg, deadline=dl(1))
    assert r.timed_out is True and r.exit_code is None and r.stdout == "first\n"
    assert r.elapsed_ms < 4000


async def test_deadline_kills_process_group(cfg, fake_tsh):
    fake_tsh.push(stdout="first\n", child_sleep=30, sleep=30)   # 像 tsh aws 那样带一个子进程
    t0 = time.monotonic()
    r = await run_tsh(["aws"], cfg=cfg, deadline=dl(1))
    assert r.timed_out and r.stdout == "first\n" and time.monotonic() - t0 < 4
    assert not _alive(fake_tsh.child_pid())


async def test_past_deadline_does_not_spawn(cfg, fake_tsh):
    r = await run_tsh(["x"], cfg=cfg, deadline=time.monotonic() - 1)
    assert r.timed_out and r.exit_code is None and fake_tsh.calls == []


async def test_login_then_retry(cfg, fake_tsh):
    fake_tsh.push(stderr="no creds", code=1)    # 第一次执行
    fake_tsh.push()                              # 登录
    fake_tsh.push(stdout="done")                 # 重试
    r = await run_tsh_with_login(["aws", "x"], cfg=cfg, deadline=dl(10),
                                 login_args=["apps", "login", "a"],
                                 needs_login=lambda res: res.exit_code != 0)
    assert r.stdout == "done"
    assert [c["argv"] for c in fake_tsh.calls] == [["aws", "x"], ["apps", "login", "a"], ["aws", "x"]]


async def test_concurrent_calls_login_once(cfg, fake_tsh):
    fake_tsh.until_login(code=1)
    await asyncio.gather(*[run_tsh_with_login(["aws", str(i)], cfg=cfg, deadline=dl(10),
        login_args=["apps", "login", "a"], needs_login=lambda r: r.exit_code != 0)
        for i in range(5)])
    assert sum(c["argv"][:2] == ["apps", "login"] for c in fake_tsh.calls) == 1


async def test_login_timeout_uses_remaining_time(cfg, fake_tsh):
    fake_tsh.push(code=1); fake_tsh.push(sleep=30)          # 登录卡住
    t0 = time.monotonic()
    with pytest.raises(TshTimeout):
        await run_tsh_with_login(["aws"], cfg=cfg, deadline=dl(2),
                                 login_args=["apps", "login", "a"], needs_login=lambda r: True)
    assert time.monotonic() - t0 < 4


async def test_login_failure_without_session_is_expired(cfg, fake_tsh):
    # Nobody is logged in (fixture state): a failed sub-login means the session is gone.
    fake_tsh.push(code=1)
    fake_tsh.push(stderr="ERROR: No proxy address specified, missed --proxy flag?", code=1)
    with pytest.raises(TeleportAuthExpired):
        await run_tsh_with_login(["aws"], cfg=cfg, deadline=dl(10),
                                 login_args=["apps", "login", "a"], needs_login=lambda r: True)


async def test_login_failure_with_session_is_not_authorized(cfg, fake_tsh):
    install_session(cfg)                       # a valid login: a role or app is missing
    fake_tsh.push(code=1); fake_tsh.push(stderr='ERROR: app "a" not found', code=1)
    with pytest.raises(TeleportNotAuthorized, match="production-read-all"):
        await run_tsh_with_login(["aws"], cfg=cfg, deadline=dl(10),
                                 login_args=["apps", "login", "a"], needs_login=lambda r: True)


# --- final review fixes -----------------------------------------------------------


async def test_login_timeout_with_valid_session_is_timeout(cfg, fake_tsh):
    install_session(cfg)
    fake_tsh.push(code=1); fake_tsh.push(sleep=30)
    with pytest.raises(TshTimeout):
        await run_tsh_with_login(["aws"], cfg=cfg, deadline=dl(1),
                                 login_args=["apps", "login", "a"], needs_login=lambda r: True)
