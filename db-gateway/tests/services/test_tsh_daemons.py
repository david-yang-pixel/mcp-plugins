"""tsh_daemons: long-running tsh processes (aws proxy, db tunnel) on the shadow home."""
import asyncio
import re
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from db_gateway.teleport_cli import TshTimeout
from db_gateway.tsh_daemons import TshDaemon, TshDaemonError, stop_all
from tsh_helpers import alive, write_tsh_src

READY = "Started tunnel on 127.0.0.1:{port}.\n"


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


async def test_ensure_parses_ready_output_and_reuses_process(cfg, fake_tsh):
    fake_tsh.push(stdout=READY.format(port=5555), sleep=30)
    d = make()
    assert await d.ensure(cfg, dl(5)) == {"port": "5555"}
    assert await d.ensure(cfg, dl(5)) == {"port": "5555"}
    assert [c["argv"] for c in fake_tsh.calls] == [["proxy", "x"]]
    assert fake_tsh.calls[0]["env"]["TELEPORT_HOME"] == cfg.teleport_tools.shadow_home
    assert alive(d.pid)


async def test_restart_after_shadow_signature_change(cfg, fake_tsh):
    fake_tsh.push(stdout=READY.format(port=5555), sleep=30)
    fake_tsh.push(stdout=READY.format(port=6666), sleep=30)
    d = make()
    await d.ensure(cfg, dl(5))
    old = d.pid
    write_tsh_src(Path(cfg.teleport.auth_home), cert="cert-2")      # watchdog 续期
    assert await d.ensure(cfg, dl(5)) == {"port": "6666"}
    assert d.pid != old and not alive(old)


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
    from db_gateway.tsh_daemons import ensure_knowledgefs_tunnel

    fake_tsh.push(stdout='Started authenticated tunnel for the PostgreSQL database '
                         '"knowledge-fs-production-readonly" in cluster "teleport.dify.dev" '
                         'on 127.0.0.1:56064.\n', sleep=30)
    assert await ensure_knowledgefs_tunnel(cfg, dl(5)) == {"port": "56064"}
    assert fake_tsh.calls[0]["argv"] == [
        "proxy", "db", "--tunnel", "--port", "56064", "--db-user", "pro_ops_teleport_read",
        "--db-name", "knowledgefs", "knowledge-fs-production-readonly",
    ]


# --- final review fixes -----------------------------------------------------------


async def test_tunnel_bind_error_is_not_ready(cfg, fake_tsh):
    from db_gateway.tsh_daemons import ensure_knowledgefs_tunnel

    fake_tsh.push(stderr="ERROR: listen tcp 127.0.0.1:56064: bind: address already in use\n", code=1)
    with pytest.raises(TshDaemonError, match="address already in use"):
        await ensure_knowledgefs_tunnel(cfg, dl(5))


async def test_tunnel_on_unexpected_port_is_an_error(cfg, fake_tsh):
    from db_gateway.tsh_daemons import ensure_knowledgefs_tunnel

    fake_tsh.push(stdout='Started authenticated tunnel for the PostgreSQL database "x" in cluster "c" '
                         'on 127.0.0.1:41000.\n', sleep=30)
    with pytest.raises(TshDaemonError, match="56064"):
        await ensure_knowledgefs_tunnel(cfg, dl(5))


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
    from db_gateway.tsh_daemons import ensure_knowledgefs_tunnel

    cfg.postgres.user = "other_reader"
    fake_tsh.push(stdout='Started authenticated tunnel for the PostgreSQL database "x" in cluster "c" '
                         'on 127.0.0.1:56064.\n', sleep=30)
    await ensure_knowledgefs_tunnel(cfg, dl(5))
    argv = fake_tsh.calls[0]["argv"]
    assert argv[argv.index("--db-user") + 1] == "other_reader"


async def test_refresh_running_restarts_only_running_daemons_after_renewal(cfg, fake_tsh):
    """Background renewal handling: restart now, not on the user's next tool call."""
    from db_gateway.tsh_daemons import refresh_running

    fake_tsh.push(stdout=READY.format(port=5555), sleep=30)
    fake_tsh.push(stdout=READY.format(port=6666), sleep=30)
    running, idle = make(), make()
    await running.ensure(cfg, dl(5))
    old = running.pid
    write_tsh_src(Path(cfg.teleport.auth_home), cert="cert-2")      # watchdog 续期

    await refresh_running(cfg, dl(5))

    assert running.pid != old and alive(running.pid) and not alive(old)
    assert idle.pid is None                       # never started: still lazy
    assert len(fake_tsh.calls) == 2


async def test_refresh_running_survives_a_failed_restart(cfg, fake_tsh):
    from db_gateway.tsh_daemons import refresh_running

    fake_tsh.push(stdout=READY.format(port=5555), sleep=30)
    fake_tsh.push(stdout="ERROR: access denied\n", code=1)
    d = make()
    await d.ensure(cfg, dl(5))
    write_tsh_src(Path(cfg.teleport.auth_home), cert="cert-2")

    await refresh_running(cfg, dl(5))             # logs, does not raise

    assert d.pid is None                          # the next ensure() starts it again
