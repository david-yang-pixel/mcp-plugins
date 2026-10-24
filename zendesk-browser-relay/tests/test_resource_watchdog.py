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
