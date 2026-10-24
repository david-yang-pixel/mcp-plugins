from __future__ import annotations

import asyncio
import logging
from typing import Protocol


logger = logging.getLogger("uvicorn.error")


class ContextRecycler(Protocol):
    async def recycle_context_if_over_memory_limit(self) -> bool: ...


async def run_browser_resource_watchdog(
    relay: ContextRecycler,
    *,
    interval_seconds: float,
    memory_limit_mb: int,
) -> None:
    """Recycle the browser context when Chrome RSS exceeds the configured limit."""
    if memory_limit_mb <= 0:
        return
    while True:
        try:
            recycled = await relay.recycle_context_if_over_memory_limit()
            if recycled:
                logger.warning(
                    "browser_relay_memory_recycle limit_mb=%s",
                    memory_limit_mb,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - watchdog must stay alive.
            logger.warning(
                "browser_relay_resource_watchdog_failed error_type=%s",
                type(exc).__name__,
            )
        await asyncio.sleep(interval_seconds)
