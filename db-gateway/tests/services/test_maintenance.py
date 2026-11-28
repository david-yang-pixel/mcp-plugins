"""Background maintenance: Teleport session renewal and pool keepalive, off the request path."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from db_gateway.config import AppConfig
from db_gateway.maintenance import Maintenance
from db_gateway.teleport_session import LoginFailed, LoginRejected, SessionSnapshot


class FakeManager:
    def __init__(self, events: list[str], fail_first: bool = False) -> None:
        self.events = events
        self.fail_first = fail_first

    async def keepalive(self, *, older_than: float, probe_timeout: float) -> None:
        self.events.append(f"keepalive:{older_than:g}:{probe_timeout:g}")
        if self.fail_first:
            self.fail_first = False
            raise RuntimeError("boom")


def snapshot(generation: int = 1, minutes_left: float = 55) -> SessionSnapshot:
    return SessionSnapshot(
        generation=generation, root=Path(f"/sessions/gen-{generation}"),
        valid_until=datetime.now(timezone.utc) + timedelta(minutes=minutes_left), roles=(),
    )


class FakeSession:
    """The parts of TeleportSessionManager the maintenance loop uses."""

    def __init__(self, *, auto: bool = True, snap: SessionSnapshot | None = None) -> None:
        self.auto = auto
        self.snap = snap
        self.blocked: str | None = None
        self.renewals: list[str] = []
        self.results: list[object] = []      # queued outcomes: a snapshot or an exception

    def can_auto_login(self, cfg) -> bool:
        return self.auto

    def current(self):
        return self.snap

    async def renew(self, cfg, **kwargs):
        raise AssertionError("background renewals must use renew_if_unchanged")

    async def renew_if_unchanged(self, cfg, *, seen, reason: str):
        assert seen is self.snap, "the loop passes the snapshot it judged due"
        self.renewals.append(reason)
        result = self.results.pop(0) if self.results else snapshot(len(self.renewals))
        if isinstance(result, BaseException):
            if isinstance(result, LoginRejected) and result.kind in ("locked", "blocked"):
                self.blocked = str(result)
            raise result
        self.snap = result
        return result


def _cfg(teleport=None, **postgres) -> AppConfig:
    pg = {"driver": "pool", "pool_keepalive_seconds": 30, "pool_maintenance_interval_seconds": 10,
          "pool_probe_timeout_seconds": 5}
    pg.update(postgres)
    tp = {"renew_before_seconds": 480, "renew_retry_seconds": 30, "notify_interval_seconds": 3600}
    tp.update(teleport or {})
    return AppConfig(teleport=tp, postgres=pg)


def _maintenance(session=None, alerts=None, events=None, teleport=None, **postgres) -> Maintenance:
    async def alert(title, message, link):
        alerts.append((title, message, link))

    return Maintenance(_cfg(teleport, **postgres), manager=FakeManager([] if events is None else events),
                       session=session or FakeSession(), alert=alert if alerts is not None else None)


# --- session renewal --------------------------------------------------------------------


async def test_first_login_runs_at_once_and_the_next_one_before_expiry():
    session = FakeSession()
    m = _maintenance(session)

    wait = await m.session_step()

    assert session.renewals == ["startup"]
    assert wait == 300                      # re-check at least every 5 minutes
    session.snap = snapshot(1, minutes_left=10)
    assert 115 <= await m.session_step() <= 121     # renew 480 s before expiry
    assert session.renewals == ["startup"]


async def test_due_session_is_renewed_on_schedule():
    session = FakeSession(snap=snapshot(1, minutes_left=7))   # inside renew_before
    m = _maintenance(session)
    await m.session_step()
    assert session.renewals == ["scheduled"]


async def test_short_lived_certificates_do_not_cause_back_to_back_logins():
    session = FakeSession()
    session.results = [snapshot(1, minutes_left=5)]        # shorter than renew_before
    m = _maintenance(session)

    wait = await m.session_step()

    assert 115 <= wait <= 120                # not a login every second
    assert await m.session_step() <= 120 and session.renewals == ["startup"]


async def test_transient_failures_back_off_exponentially_up_to_five_minutes():
    session = FakeSession()
    session.results = [LoginFailed("dial tcp: i/o timeout")] * 6
    m = _maintenance(session, alerts=[])
    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 6:
            raise asyncio.CancelledError

    m._sleep = fake_sleep
    with pytest.raises(asyncio.CancelledError):
        await m.run_session()
    assert sleeps == [30, 60, 120, 240, 300, 300]


async def test_success_resets_the_backoff():
    session = FakeSession()
    session.results = [LoginFailed("timeout"), LoginFailed("timeout"), snapshot(1), LoginFailed("timeout")]
    m = _maintenance(session)
    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 3:
            session.snap = snapshot(1, minutes_left=1)     # due again
        if len(sleeps) == 4:
            raise asyncio.CancelledError

    m._sleep = fake_sleep
    with pytest.raises(asyncio.CancelledError):
        await m.run_session()
    assert sleeps[:2] == [30, 60] and sleeps[3] == 30


async def test_without_credentials_nothing_is_attempted():
    session = FakeSession(auto=False)
    m = _maintenance(session)
    await asyncio.wait_for(m.run_session(), timeout=1)     # returns at once
    assert session.renewals == []


async def test_stopped_auto_login_sends_one_throttled_alert_with_a_link():
    session = FakeSession()
    session.results = [LoginRejected("invalid credentials (2x)", kind="blocked")]
    alerts: list = []
    m = _maintenance(session, alerts=alerts)

    with pytest.raises(LoginRejected):
        await m.session_step()
    assert await m.session_step() is None      # stopped: waits for a manual login
    assert await m.session_step() is None
    assert session.renewals == ["startup"]     # never another automatic attempt

    assert len(alerts) == 1
    title, message, link = alerts[0]
    assert link is True and "自动登录已停止" in title and "invalid credentials" in message

    session.blocked = None                     # a manual login on /teleport-auth succeeded
    session.snap = snapshot(2)
    assert await m.session_step() == 300


async def test_a_new_stop_after_a_manual_login_alerts_at_once():
    session = FakeSession()
    session.blocked = "连续 2 次被拒绝"
    alerts: list = []
    m = _maintenance(session, alerts=alerts)
    await m.session_step()
    session.blocked = None                     # manual login
    session.snap = snapshot(2)
    await m.session_step()
    session.blocked = "账号被锁定"               # stopped again within the hour
    await m.session_step()
    assert len(alerts) == 2


async def test_without_auto_login_a_configured_gateway_asks_for_manual_logins():
    session = FakeSession(auto=False)
    alerts: list = []
    m = _maintenance(session, alerts=alerts, teleport={"proxy": "teleport.example:443", "user": "u",
                                                       "auto_renew": False})
    steps: list[int] = []

    async def fake_sleep(seconds):
        steps.append(len(alerts))
        if len(steps) == 1:
            session.snap = snapshot(1, minutes_left=55)      # a manual login happened
        if len(steps) == 2:
            session.snap = snapshot(1, minutes_left=5)       # nearly expired again
        if len(steps) == 3:
            raise asyncio.CancelledError

    m._sleep = fake_sleep
    with pytest.raises(asyncio.CancelledError):
        await m.run_session()
    assert steps == [1, 1, 2] and session.renewals == []
    assert all(link for _, _, link in alerts) and "人工登录" in alerts[0][0]


async def test_failure_alert_only_once_the_session_is_unusable():
    session = FakeSession(snap=snapshot(1, minutes_left=6))
    alerts: list = []
    m = _maintenance(session, alerts=alerts)
    m._failures = 3

    await m._alert_if_unusable(LoginFailed("timeout"))
    assert alerts == []                        # still valid: retry quietly

    session.snap = snapshot(1, minutes_left=0.5)
    await m._alert_if_unusable(LoginFailed("timeout"))
    await m._alert_if_unusable(LoginFailed("timeout"))
    assert len(alerts) == 1 and alerts[0][2] is False and "续期失败" in alerts[0][0]


async def test_a_startup_blip_does_not_alert_until_three_failures_in_a_row():
    session = FakeSession()
    session.results = [LoginFailed("timeout"), LoginFailed("timeout"), LoginFailed("timeout")]
    alerts: list = []
    m = _maintenance(session, alerts=alerts)
    seen: list[int] = []

    async def fake_sleep(seconds):
        seen.append(len(alerts))
        if len(seen) == 3:
            raise asyncio.CancelledError

    m._sleep = fake_sleep
    with pytest.raises(asyncio.CancelledError):
        await m.run_session()
    assert seen == [0, 0, 1]          # alerted after the third failure in a row


async def test_alert_errors_do_not_stop_renewal():
    session = FakeSession()
    session.blocked = "locked"

    async def broken(*args):
        raise RuntimeError("feishu down")

    m = Maintenance(_cfg(), manager=FakeManager([]), session=session, alert=broken)
    assert await m.session_step() is None


async def test_unexpected_errors_are_retried():
    session = FakeSession()
    session.results = [RuntimeError("bug"), snapshot(1)]
    m = _maintenance(session)
    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise asyncio.CancelledError

    m._sleep = fake_sleep
    with pytest.raises(asyncio.CancelledError):
        await m.run_session()
    assert session.renewals == ["startup", "startup"] and sleeps[0] == 30


# --- keepalive ----------------------------------------------------------------------------


async def test_keepalive_runs_every_keepalive_seconds():
    events: list[str] = []
    m = _maintenance(events=events)

    for now in (0, 10, 20, 30, 40, 50, 60):
        await m.tick(now=now)

    # Probe what was not used or verified for keepalive - interval seconds, so no
    # connection reaches the 60 s checkout-probe threshold between passes.
    assert events == ["keepalive:20:5", "keepalive:20:5", "keepalive:20:5"]


async def test_keepalive_can_be_disabled():
    events: list[str] = []
    m = _maintenance(events=events, pool_keepalive_seconds=0)
    for now in (0, 100, 200):
        await m.tick(now=now)
    assert events == []


async def test_run_keeps_both_loops_going_after_errors():
    events: list[str] = []
    session = FakeSession()
    session.results = [RuntimeError("bug")]
    m = Maintenance(_cfg(pool_keepalive_seconds=0.01), manager=FakeManager(events, fail_first=True),
                    session=session)
    m.interval = 0.01   # below the configurable floor, to keep the test fast
    m.retry_seconds = 0.01

    task = asyncio.create_task(m.run())
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len([e for e in events if e.startswith("keepalive")]) >= 2
    assert session.renewals[:2] == ["startup", "startup"]


async def test_bad_settings_fall_back_to_defaults():
    m = Maintenance(MagicMock(), manager=FakeManager([]), session=FakeSession(auto=False))
    assert (m.interval, m.keepalive_seconds, m.probe_timeout) == (10, 30, 5)
    assert (m.renew_before, m.retry_seconds, m.notify_interval) == (480, 30, 3600)
    await m.tick(now=0)


def test_settings_have_safe_floors():
    m = Maintenance(_cfg(pool_maintenance_interval_seconds=0, pool_probe_timeout_seconds=0.05),
                    manager=FakeManager([]), session=FakeSession())
    assert m.interval == 1.0         # never a busy loop
    assert m.probe_timeout == 1.0    # above the ~0.2 s RTT


def test_magicmock_config_never_counts_as_credentials():
    from db_gateway.teleport_session import SESSION

    assert SESSION.can_auto_login(MagicMock()) is False
