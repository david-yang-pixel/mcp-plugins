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
