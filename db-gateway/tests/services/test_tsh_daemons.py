"""tsh_daemons: long-running tsh processes (AWS proxy, db tunnels) on db-gateway's own session."""
import asyncio
import re
import time
from unittest.mock import AsyncMock

import pytest

from db_gateway.postgres import TeleportAuthExpired
from db_gateway.teleport_cli import TshTimeout
from db_gateway.teleport_session import live_home
from db_gateway.tsh_daemons import TshDaemon, TshDaemonError, stop_all
from tsh_helpers import alive, install_session

READY = "Started tunnel on 127.0.0.1:{port}.\n"
TUNNEL_READY = ('Started authenticated tunnel for the PostgreSQL database "{db}" in cluster '
                '"teleport.dify.dev" on 127.0.0.1:{port}.\n')


def dl(seconds):
    return time.monotonic() + seconds


def parse_port(lines):
    for line in lines:
        m = re.search(r"127\.0\.0\.1:(\d+)", line)
        if m:
            return {"port": m.group(1)}
    return None


def make(**kw):
    return TshDaemon("test", args=lambda cfg: ["proxy", "x"], parse=parse_port, **kw)


@pytest.fixture(autouse=True)
async def _stop_daemons():
    yield
    await stop_all()


@pytest.fixture(autouse=True)
def session(cfg):
    """A live login, so daemons start without running `tsh login`."""
    return install_session(cfg)


async def test_ensure_parses_ready_output_and_reuses_process(cfg, fake_tsh):
    fake_tsh.push(stdout=READY.format(port=5555), sleep=30)
    d = make()
    assert await d.ensure(cfg, dl(5)) == {"port": "5555"}
    assert await d.ensure(cfg, dl(5)) == {"port": "5555"}
    assert [c["argv"] for c in fake_tsh.calls] == [["proxy", "x"]]
    # The live symlinked home: the process follows every renewal by itself.
    assert fake_tsh.calls[0]["env"]["TELEPORT_HOME"] == str(live_home(cfg))
    assert alive(d.pid)


async def test_renewal_does_not_restart_a_running_daemon(cfg, fake_tsh):
    fake_tsh.push(stdout=READY.format(port=5555), sleep=30)
    d = make()
    await d.ensure(cfg, dl(5))
    pid = d.pid
    install_session(cfg, generation=2)                       # renewed: current -> gen-2
    assert await d.ensure(cfg, dl(5)) == {"port": "5555"}
    assert d.pid == pid and len(fake_tsh.calls) == 1


async def test_no_session_means_no_start(cfg, fake_tsh):
    from db_gateway.teleport_session import _reset_state

    _reset_state()                                            # nobody logged in
    cfg.teleport.password = ""                                # and no automatic login
    with pytest.raises(TeleportAuthExpired):
        await make().ensure(cfg, dl(5))
    assert fake_tsh.calls == []


async def test_rotate_starts_the_new_process_before_stopping_the_old(cfg, fake_tsh, monkeypatch):
    from db_gateway import tsh_daemons

    monkeypatch.setattr(tsh_daemons, "ROTATE_KILL_DELAY_SECONDS", 0.3)
    fake_tsh.push(stdout=READY.format(port=5555), sleep=30)
    fake_tsh.push(stdout=READY.format(port=6666), sleep=30)
    d = make()
    await d.ensure(cfg, dl(5))
    old = d.pid
    await d.rotate(cfg, dl(5))
    assert d.pid != old and await d.ensure(cfg, dl(5)) == {"port": "6666"}
    assert alive(old)                 # still serving requests that started on it
    await asyncio.sleep(0.6)
    assert not alive(old)


async def test_stop_also_kills_a_rotated_process_still_waiting_to_retire(cfg, fake_tsh):
    fake_tsh.push(stdout=READY.format(port=5555), sleep=30)
    fake_tsh.push(stdout=READY.format(port=6666), sleep=30)
    d = make()
    await d.ensure(cfg, dl(5))
    old = d.pid
    await d.rotate(cfg, dl(5))
    new = d.pid
    await d.stop()
    await asyncio.sleep(0.2)
    assert not alive(old) and not alive(new)


def test_a_retired_process_outlives_any_tool_call_that_started_on_it():
    from db_gateway import tsh_daemons
    from db_gateway.config import TeleportToolsConfig, TeleportConfig

    budget = TeleportToolsConfig().time_budget_seconds
    assert budget < tsh_daemons.ROTATE_KILL_DELAY_SECONDS < TeleportConfig().session_retain_seconds


async def test_failed_rotation_keeps_the_running_process(cfg, fake_tsh):
    fake_tsh.push(stdout=READY.format(port=5555), sleep=30)
    fake_tsh.push(stderr="ERROR: boom\n", code=1)
    d = make()
    await d.ensure(cfg, dl(5))
    old = d.pid
    with pytest.raises(TshDaemonError):
        await d.rotate(cfg, dl(5))
    assert d.pid == old and alive(old) and await d.ensure(cfg, dl(5)) == {"port": "5555"}


async def test_rotate_does_nothing_when_not_running(cfg, fake_tsh):
    await make().rotate(cfg, dl(5))
    assert fake_tsh.calls == []


