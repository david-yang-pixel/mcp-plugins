from __future__ import annotations

import asyncio
import logging
from typing import Protocol


logger = logging.getLogger("uvicorn.error")


class ReadinessMaintainer(Protocol):
    async def check_readiness(self) -> object: ...


async def run_session_supervisor(
    relay: ReadinessMaintainer,
    *,
    interval_seconds: float,
) -> None:
    """Keep the standalone Relay session healthy without external probes."""
    while True:
        try:
            await relay.check_readiness()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - supervisor must stay alive.
            logger.warning(
                "browser_relay_session_supervisor_failed error_type=%s",
                type(exc).__name__,
            )
        await asyncio.sleep(interval_seconds)
