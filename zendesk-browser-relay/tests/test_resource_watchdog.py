import asyncio
import os
from pathlib import Path

import pytest

from zendesk_browser_relay.browser_relay import (
    ZendeskBrowserRelay,
    profile_chromium_cpu_seconds,
    profile_chromium_memory_mb,
    profile_chromium_pids,
)
from zendesk_browser_relay.settings import Settings


@pytest.mark.asyncio
async def test_resource_watchdog_recycles_when_over_limit(caplog):
    from zendesk_browser_relay.resource_watchdog import run_browser_resource_watchdog

    calls = {"count": 0}

    class Relay:
        async def recycle_context_if_over_resource_limit(self):
            calls["count"] += 1
            return "memory_limit" if calls["count"] == 1 else None

    task = asyncio.create_task(
        run_browser_resource_watchdog(Relay(), interval_seconds=0.01)
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert calls["count"] >= 2
    assert "reason=memory_limit" in caplog.text


@pytest.mark.asyncio
async def test_resource_watchdog_survives_failed_checks():
    from zendesk_browser_relay.resource_watchdog import run_browser_resource_watchdog

    calls = {"count": 0}

    class Relay:
        async def recycle_context_if_over_resource_limit(self):
            calls["count"] += 1
            raise RuntimeError("proc unavailable")

    task = asyncio.create_task(
        run_browser_resource_watchdog(Relay(), interval_seconds=0.01)
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert calls["count"] >= 2


def _fake_process(
    proc: Path,
    pid: int,
    *,
    parent: int,
    argv: list[str],
    pss_kb: int | None = None,
    rss_kb: int = 0,
    ticks: tuple[int, int] = (0, 0),
):
    entry = proc / str(pid)
    entry.mkdir()
    (entry / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    fields = ["S", str(parent)] + ["0"] * 9 + [str(ticks[0]), str(ticks[1])]
    (entry / "stat").write_text(f"{pid} (chrome (x)) " + " ".join(fields))
    (entry / "status").write_text(f"Name:\tchrome\nVmRSS:\t{rss_kb} kB\n")
    if pss_kb is not None:
        (entry / "smaps_rollup").write_text(f"Rss: {rss_kb} kB\nPss: {pss_kb} kB\n")


def test_profile_processes_include_children_and_use_pss(tmp_path):
    proc = tmp_path / "proc"
    proc.mkdir()
    profile = tmp_path / "profile"
    profile.mkdir()
    _fake_process(
        proc, 100, parent=1,
        argv=["chrome", f"--user-data-dir={profile}"],
        pss_kb=300 * 1024, rss_kb=400 * 1024, ticks=(100, 50),
    )
    # Renderer without the profile flag, found through its parent.
    _fake_process(
        proc, 101, parent=100, argv=["chrome", "--type=renderer"],
        pss_kb=200 * 1024, rss_kb=380 * 1024, ticks=(30, 20),
    )
    # No smaps_rollup: falls back to VmRSS.
    _fake_process(
        proc, 102, parent=101, argv=["chrome", "--type=utility"],
        rss_kb=100 * 1024,
    )
    _fake_process(
        proc, 200, parent=1,
        argv=["chrome", f"--user-data-dir={tmp_path / 'other'}"],
        pss_kb=999 * 1024,
    )
    _fake_process(proc, 201, parent=1, argv=["grep", str(profile)], pss_kb=999 * 1024)

    assert profile_chromium_pids(profile, proc) == {100, 101, 102}
    assert profile_chromium_memory_mb(profile, proc) == 600
    assert profile_chromium_cpu_seconds(profile, proc) == 200 / os.sysconf("SC_CLK_TCK")


def _relay(tmp_path, **overrides):
    return ZendeskBrowserRelay(
        Settings(relay_api_key="x" * 32, zendesk_profile_dir=tmp_path, **overrides)
    )


class _Context:
    def __init__(self):
        self.close_count = 0

    async def close(self):
        self.close_count += 1


@pytest.mark.asyncio
async def test_memory_over_limit_recycles_context(monkeypatch, tmp_path):
    relay = _relay(
        tmp_path,
        zendesk_browser_memory_limit_mb=1_000,
        zendesk_browser_cpu_limit_percent=0,
    )
    context = _Context()
    relay._context = context
    usage = iter([1_200.0, 1_300.0])
    monkeypatch.setattr(
        "zendesk_browser_relay.browser_relay.profile_chromium_memory_mb",
        lambda _profile: next(usage),
    )

    assert await relay.recycle_context_if_over_resource_limit() == "memory_limit"
    assert context.close_count == 1
    assert relay._context is None


@pytest.mark.asyncio
async def test_memory_rechecked_under_lock_before_recycling(monkeypatch, tmp_path):
    relay = _relay(
        tmp_path,
        zendesk_browser_memory_limit_mb=1_000,
        zendesk_browser_cpu_limit_percent=0,
    )
    context = _Context()
    relay._context = context
    usage = iter([1_200.0, 800.0])
    monkeypatch.setattr(
        "zendesk_browser_relay.browser_relay.profile_chromium_memory_mb",
        lambda _profile: next(usage),
    )

    assert await relay.recycle_context_if_over_resource_limit() is None
    assert context.close_count == 0


@pytest.mark.asyncio
async def test_sustained_idle_cpu_recycles_context(monkeypatch, tmp_path):
    relay = _relay(
        tmp_path,
        zendesk_browser_memory_limit_mb=0,
        zendesk_browser_cpu_limit_percent=80,
        zendesk_browser_cpu_high_samples=3,
    )
    context = _Context()
    relay._context = context
    clock = [0.0]
    cpu = [0.0]
    monkeypatch.setattr(
        "zendesk_browser_relay.browser_relay.time.monotonic", lambda: clock[0]
    )
    monkeypatch.setattr(
        "zendesk_browser_relay.browser_relay.profile_chromium_cpu_seconds",
        lambda _profile: cpu[0],
    )

    results = []
    for _ in range(4):
        results.append(await relay.recycle_context_if_over_resource_limit())
        clock[0] += 60
        cpu[0] += 57  # 95% of one core.

    assert results == [None, None, None, "cpu_limit"]
    assert context.close_count == 1


@pytest.mark.asyncio
async def test_cpu_while_an_operation_runs_resets_the_streak(monkeypatch, tmp_path):
    relay = _relay(
        tmp_path,
        zendesk_browser_memory_limit_mb=0,
        zendesk_browser_cpu_limit_percent=80,
        zendesk_browser_cpu_high_samples=2,
    )
    relay._context = _Context()
    clock = [0.0]
    cpu = [0.0]
    monkeypatch.setattr(
        "zendesk_browser_relay.browser_relay.time.monotonic", lambda: clock[0]
    )
    monkeypatch.setattr(
        "zendesk_browser_relay.browser_relay.profile_chromium_cpu_seconds",
        lambda _profile: cpu[0],
    )

    lock = relay._get_lock()
    results = []
    for busy in (False, False, True, False):
        if busy:
            await lock.acquire()
        clock[0] += 60
        cpu[0] += 57
        results.append(relay._cpu_over_limit(tmp_path))
        if busy:
            lock.release()

    # First sample is the baseline; the busy sample resets the streak, so
    # the last idle sample starts counting again from one.
    assert results == [False, False, False, False]
    assert relay._cpu_high_samples == 1