async def test_restart_after_process_exit(cfg, fake_tsh):
    fake_tsh.push(stdout=READY.format(port=5555), sleep=0.2)
    fake_tsh.push(stdout=READY.format(port=6666), sleep=30)
    d = make()
    await d.ensure(cfg, dl(5))
    await asyncio.sleep(0.6)
    assert await d.ensure(cfg, dl(5)) == {"port": "6666"} and len(fake_tsh.calls) == 2


async def test_mark_stale_forces_restart(cfg, fake_tsh):
    fake_tsh.push(stdout=READY.format(port=5555), sleep=30)
    fake_tsh.push(stdout=READY.format(port=6666), sleep=30)
    d = make()
    await d.ensure(cfg, dl(5))
    old = d.pid
    d.mark_stale()
    assert await d.ensure(cfg, dl(5)) == {"port": "6666"} and not alive(old)


async def test_exit_before_ready_raises_with_output_tail(cfg, fake_tsh):
    fake_tsh.push(stdout="", stderr="\x1b[31mERROR: \x1b[0mboom happened\n", code=1)
    with pytest.raises(TshDaemonError, match="boom happened"):
        await make().ensure(cfg, dl(5))


async def test_ready_timeout_kills_process_group(cfg, fake_tsh):
    fake_tsh.push(stdout="still starting\n", child_sleep=30, sleep=30)
    t0 = time.monotonic()
    with pytest.raises(TshDaemonError, match="still starting"):
        await make(start_timeout=1).ensure(cfg, dl(10))
    assert time.monotonic() - t0 < 4 and not alive(fake_tsh.child_pid())


async def test_past_deadline_raises_timeout(cfg, fake_tsh):
    with pytest.raises(TshTimeout):
        await make().ensure(cfg, time.monotonic() - 1)
    assert fake_tsh.calls == []


async def test_before_start_runs_first_and_only_on_start(cfg, fake_tsh):
    order = []
    before = AsyncMock(side_effect=lambda cfg, deadline: order.append(len(fake_tsh.calls)))
    fake_tsh.push(stdout=READY.format(port=5555), sleep=30)
    d = make(before_start=before)
    await d.ensure(cfg, dl(5))
    await d.ensure(cfg, dl(5))
    assert before.await_count == 1 and order == [0]


async def test_concurrent_ensure_starts_once(cfg, fake_tsh):
    fake_tsh.push(stdout=READY.format(port=5555), sleep=30)
    d = make()
    results = await asyncio.gather(*[d.ensure(cfg, dl(5)) for _ in range(5)])
    assert all(r == {"port": "5555"} for r in results) and len(fake_tsh.calls) == 1


async def test_stop_all_kills_processes(cfg, fake_tsh):
    fake_tsh.push(stdout=READY.format(port=5555), sleep=30, child_sleep=30)
    d = make()
    await d.ensure(cfg, dl(5))
    for _ in range(40):                       # 假 tsh 在就绪行之后才起子进程、写 child.pid
        if (fake_tsh.dir / "child.pid").exists():
            break
        await asyncio.sleep(0.05)
    pid, child = d.pid, fake_tsh.child_pid()
    await stop_all()
    assert not alive(pid) and not alive(child)


async def test_knowledgefs_tunnel_args_and_ready(cfg, fake_tsh):
    from db_gateway.tsh_daemons import ensure_tunnel

    fake_tsh.push(stdout='Started authenticated tunnel for the PostgreSQL database '
                         '"knowledge-fs-production-readonly" in cluster "teleport.dify.dev" '
                         'on 127.0.0.1:56064.\n', sleep=30)
    assert await ensure_tunnel(cfg, "knowledgefs", dl(5)) == 56064
    assert fake_tsh.calls[0]["argv"] == [
        "proxy", "db", "--tunnel", "--port", "56064", "--db-user", "pro_ops_teleport_read",
        "--db-name", "knowledgefs", "knowledge-fs-production-readonly",
    ]


# --- final review fixes -----------------------------------------------------------


async def test_tunnel_bind_error_is_not_ready(cfg, fake_tsh):
    from db_gateway.tsh_daemons import ensure_tunnel

    fake_tsh.push(stderr="ERROR: listen tcp 127.0.0.1:56064: bind: address already in use\n", code=1)
    with pytest.raises(TshDaemonError, match="address already in use"):
        await ensure_tunnel(cfg, "knowledgefs", dl(5))


async def test_tunnel_on_unexpected_port_is_an_error(cfg, fake_tsh):
    from db_gateway.tsh_daemons import ensure_tunnel

    fake_tsh.push(stdout='Started authenticated tunnel for the PostgreSQL database "x" in cluster "c" '
                         'on 127.0.0.1:41000.\n', sleep=30)
    with pytest.raises(TshDaemonError, match="56064"):
        await ensure_tunnel(cfg, "knowledgefs", dl(5))


async def test_ready_timeout_raises_daemon_timeout(cfg, fake_tsh):
    from db_gateway.tsh_daemons import TshDaemonTimeout

    fake_tsh.push(stdout="still starting\n", sleep=30)
    with pytest.raises(TshDaemonTimeout):
        await make(start_timeout=1).ensure(cfg, dl(10))


