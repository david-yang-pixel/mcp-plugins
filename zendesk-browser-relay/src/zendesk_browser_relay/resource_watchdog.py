from __future__ import annotations

import asyncio
import logging
from typing import Protocol


logger = logging.getLogger("uvicorn.error")


class ContextRecycler(Protocol):
    async def recycle_context_if_over_resource_limit(self) -> str | None: ...


async def run_browser_resource_watchdog(
    relay: ContextRecycler,
    *,
    interval_seconds: float,
) -> None:
    """Recycle the browser context when Chrome memory or idle CPU stays high."""
    while True:
        try:
            reason = await relay.recycle_context_if_over_resource_limit()
            if reason:
                logger.warning("browser_relay_resource_recycle reason=%s", reason)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - watchdog must stay alive.
            logger.warning(
                "browser_relay_resource_watchdog_failed error_type=%s",
                type(exc).__name__,
            )
        await asyncio.sleep(interval_seconds)
