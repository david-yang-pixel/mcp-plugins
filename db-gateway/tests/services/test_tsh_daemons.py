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
