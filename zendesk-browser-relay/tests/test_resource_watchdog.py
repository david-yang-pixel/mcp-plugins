import asyncio

import pytest


@pytest.mark.asyncio
async def test_resource_watchdog_recycles_when_over_limit(monkeypatch):
    from zendesk_browser_relay.resource_watchdog import run_browser_resource_watchdog

    calls = {"count": 0}

    class Relay:
        async def recycle_context_if_over_memory_limit(self) -> bool:
            calls["count"] += 1
            return calls["count"] == 1

    task = asyncio.create_task(
        run_browser_resource_watchdog(
            Relay(),
            interval_seconds=0.01,
            memory_limit_mb=512,
        )
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert calls["count"] >= 1


def test_profile_rss_includes_chromium_descendants_without_profile_arg(
    monkeypatch, tmp_path
):
    from pathlib import Path
    from types import SimpleNamespace

    import zendesk_browser_relay.browser_relay as browser_relay

    proc_root = tmp_path / "proc"
    profile = str((tmp_path / "profile").resolve())

    def process(pid, parent, command, rss_mb):
        entry = proc_root / str(pid)
        entry.mkdir(parents=True)
        (entry / "cmdline").write_bytes(b"\0".join(part.encode() for part in command))
        (entry / "status").write_text(
            f"Name:\tchrome\nPPid:\t{parent}\nVmRSS:\t{rss_mb * 1024} kB\n"
        )

    process(100, 1, ["chrome", f"--user-data-dir={profile}"], 100)
    process(101, 100, ["chrome", "--type=zygote"], 50)
    process(102, 101, ["chrome", "--type=renderer"], 1600)
    process(200, 1, ["chrome", f"--user-data-dir={profile}-other"], 900)
    process(201, 200, ["chrome", "--type=renderer"], 800)

    original_path = Path
    monkeypatch.setattr(
        browser_relay,
        "Path",
        lambda value: proc_root if value == "/proc" else original_path(value),
    )
    assert browser_relay.profile_chromium_rss_mb(
        SimpleNamespace(resolve=lambda: profile)
    ) == 1750.0
