"""DB gateway core behavior."""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from db_gateway.postgres import PsqlResult, TeleportAuthExpired


def _cfg(tmp_path):
    cfg = MagicMock()
    cfg.postgres.host = "localhost"
    cfg.postgres.port = 56061
    cfg.postgres.user = "read"
    cfg.postgres.password = "pw"
    cfg.postgres.db_prod = "langgenius"
    cfg.postgres.db_billing = "billing"
    cfg.postgres.db_plugin = "dify_plugin_daemon"
    cfg.postgres.port_prod = 56061
    cfg.postgres.port_billing = 56062
    cfg.postgres.port_plugin = 56063
    cfg.postgres.query_timeout_seconds = 30
    cfg.postgres.max_rows = 1000
    cfg.postgres.max_concurrent_queries = 3
    cfg.postgres.teleport_retry_attempts = 1
    cfg.postgres.teleport_retry_delay_seconds = 0
    cfg.postgres.singleflight_enabled = True
    cfg.resources.base_dir = str(tmp_path)
    cfg.resources.state_dir = ".state"
    return cfg


@pytest.mark.asyncio
async def test_gateway_query_success(tmp_path):
    from db_gateway.core import execute_gateway_query

    fake_result = PsqlResult(
        csv="id,name\n1,alice\n",
        row_count=1,
        elapsed_ms=50,
        truncated=False,
    )
    with patch(
        "db_gateway.core.run_psql",
        AsyncMock(return_value=fake_result),
    ):
        result = await execute_gateway_query(
            cfg=_cfg(tmp_path),
            database="prod",
            sql="SELECT id, name FROM tenants LIMIT 1",
            ticket_id="12345",
        )

    assert result.success is True
    assert result.csv == "id,name\n1,alice\n"
    assert result.row_count == 1


@pytest.mark.asyncio
async def test_gateway_rejects_write_without_psql(tmp_path):
    from db_gateway.core import execute_gateway_query

    run = AsyncMock()
    with patch("db_gateway.core.run_psql", run):
        result = await execute_gateway_query(
            cfg=_cfg(tmp_path),
            database="prod",
            sql="DELETE FROM tenants WHERE id=1",
            ticket_id="12345",
        )

    assert result.success is False
    assert result.code == "SQL_SAFETY_ERROR"
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_gateway_teleport_expired_has_explicit_code(tmp_path):
    from db_gateway.core import execute_gateway_query

    with patch(
        "db_gateway.core.run_psql",
        AsyncMock(side_effect=TeleportAuthExpired("conn refused")),
    ):
        result = await execute_gateway_query(
            cfg=_cfg(tmp_path),
            database="prod",
            sql="SELECT 1",
            ticket_id="12345",
        )

    assert result.success is False
    assert result.code == "TELEPORT_EXPIRED"
    assert "conn refused" in (result.error or "")


@pytest.mark.asyncio
async def test_gateway_retries_transient_teleport_error(tmp_path):
    from db_gateway.core import execute_gateway_query

    fake_result = PsqlResult(
        csv="ok\n1\n",
        row_count=1,
        elapsed_ms=50,
        truncated=False,
    )
    run = AsyncMock(side_effect=[TeleportAuthExpired("conn refused"), fake_result])
    cfg = _cfg(tmp_path)
    cfg.postgres.teleport_retry_attempts = 1

    with patch("db_gateway.core.run_psql", run):
        result = await execute_gateway_query(
            cfg=cfg,
            database="prod",
            sql="SELECT 1 AS ok",
            ticket_id="12345",
        )

    assert result.success is True
    assert result.csv == "ok\n1\n"
    assert run.await_count == 2


@pytest.mark.asyncio
async def test_gateway_singleflights_identical_concurrent_queries(tmp_path):
    from db_gateway.core import execute_gateway_query

    fake_result = PsqlResult(
        csv="ok\n1\n",
        row_count=1,
        elapsed_ms=50,
        truncated=False,
    )
    async def fake_run_psql(**kwargs):
        await asyncio.sleep(0.05)
        return fake_result

    run = AsyncMock(side_effect=fake_run_psql)
    cfg = _cfg(tmp_path)
    cfg.postgres.singleflight_enabled = True

    with patch("db_gateway.core.run_psql", run):
        first, second = await asyncio.gather(
            execute_gateway_query(cfg=cfg, database="prod", sql="SELECT 1 AS ok", ticket_id="a"),
            execute_gateway_query(cfg=cfg, database="prod", sql="SELECT 1 AS ok", ticket_id="b"),
        )

    assert first.success is True
    assert second.success is True
    assert run.await_count == 1


@pytest.mark.asyncio
async def test_gateway_singleflight_waiter_cancel_does_not_cancel_shared_query(tmp_path):
    from db_gateway.core import execute_gateway_query

    fake_result = PsqlResult(
        csv="ok\n1\n",
        row_count=1,
        elapsed_ms=50,
        truncated=False,
    )

    async def fake_run_psql(**kwargs):
        await asyncio.sleep(0.05)
        return fake_result

    run = AsyncMock(side_effect=fake_run_psql)
    cfg = _cfg(tmp_path)
    cfg.postgres.singleflight_enabled = True

    with patch("db_gateway.core.run_psql", run):
        first = asyncio.create_task(
            execute_gateway_query(
                cfg=cfg,
                database="prod",
                sql="SELECT 1 AS ok",
                ticket_id="a",
            )
        )
        await asyncio.sleep(0.01)
        second = asyncio.create_task(
            execute_gateway_query(
                cfg=cfg,
                database="prod",
                sql="SELECT 1 AS ok",
                ticket_id="b",
            )
        )
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        result = await second

    assert result.success is True
    assert run.await_count == 1


@pytest.mark.asyncio
async def test_gateway_limits_database_query_concurrency(tmp_path):
    from db_gateway.core import execute_gateway_query

    active = 0
    max_active = 0
    lock = asyncio.Lock()
    fake_result = PsqlResult(
        csv="ok\n1\n",
        row_count=1,
        elapsed_ms=50,
        truncated=False,
    )

    async def fake_run_psql(**kwargs):
        nonlocal active, max_active
        async with lock:
            active += 1
            max_active = max(max_active, active)
        await asyncio.sleep(0.05)
        async with lock:
            active -= 1
        return fake_result

    cfg = _cfg(tmp_path)
    cfg.postgres.max_concurrent_queries = 2
    cfg.postgres.singleflight_enabled = False

    with patch("db_gateway.core.run_psql", AsyncMock(side_effect=fake_run_psql)):
        results = await asyncio.gather(
            *[
                execute_gateway_query(
                    cfg=cfg,
                    database="prod",
                    sql=f"SELECT {i} AS ok",
                    ticket_id=str(i),
                )
                for i in range(5)
            ]
        )

    assert all(result.success for result in results)
    assert max_active <= 2
