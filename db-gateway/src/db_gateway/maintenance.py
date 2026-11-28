"""Background maintenance for db-gateway, off the request path.

* Teleport session: db-gateway logs in by itself (``teleport_session``). The
  first login runs here, in the background, so startup, ``/healthz`` and
  ``/mcp`` never wait for Teleport; afterwards the session is renewed
  ``renew_before_seconds`` before it expires. Each renewal logs into a fresh
  ``gen-<N>`` directory and switches the ``current`` symlink atomically, so
  queries and running tunnels never see a gap. Network/cluster failures are
  retried with exponential backoff. Rejected credentials stop automatic login
  (account-lock guard, see ``TeleportSessionManager``): a throttled Feishu
  alert then carries a one-time ``/teleport-auth`` link for a manual login,
  which lifts the stop. Without credentials (or with ``auto_renew: false``) a
  gateway that has ``teleport.proxy`` and a user set asks for a manual login the
  same way whenever its session is missing or about to expire; with neither, as
  in the CI smoke test, nothing is attempted.
* Keepalive: idle pooled connections are probed every ``pool_keepalive_seconds``
  so that a query never pays the checkout probe (see ``PoolManager.keepalive``).
  A renewal needs no pool action: connections outlive the certificate that
  opened them, and the self-managed tunnels follow ``current`` without restarting.
"""
from __future__ import annotations

import asyncio
import math
import time
from typing import Any, Awaitable, Callable, Optional

from db_gateway.log import get_logger

logger = get_logger(module="maintenance")

# Floors: never a busy loop, never a probe shorter than a few round trips.
_MIN_INTERVAL_SECONDS = 1.0
_MIN_PROBE_TIMEOUT_SECONDS = 1.0
# Renewal loop: re-check at least this often (a manual login may have renewed
# meanwhile), cap the retry backoff, poll for a manual login while stopped.
_MAX_SLEEP_SECONDS = 300.0
_MAX_BACKOFF_SECONDS = 300.0
_BLOCKED_POLL_SECONDS = 30.0
# Scheduled logins never closer together than this, even if Teleport issues
# certificates that live shorter than renew_before_seconds.
_MIN_RENEW_GAP_SECONDS = 120.0
# A failing renewal alerts only after this many failures in a row (about 3.5
# minutes of backoff), so a blip during a deploy restart does not page anyone.
_ALERT_AFTER_FAILURES = 3

# (title, message, auth link wanted) -> sends the alert; supplied by the app.
Alert = Callable[[str, str, bool], Awaitable[None]]