# --- PR #30 review (Codex) ----------------------------------------------------------

SECRET_OUT = ("Started AWS proxy on http://127.0.0.1:45137.\n"
              "  export AWS_ACCESS_KEY_ID=AKIAREALLOOKING\n"
              "  export AWS_SECRET_ACCESS_KEY=supersecretvalue\n")


async def test_credentials_are_redacted_in_error_tail(cfg, fake_tsh):
    fake_tsh.push(stdout=SECRET_OUT, code=1)            # 打印了凭据但没就绪就退出
    never_ready = TshDaemon("test", args=lambda cfg: ["proxy", "aws"], parse=lambda lines: None)
    with pytest.raises(TshDaemonError) as exc:
        await never_ready.ensure(cfg, dl(5))
    text = str(exc.value)
    assert "supersecretvalue" not in text and "AKIAREALLOOKING" not in text
    assert "AWS_SECRET_ACCESS_KEY=<redacted>" in text


async def test_credentials_are_redacted_in_kept_output(cfg, fake_tsh):
    from db_gateway.aws_logs import AWS_PROXY

    fake_tsh.push()                                       # apps login
    fake_tsh.push(stdout=SECRET_OUT + '  export AWS_CA_BUNDLE="/x/ca.pem"\n'
                         '  export HTTPS_PROXY="http://127.0.0.1:45137"\n', sleep=30)
    env = await AWS_PROXY.ensure(cfg, dl(10))
    assert env["AWS_SECRET_ACCESS_KEY"] == "supersecretvalue"          # 解析仍拿到真值
    kept = "\n".join(AWS_PROXY.output)
    assert "supersecretvalue" not in kept and "AKIAREALLOOKING" not in kept
    assert "HTTPS_PROXY" in kept


async def test_knowledgefs_tunnel_uses_postgres_user(cfg, fake_tsh):
    from db_gateway.tsh_daemons import ensure_tunnel

    cfg.postgres.user = "other_reader"
    fake_tsh.push(stdout='Started authenticated tunnel for the PostgreSQL database "x" in cluster "c" '
                         'on 127.0.0.1:56064.\n', sleep=30)
    await ensure_tunnel(cfg, "knowledgefs", dl(5))
    argv = fake_tsh.calls[0]["argv"]
    assert argv[argv.index("--db-user") + 1] == "other_reader"


async def test_starts_counts_every_start(cfg, fake_tsh):
    fake_tsh.push(stdout=READY.format(port=5555), sleep=30)
    fake_tsh.push(stdout=READY.format(port=6666), sleep=30)
    d = make()
    assert d.starts == 0
    await d.ensure(cfg, dl(5))
    await d.ensure(cfg, dl(5))                    # reused: no new start
    assert d.starts == 1
    d.mark_stale()
    await d.ensure(cfg, dl(5))
    assert d.starts == 2


# --- self-managed DB tunnels --------------------------------------------------------


def test_tunnel_specs_follow_the_config(cfg):
    from db_gateway.tsh_daemons import tunnel_spec

    assert tunnel_spec(cfg, "prod") is None              # step 1: host tunnel
    assert tunnel_spec(cfg, "billing") is None and tunnel_spec(cfg, "plugin") is None
    kfs = tunnel_spec(cfg, "knowledgefs")
    assert (kfs.teleport_db, kfs.db_name, kfs.port) == ("knowledge-fs-production-readonly", "knowledgefs", 56064)

    cfg.postgres.teleport_db_prod = "lg-pgsql-read-01"
    cfg.postgres.teleport_db_billing = "lg-billing-01"
    cfg.postgres.teleport_db_plugin = "lg-pgsql-read-01"
    specs = {k: tunnel_spec(cfg, k) for k in ("prod", "billing", "plugin")}
    assert {k: (v.teleport_db, v.db_name, v.port) for k, v in specs.items()} == {
        "prod": ("lg-pgsql-read-01", "langgenius", 56071),
        "billing": ("lg-billing-01", "billing", 56072),
        "plugin": ("lg-pgsql-read-01", "dify_plugin_daemon", 56073),
    }
    cfg.teleport_tools.knowledgefs.enabled = False
    assert tunnel_spec(cfg, "knowledgefs") is None


async def test_ensure_tunnel_runs_tsh_proxy_db_on_the_fixed_port(cfg, fake_tsh):
    from db_gateway.tsh_daemons import ensure_tunnel

    cfg.postgres.teleport_db_billing = "lg-billing-01"
    fake_tsh.push(stdout=TUNNEL_READY.format(db="lg-billing-01", port=56072), sleep=30)
    assert await ensure_tunnel(cfg, "billing", dl(5)) == 56072
    assert fake_tsh.calls[0]["argv"] == [
        "proxy", "db", "--tunnel", "--port", "56072", "--db-user", cfg.postgres.user,
        "--db-name", "billing", "lg-billing-01",
    ]
    assert fake_tsh.calls[0]["env"]["TELEPORT_HOME"] == str(live_home(cfg))
