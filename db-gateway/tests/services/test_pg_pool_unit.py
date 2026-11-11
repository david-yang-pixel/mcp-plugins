"""Pool manager behaviour that needs no database."""
import os
import time

from db_gateway.pg_pool import keys_signature, pool_settings_from_config


def test_keys_signature_tracks_changes(tmp_path):
    assert keys_signature(str(tmp_path)) is None  # no keys/ dir yet
    keys = tmp_path / "keys" / "cluster"
    keys.mkdir(parents=True)
    cert = keys / "user-x509.pem"
    cert.write_text("a")
    first = keys_signature(str(tmp_path))
    assert first == keys_signature(str(tmp_path))

    os.utime(cert, (time.time() + 10, time.time() + 10))
    assert keys_signature(str(tmp_path)) != first
    second = keys_signature(str(tmp_path))
    cert.unlink()  # `tsh logout --all` removes the keys
    assert keys_signature(str(tmp_path)) != second


def test_pool_settings_follow_concurrency_limit():
    class PG:
        max_concurrent_queries = 2
        pool_min_size = 5
        pool_max_idle_seconds = 100
        pool_max_lifetime_seconds = 900
        pool_check_idle_seconds = 30
        pool_acquire_timeout_seconds = 7

    class Cfg:
        postgres = PG()

    settings = pool_settings_from_config(Cfg())
    assert settings.max_size == 2
    assert settings.min_size == 2  # never above max_size
    assert settings.max_idle_seconds == 100
    assert settings.acquire_timeout_seconds == 7


async def test_key_change_closes_pools_outside_the_lock(tmp_path, monkeypatch):
    """Bugbot: closing pools under the manager lock stalled other databases."""
    import asyncio

    from db_gateway import pg_pool
    from db_gateway.pg_pool import PoolKey, PoolManager, PoolSettings

    keys = tmp_path / "keys"
    keys.mkdir()
    cert = keys / "cert.pem"
    cert.write_text("a")
    settings = PoolSettings(min_size=0)  # opening makes no connection
    manager = PoolManager()

    async def slow_close(_pool):
        await asyncio.sleep(1.0)

    monkeypatch.setattr(pg_pool, "_close_quietly", slow_close)
    prod = PoolKey("127.0.0.1", 1, "u", "prod")
    billing = PoolKey("127.0.0.1", 2, "u", "billing")
    await manager.pool_for(prod, password="", settings=settings, auth_home=str(tmp_path))
    os.utime(cert, (time.time() + 10, time.time() + 10))

    rotating = asyncio.create_task(
        manager.pool_for(prod, password="", settings=settings, auth_home=str(tmp_path))
    )
    await asyncio.sleep(0.05)  # the rotating lookup is now closing the old pool
    started = time.monotonic()
    await manager.pool_for(billing, password="", settings=settings, auth_home=str(tmp_path))
    elapsed = time.monotonic() - started
    await rotating

    assert elapsed < 0.5
    for pool in manager._pools.values():
        await pool.close()


async def test_failed_pool_open_does_not_close_a_newer_pool(monkeypatch):
    """Bugbot: a pool_for failure drained with pool=None and closed the current pool."""
    import pytest
    from psycopg_pool import PoolTimeout

    from db_gateway.pg_pool import PoolKey, PoolManager, PoolSettings, run_pool_query
    from db_gateway.postgres import TeleportAuthExpired

    manager = PoolManager()
    settings = PoolSettings(min_size=0)  # opening makes no connection
    key = PoolKey("127.0.0.1", 1, "u", "db")
    newer = await manager.pool_for(key, password="", settings=settings, auth_home=None)

    async def failing_pool_for(*_args, **_kwargs):
        # Another query rebuilt the pool while this one's open attempt failed.
        raise PoolTimeout("initial connections failed")

    monkeypatch.setattr(manager, "pool_for", failing_pool_for)
    with pytest.raises(TeleportAuthExpired):
        await run_pool_query(
            manager=manager, settings=settings, host="127.0.0.1", port=1, user="u",
            password="", database="db", sql="SELECT 1", timeout=5, max_rows=10,
        )

    assert manager._pools[key] is newer
    assert not newer.closed
    await newer.close()


class _Diag:
    def __init__(self, **fields):
        self.message_primary = fields.get("primary")
        self.message_detail = fields.get("detail")
        self.message_hint = fields.get("hint")
        self.statement_position = fields.get("position")


class _FakePgError(Exception):
    def __init__(self, **fields):
        super().__init__(fields.get("primary") or "")
        self.diag = _Diag(**fields)


def test_error_text_includes_hint_and_position():
    from db_gateway.pg_pool import _error_text

    err = _FakePgError(
        primary='column "stauts" does not exist',
        hint='Perhaps you meant to reference the column "workflow_runs.status".',
        position="35",
    )
    text = _error_text(err)
    assert text.splitlines() == [
        'ERROR:  column "stauts" does not exist',
        'HINT:  Perhaps you meant to reference the column "workflow_runs.status".',
        "POSITION:  35",
    ]


def test_error_text_maps_position_into_the_callers_sql():
    from db_gateway.pg_pool import _error_text, _remember_sql

    body = "SELECT id\nFROM workflow_runs WHERE tenant_id = 'x'::text"
    sent = "BEGIN;\nDECLARE c CURSOR FOR SELECT * FROM (\n" + body + "\n) AS q"
    start = sent.index(body)
    position = start + body.index("= 'x'") + 1  # server positions are 1-based
    err = _FakePgError(
        primary="operator does not exist: uuid = text",
        hint="No operator matches the given name and argument types. You might need to add explicit type casts.",
        detail="some detail",
        position=str(position),
    )
    _remember_sql(err, body, start)
    lines = _error_text(err).splitlines()
    assert lines[0] == "ERROR:  operator does not exist: uuid = text"
    assert lines[1] == "DETAIL:  some detail"
    assert lines[2].startswith("HINT:  No operator matches")
    assert lines[3] == "POSITION:  46 (line 2, column 36 of the submitted SQL)"
    assert lines[4] == "LINE 2: FROM workflow_runs WHERE tenant_id = 'x'::text"
    assert lines[5].index("^") == len("LINE 2: ") + 35


def test_error_text_drops_positions_outside_the_callers_sql():
    from db_gateway.pg_pool import _error_text, _remember_sql

    err = _FakePgError(primary="syntax error", position="3")
    _remember_sql(err, "SELECT 1", 40)
    assert _error_text(err) == "ERROR:  syntax error"


def test_error_text_without_diag_falls_back_to_message():
    from db_gateway.pg_pool import _error_text

    assert _error_text(RuntimeError("boom")) == "ERROR:  boom"  # type: ignore[arg-type]