def _number(obj: Any, name: str, default: float) -> float:
    value = getattr(obj, name, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


def _short(error: BaseException) -> str:
    return f"{type(error).__name__}: {str(error)[:300]}"


class Maintenance:
    def __init__(
        self,
        cfg: Any,
        *,
        manager: Any = None,
        session: Any = None,
        alert: Optional[Alert] = None,
    ) -> None:
        if manager is None:
            from db_gateway.pg_pool import MANAGER as manager  # noqa: N811
        if session is None:
            from db_gateway.teleport_session import SESSION as session  # noqa: N811
        pg = getattr(cfg, "postgres", None)
        tp = getattr(cfg, "teleport", None)
        self.cfg = cfg
        self.manager = manager
        self.session = session
        self.alert = alert
        self.interval = max(_MIN_INTERVAL_SECONDS, _number(pg, "pool_maintenance_interval_seconds", 10.0))
        self.keepalive_seconds = max(0.0, _number(pg, "pool_keepalive_seconds", 30.0))
        self.probe_timeout = max(_MIN_PROBE_TIMEOUT_SECONDS, _number(pg, "pool_probe_timeout_seconds", 5.0))
        self.renew_before = max(0.0, _number(tp, "renew_before_seconds", 480.0))
        self.retry_seconds = max(_MIN_INTERVAL_SECONDS, _number(tp, "renew_retry_seconds", 30.0))
        self.notify_interval = max(0.0, _number(tp, "notify_interval_seconds", 3600.0))
        self._last_keepalive = -math.inf
        self._last_renewal = -math.inf
        self._failures = 0
        self._last_alert: dict[str, float] = {}

    # --- lifecycle ------------------------------------------------------------------

    async def run(self) -> None:
        await asyncio.gather(self.run_keepalive(), self.run_session())

    async def run_keepalive(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - keep maintaining
                logger.warning("maintenance_tick_failed", error=_short(e))

    async def run_session(self) -> None:
        if not self.session.can_auto_login(self.cfg):
            if self._teleport_configured():
                logger.info("teleport_manual_login_mode",
                            reason="no credentials or teleport.auto_renew is off")
                await self._run_manual()
            else:
                logger.info("teleport_session_disabled", reason="teleport.proxy or user not set")
            return
        backoff = self.retry_seconds
        while True:
            try:
                delay = await self.session_step()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - keep renewing
                self._failures += 1
                logger.warning("teleport_renewal_failed", error=_short(e), failures=self._failures)
                await self._alert_if_unusable(e)
                delay, backoff = backoff, min(backoff * 2, _MAX_BACKOFF_SECONDS)
            else:
                self._failures = 0
                if delay is not None:
                    backoff = self.retry_seconds
                else:            # stopped: wait for a manual login
                    delay = _BLOCKED_POLL_SECONDS
            await self._sleep(delay)

    def _teleport_configured(self) -> bool:
        tp = getattr(self.cfg, "teleport", None)
        return all(isinstance(getattr(tp, name, None), str) and getattr(tp, name) for name in ("proxy", "user"))

    async def _run_manual(self) -> None:
        """No automatic login: ask for a manual one before the session runs out."""
        while True:
            snap = self.session.current()
            if snap is None or snap.valid_until is None or snap.seconds_left() < self.renew_before:
                await self._send_alert(
                    "manual",
                    "⚠️ db-gateway Teleport 需要人工登录",
                    "没有配置自动登录（或 teleport.auto_renew 已关闭），会话"
                    + ("不可用" if snap is None or not snap.usable() else "即将到期")
                    + "。请用下面的一次性链接登录。",
                    link=True,
                )
            else:
                self._last_alert.pop("manual", None)
            await self._sleep(_BLOCKED_POLL_SECONDS)

    # --- one step each ----------------------------------------------------------------

    async def tick(self, now: Optional[float] = None) -> None:
        """Keepalive pass when due."""
        now = time.monotonic() if now is None else now
        if self.keepalive_seconds > 0 and now - self._last_keepalive >= self.keepalive_seconds:
            self._last_keepalive = now
            # A connection verified just after a pass is probed at the next one,
            # so none goes unverified past keepalive + interval seconds.
            older_than = max(0.0, self.keepalive_seconds - self.interval)
            await self.manager.keepalive(older_than=older_than, probe_timeout=self.probe_timeout)

    async def session_step(self) -> Optional[float]:
        """Renew when due; return seconds until the next look (None while stopped).

        Raises the renewal error; ``run_session`` retries with backoff.
        """
        blocked = self.session.blocked
        if blocked:
            await self._send_alert(
                "blocked",
                "⚠️ db-gateway Teleport 自动登录已停止",
                f"原因：{blocked}\n为避免账号被锁，自动登录已暂停，"
                "请用下面的一次性链接手动登录一次（成功后自动恢复续期）。",
                link=True,
            )
            return None
        self._last_alert.pop("blocked", None)   # a manual login lifted it: alert a new stop at once
        snap = self.session.current()
        wait = self._next_look(snap)
        if wait is not None:
            return wait
        # Skipped inside if an on-demand or manual login renewed while this waited.
        await self.session.renew_if_unchanged(
            self.cfg, seen=snap, reason="startup" if snap is None else "scheduled",
        )
        self._last_renewal = time.monotonic()
        self._last_alert.clear()       # healthy again: the next problem alerts at once
        return self._next_look(self.session.current()) or self.retry_seconds

    def _next_look(self, snap: Any) -> Optional[float]:
        """Seconds until the renewal is due, or None when it is due now."""
        if snap is None or snap.valid_until is None:
            return None
        due = snap.seconds_left() - self.renew_before
        if due <= 0 and snap.usable():
            gap = _MIN_RENEW_GAP_SECONDS - (time.monotonic() - self._last_renewal)
            if gap > 0:
                due = gap
        return min(due, _MAX_SLEEP_SECONDS) if due > 0 else None

    # --- alerts -------------------------------------------------------------------------

    async def _alert_if_unusable(self, error: BaseException) -> None:
        """A failed renewal matters to people once the session can no longer serve."""
        if self.session.blocked:
            return                       # the next step sends the manual-login alert
        if self._failures < _ALERT_AFTER_FAILURES:
            return                       # may be a blip: retrying quietly
        snap = self.session.current()
        if snap is not None and snap.usable():
            return                       # still valid: retrying quietly
        await self._send_alert(
            "failing",
            "⚠️ db-gateway Teleport 续期失败",
            f"错误：{_short(error)}\n会话已不可用，正在按退避自动重试；"
            "请检查 Teleport 集群与网络是否可达。",
            link=False,
        )

    async def _send_alert(self, key: str, title: str, message: str, *, link: bool) -> None:
        now = time.monotonic()
        last = self._last_alert.get(key)
        if last is not None and now - last < self.notify_interval:
            return
        self._last_alert[key] = now
        logger.warning("teleport_alert", kind=key)
        if self.alert is None:
            return
        try:
            await self.alert(title, message, link)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - alerting must not stop renewal
            logger.warning("teleport_alert_failed", error=_short(e))

    async def _sleep(self, seconds: float) -> None:      # seam for tests
        await asyncio.sleep(seconds)
