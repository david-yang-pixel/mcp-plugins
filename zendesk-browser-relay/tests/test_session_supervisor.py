import asyncio

import pytest


@pytest.mark.asyncio
async def test_session_supervisor_checks_relay_without_an_http_probe():
    from zendesk_browser_relay.session_supervisor import run_session_supervisor

    checked = asyncio.Event()

    class Relay:
        async def check_readiness(self):
            checked.set()

    task = asyncio.create_task(
        run_session_supervisor(Relay(), interval_seconds=3_600)
    )
    try:
        await asyncio.wait_for(checked.wait(), timeout=1)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

