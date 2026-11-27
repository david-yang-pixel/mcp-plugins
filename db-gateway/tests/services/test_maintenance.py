"""Background maintenance: Teleport renewal handling and pool keepalive, off the request path."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from db_gateway.config import AppConfig
from db_gateway.maintenance import Maintenance


class FakeManager:
    def __init__(self, events: list[str], fail_first: bool = False) -> None:
        self.events = events
        self.fail_first = fail_first

    async def keepalive(self, *, older_than: float, probe_timeout: float) -> None:
        self.events.append(f"keepalive:{older_than:g}:{probe_timeout:g}")
        if self.fail_first:
            self.fail_first = False
            raise RuntimeError("boom")


def _profile(home: Path, cert: str) -> None:
    """current-profile + user cert, as tsh-auth-watchdog leaves them."""
    keys = home / "keys" / "teleport.example.com"
    keys.mkdir(parents=True, exist_ok=True)
    (home / "current-profile").write_text("teleport.example.com")
    (keys / "alice.crt").write_text(cert)


def _cfg(home: Path, **postgres) -> AppConfig:
    pg = {"driver": "pool", "pool_keepalive_seconds": 30, "pool_maintenance_interval_seconds": 10,
          "pool_probe_timeout_seconds": 5}
    pg.update(postgres)
    return AppConfig(teleport={"auth_home": str(home)}, postgres=pg)


def _maintenance(home: Path, events: list[str], **postgres) -> Maintenance:
    async def refresh(cfg, deadline):
        events.append("refresh_daemons")

    return Maintenance(_cfg(home, **postgres), manager=FakeManager(events), refresh_daemons=refresh)


async def test_renewal_restarts_daemons_then_verifies_every_pooled_connection(tmp_path):
    events: list[str] = []
    _profile(tmp_path, "cert-1")
    m = _maintenance(tmp_path, events, pool_keepalive_seconds=0)

    await m.tick(now=0)                 # first look: remember the cert
    _profile(tmp_path, "cert-2")        # watchdog renewed the session
    await m.tick(now=10)

    # Pools at once (not behind a slow AWS proxy restart), then the daemons, then
    # the pools again (a restarted tunnel drops its connections).
    assert events == ["keepalive:0:5", "refresh_daemons", "keepalive:0:5"]

    events.clear()
    for now in (20, 50, 90, 100, 110):  # 90 s grace: tunnels outside this process may restart late
        await m.tick(now=now)
    assert events == ["keepalive:0:5"] * 4   # 20, 50, 90, 100 are inside the grace window

    events.clear()
    await m.tick(now=130)               # same cert, grace over, keepalive disabled
    assert events == []


async def test_db_cert_written_by_a_tunnel_is_not_a_renewal(tmp_path):
    events: list[str] = []
    _profile(tmp_path, "cert-1")
    m = _maintenance(tmp_path, events, pool_keepalive_seconds=0)
    await m.tick(now=0)

    db = tmp_path / "keys" / "teleport.example.com" / "alice-db" / "teleport.example.com"
    db.mkdir(parents=True)
    (db / "lg-billing-01.crt").write_text("db cert")   # tsh-proxy-db re-issued its cert
    await m.tick(now=10)

    assert events == []


async def test_renewal_window_is_ignored_until_the_new_cert_appears(tmp_path):
    events: list[str] = []
    _profile(tmp_path, "cert-1")
    m = _maintenance(tmp_path, events, pool_keepalive_seconds=0)
    await m.tick(now=0)

    (tmp_path / "current-profile").unlink()   # watchdog purged the profile, login running
    await m.tick(now=10)
    assert events == []

    _profile(tmp_path, "cert-2")
    await m.tick(now=20)
    assert events == ["keepalive:0:5", "refresh_daemons", "keepalive:0:5"]


async def test_keepalive_runs_every_keepalive_seconds(tmp_path):
    events: list[str] = []
    m = _maintenance(tmp_path, events)      # not logged in: no renewal handling

    for now in (0, 10, 20, 30, 40, 50, 60):
        await m.tick(now=now)

    # Probe what was not used or verified for keepalive - interval seconds, so no
    # connection reaches the 60 s checkout-probe threshold between passes.
    assert events == ["keepalive:20:5", "keepalive:20:5", "keepalive:20:5"]


async def test_keepalive_and_key_watching_can_be_disabled(tmp_path):
    events: list[str] = []
    _profile(tmp_path, "cert-1")
    m = _maintenance(tmp_path, events, pool_keepalive_seconds=0, pool_watch_teleport_keys=False)
    await m.tick(now=0)
    _profile(tmp_path, "cert-2")
    await m.tick(now=100)

    assert events == []


async def test_run_keeps_going_after_an_error(tmp_path):
    events: list[str] = []
    cfg = _cfg(tmp_path, pool_keepalive_seconds=0.01)
    m = Maintenance(cfg, manager=FakeManager(events, fail_first=True))
    m.interval = 0.01   # below the configurable floor, to keep the test fast

    task = asyncio.create_task(m.run())
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len([e for e in events if e.startswith("keepalive")]) >= 2


async def test_bad_settings_fall_back_to_defaults():
    from unittest.mock import MagicMock

    m = Maintenance(MagicMock(), manager=FakeManager([]))
    assert m.interval == 10 and m.keepalive_seconds == 30 and m.probe_timeout == 5
    await m.tick(now=0)                 # no usable auth_home: no key watching, no crash


def test_settings_have_safe_floors(tmp_path):
    m = Maintenance(_cfg(tmp_path, pool_maintenance_interval_seconds=0, pool_probe_timeout_seconds=0.05),
                    manager=FakeManager([]))
    assert m.interval == 1.0         # never a busy loop
    assert m.probe_timeout == 1.0    # above the ~0.2 s RTT
