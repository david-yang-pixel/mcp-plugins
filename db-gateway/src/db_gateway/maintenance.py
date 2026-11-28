"""Background maintenance for db-gateway, off the request path.

* Teleport renewal: tsh-auth-watchdog renews the shared session about every
  35 minutes. A renewal is detected from the user certificate digest
  (``teleport_cli.source_signature``), not from every file under ``keys/``:
  the tsh-proxy-db tunnels also write their database certificates there. On a
  renewal the running tsh daemons (KnowledgeFS tunnel, AWS proxy) are restarted
  first, then every pooled connection is verified, so the next query neither
  waits for a restart nor lands on a dead connection.
* Keepalive: idle pooled connections are probed every ``pool_keepalive_seconds``
  so that a query never pays the checkout probe (see ``PoolManager.keepalive``).
"""
from __future__ import annotations

import asyncio
import math
import time
from typing import Any, Awaitable, Callable, Optional

from db_gateway.log import get_logger
from db_gateway.teleport_cli import source_signature

logger = get_logger(module="maintenance")

# Budget for restarting the daemons after a renewal (tsh startup + ready line).
_DAEMON_REFRESH_BUDGET_SECONDS = 40.0
# After a renewal, verify every idle connection on each tick for this long:
# tunnels outside this process (tsh-proxy-db*) may restart later than we notice.
_RENEWAL_GRACE_SECONDS = 90.0
# Floors: never a busy loop, never a probe shorter than a few round trips.
_MIN_INTERVAL_SECONDS = 1.0
_MIN_PROBE_TIMEOUT_SECONDS = 1.0


def _number(obj: Any, name: str, default: float) -> float:
    value = getattr(obj, name, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


class Maintenance:
    def __init__(
        self,
        cfg: Any,
        *,
        manager: Any = None,
        refresh_daemons: Optional[Callable[[Any, float], Awaitable[None]]] = None,
    ) -> None:
        if manager is None:
            from db_gateway.pg_pool import MANAGER as manager  # noqa: N811
        if refresh_daemons is None:
            from db_gateway.tsh_daemons import refresh_running as refresh_daemons
        pg = getattr(cfg, "postgres", None)
        self.cfg = cfg
        self.manager = manager
        self.refresh_daemons = refresh_daemons
        self.interval = max(_MIN_INTERVAL_SECONDS, _number(pg, "pool_maintenance_interval_seconds", 10.0))
        self.keepalive_seconds = max(0.0, _number(pg, "pool_keepalive_seconds", 30.0))
        self.probe_timeout = max(_MIN_PROBE_TIMEOUT_SECONDS, _number(pg, "pool_probe_timeout_seconds", 5.0))
        watch = getattr(pg, "pool_watch_teleport_keys", True)
        auth_home = getattr(getattr(cfg, "teleport", None), "auth_home", None)
        self.auth_home = auth_home if watch is not False and isinstance(auth_home, str) else None
        self._signature: Optional[list[list[str]]] = None
        self._last_keepalive = -math.inf
        self._grace_until = -math.inf

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - keep maintaining
                logger.warning("maintenance_tick_failed", error=f"{type(e).__name__}: {str(e)[:300]}")

    async def tick(self, now: Optional[float] = None) -> None:
        now = time.monotonic() if now is None else now
        if self.auth_home is not None:
            signature = source_signature(self.auth_home)
            # None during the renewal window (profile purged, login running): wait
            # for the new certificate instead of reacting to the gap.
            if signature is not None:
                renewed = self._signature is not None and signature != self._signature
                self._signature = signature
                if renewed:
                    await self._on_renewal(now)
                    return
        if now <= self._grace_until:
            self._last_keepalive = now
            await self.manager.keepalive(older_than=0.0, probe_timeout=self.probe_timeout)
            return
        if self.keepalive_seconds > 0 and now - self._last_keepalive >= self.keepalive_seconds:
            self._last_keepalive = now
            # A connection verified just after a pass is probed at the next one,
            # so none goes unverified past keepalive + interval seconds.
            older_than = max(0.0, self.keepalive_seconds - self.interval)
            await self.manager.keepalive(older_than=older_than, probe_timeout=self.probe_timeout)

    async def _on_renewal(self, now: float) -> None:
        logger.info("teleport_renewal_detected")
        self._last_keepalive = now
        self._grace_until = now + _RENEWAL_GRACE_SECONDS
        # Pools first, so their check does not wait behind the AWS proxy restart
        # (apps login + start); then the daemons; then the pools again, because a
        # restarted KnowledgeFS tunnel dropped the connections made through it.
        await self.manager.keepalive(older_than=0.0, probe_timeout=self.probe_timeout)
        await self.refresh_daemons(self.cfg, time.monotonic() + _DAEMON_REFRESH_BUDGET_SECONDS)
        await self.manager.keepalive(older_than=0.0, probe_timeout=self.probe_timeout)
