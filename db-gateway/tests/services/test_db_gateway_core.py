"""DB gateway core behavior."""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from db_gateway.postgres import PsqlResult, TeleportAuthExpired


@pytest.fixture(autouse=True)
def _isolate_index_hints():
    """No real catalog connection from unit tests; fresh index cache per test."""
    from db_gateway import core

    core._INDEX_CACHE.clear()
    core._INDEX_CACHE_FAILED_AT.clear()
    core._INDEX_CACHE_LOCKS.clear()
    with patch(
        "db_gateway.core.run_metadata_query",
        AsyncMock(side_effect=RuntimeError("catalog lookups are not mocked in this test")),
    ):
        yield
    core._INDEX_CACHE.clear()
    core._INDEX_CACHE_FAILED_AT.clear()
    core._INDEX_CACHE_LOCKS.clear()


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
            execute_gateway_query(cfg=cfg, database="prod", sql="SELECT 1 AS ok"),
            execute_gateway_query(cfg=cfg, database="prod", sql="SELECT 1 AS ok"),
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
            )
        )
        await asyncio.sleep(0.01)
        second = asyncio.create_task(
            execute_gateway_query(
                cfg=cfg,
                database="prod",
                sql="SELECT 1 AS ok",
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
                )
                for i in range(5)
            ]
        )

    assert all(result.success for result in results)
    assert max_active <= 2


# ---------------------------------------------------------------------------
# Pagination / total_rows / format / explain / timing
# ---------------------------------------------------------------------------

import json  # noqa: E402

from db_gateway import core as core_module  # noqa: E402


def _page_result(**overrides):
    data = dict(
        csv="id\n11\n12\n",
        row_count=2,
        elapsed_ms=40,
        truncated=True,
        columns=["id"],
        rows=[["11"], ["12"]],
        has_more=True,
        total_rows=23,
        offset_skipped=10,
        timing_ms={"psql_process_ms": 40.0, "query_ms": 2.0, "connect_and_overhead_ms": 37.0},
    )
    data.update(overrides)
    return PsqlResult(**data)


def _audit_entries(tmp_path):
    path = tmp_path / ".state" / "logs" / "db_gateway_audit.log"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.mark.asyncio
async def test_gateway_pagination_returns_total_and_next_offset(tmp_path):
    from db_gateway.core import execute_gateway_query

    run = AsyncMock(return_value=_page_result())
    with patch("db_gateway.core.run_psql", run):
        result = await execute_gateway_query(
            cfg=_cfg(tmp_path),
            database="prod",
            sql="SELECT id FROM tenants ORDER BY id;",
            offset=10,
            limit=2,
        )

    kwargs = run.await_args.kwargs
    assert kwargs["offset"] == 10
    assert kwargs["limit"] == 2
    assert kwargs["count_timeout"] == 5.0
    assert kwargs["describe"] is False
    assert kwargs["sql"] == "SELECT id FROM tenants ORDER BY id"
    payload = result.to_json()
    assert payload["success"] is True
    assert payload["total_rows"] == 23
    assert payload["has_more"] is True
    assert payload["truncated"] is True
    assert payload["next_offset"] == 12
    assert payload["offset"] == 10 and payload["limit"] == 2
    assert payload["warnings"] == []
    assert payload["timing_ms"]["queue_wait_ms"] >= 0
    assert payload["timing_ms"]["total_ms"] >= 0
    assert payload["timing_ms"]["query_ms"] == 2.0
    entry = _audit_entries(tmp_path)[-1]
    assert entry["status"] == "OK"
    assert entry["offset"] == 10 and entry["limit"] == 2
    assert entry["total_rows"] == 23
    assert "ticket_id" not in entry and "ticket_id_missing" not in entry
    assert "queue_wait_ms" in entry["timing_ms"]


@pytest.mark.asyncio
async def test_gateway_warns_when_paging_without_order_by(tmp_path):
    from db_gateway.core import execute_gateway_query

    with patch("db_gateway.core.run_psql", AsyncMock(return_value=_page_result())):
        result = await execute_gateway_query(
            cfg=_cfg(tmp_path),
            database="prod",
            sql="SELECT id FROM tenants",
            offset=10,
            limit=2,
        )

    assert any("ORDER BY" in w for w in result.warnings)


@pytest.mark.asyncio
async def test_gateway_last_page_has_no_next_offset_and_no_order_warning(tmp_path):
    from db_gateway.core import execute_gateway_query

    last = _page_result(has_more=False, truncated=False, total_rows=12, offset_skipped=0)
    with patch("db_gateway.core.run_psql", AsyncMock(return_value=last)):
        result = await execute_gateway_query(
            cfg=_cfg(tmp_path), database="prod", sql="SELECT id FROM t",
        )

    assert result.has_more is False
    assert result.next_offset is None
    assert result.total_rows == 12
    assert result.warnings == []


@pytest.mark.asyncio
async def test_gateway_null_total_is_explained(tmp_path):
    from db_gateway.core import execute_gateway_query

    page = _page_result(total_rows=None, total_rows_note="计数超时（超过 5s），总行数未知")
    with patch("db_gateway.core.run_psql", AsyncMock(return_value=page)):
        result = await execute_gateway_query(
            cfg=_cfg(tmp_path), database="prod", sql="SELECT id FROM t ORDER BY id", offset=10, limit=2,
        )

    assert result.total_rows is None
    assert any("total_rows 为 null" in w and "超时" in w for w in result.warnings)


@pytest.mark.asyncio
async def test_gateway_limit_is_clamped_to_max_rows(tmp_path):
    from db_gateway.core import execute_gateway_query

    cfg = _cfg(tmp_path)
    cfg.postgres.max_rows = 100
    run = AsyncMock(return_value=_page_result(has_more=False, truncated=False))
    with patch("db_gateway.core.run_psql", run):
        result = await execute_gateway_query(
            cfg=cfg, database="prod", sql="SELECT 1", limit="5000",
        )

    assert run.await_args.kwargs["limit"] == 100
    assert result.limit == 100
    assert any("max_rows=100" in w for w in result.warnings)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "fragment"),
    [
        ({"offset": -1}, "offset"),
        ({"offset": "abc"}, "offset"),
        ({"offset": True}, "offset"),
        ({"limit": 0}, "limit"),
        ({"limit": 1.5}, "limit"),
        ({"output_format": "xml"}, "format"),
    ],
)
async def test_gateway_rejects_invalid_arguments(tmp_path, kwargs, fragment):
    from db_gateway.core import execute_gateway_query

    run = AsyncMock()
    with patch("db_gateway.core.run_psql", run):
        result = await execute_gateway_query(
            cfg=_cfg(tmp_path), database="prod", sql="SELECT 1", **kwargs,
        )

    assert result.success is False
    assert result.code == "INVALID_ARGUMENT"
    assert fragment in (result.error or "")
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_gateway_json_format_returns_columns_types_and_rows(tmp_path):
    from db_gateway.core import execute_gateway_query

    page = _page_result(
        columns=["id", "note"],
        rows=[["1", None], ["2", ""]],
        column_types=["integer", "text"],
        row_count=2,
        has_more=False,
        truncated=False,
        total_rows=2,
        offset_skipped=0,
    )
    run = AsyncMock(return_value=page)
    with patch("db_gateway.core.run_psql", run):
        result = await execute_gateway_query(
            cfg=_cfg(tmp_path), database="prod", sql="SELECT id, note FROM t", output_format="JSON",
        )

    assert run.await_args.kwargs["describe"] is True
    payload = result.to_json()
    assert payload["format"] == "json"
    assert payload["csv"] == ""
    assert payload["columns"] == ["id", "note"]
    assert payload["column_types"] == ["integer", "text"]
    assert payload["rows"] == [["1", None], ["2", ""]]


@pytest.mark.asyncio
async def test_gateway_json_format_falls_back_to_csv_text(tmp_path):
    from db_gateway.core import execute_gateway_query

    plain = PsqlResult(csv='id,name\n1,"a,b"\n', row_count=1, elapsed_ms=5, truncated=False)
    with patch("db_gateway.core.run_psql", AsyncMock(return_value=plain)):
        result = await execute_gateway_query(
            cfg=_cfg(tmp_path), database="prod", sql="SELECT 1",
            output_format="json",
        )

    assert result.columns == ["id", "name"]
    assert result.rows == [["1", "a,b"]]


@pytest.mark.asyncio
async def test_gateway_csv_format_is_default_and_has_no_rows_key(tmp_path):
    from db_gateway.core import execute_gateway_query

    with patch("db_gateway.core.run_psql", AsyncMock(return_value=_page_result())):
        result = await execute_gateway_query(
            cfg=_cfg(tmp_path), database="prod", sql="SELECT id FROM t ORDER BY id",
        )

    payload = result.to_json()
    assert payload["format"] == "csv"
    assert payload["csv"] == "id\n11\n12\n"
    assert "rows" not in payload


@pytest.mark.asyncio
async def test_gateway_accepts_parenthesized_union(tmp_path):
    from db_gateway.core import execute_gateway_query

    run = AsyncMock(return_value=_page_result(has_more=False, truncated=False))
    with patch("db_gateway.core.run_psql", run):
        result = await execute_gateway_query(
            cfg=_cfg(tmp_path), database="prod",
            sql="(SELECT 1) UNION ALL (SELECT 2)",
        )

    assert result.success is True
    run.assert_awaited_once()


@pytest.mark.asyncio
async def test_gateway_sql_safety_rejection_has_reason_and_is_audited(tmp_path):
    from db_gateway.core import execute_gateway_query

    run = AsyncMock()
    with patch("db_gateway.core.run_psql", run):
        result = await execute_gateway_query(
            cfg=_cfg(tmp_path), database="prod", sql="EXPLAIN SELECT 1",
        )

    assert result.code == "SQL_SAFETY_ERROR"
    assert "SELECT 或 WITH" in (result.error or "") and "EXPLAIN" in (result.error or "")
    run.assert_not_awaited()
    assert _audit_entries(tmp_path)[-1]["status"] == "REJECTED_SQL_SAFETY"


@pytest.mark.asyncio
async def test_gateway_timeout_has_explicit_code(tmp_path):
    from db_gateway.core import execute_gateway_query

    with patch("db_gateway.core.run_psql", AsyncMock(side_effect=asyncio.TimeoutError())):
        result = await execute_gateway_query(
            cfg=_cfg(tmp_path), database="prod", sql="SELECT 1",
        )

    assert result.code == "QUERY_TIMEOUT"
    assert "超时" in (result.error or "")
    assert "total_ms" in result.timing_ms
    assert _audit_entries(tmp_path)[-1]["status"] == "TIMEOUT"


@pytest.mark.asyncio
async def test_gateway_retry_time_is_reported(tmp_path):
    from db_gateway.core import execute_gateway_query

    ok = _page_result(has_more=False, truncated=False)
    run = AsyncMock(side_effect=[TeleportAuthExpired("conn refused"), ok])
    with patch("db_gateway.core.run_psql", run):
        result = await execute_gateway_query(
            cfg=_cfg(tmp_path), database="prod", sql="SELECT 7",
        )

    assert result.success is True
    assert result.timing_ms["attempts"] == 2
    assert "teleport_retry_ms" in result.timing_ms


def _pool_cfg(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.postgres.driver = "pool"
    cfg.postgres.pool_min_size = 1
    cfg.postgres.pool_max_idle_seconds = 240
    cfg.postgres.pool_max_lifetime_seconds = 1800
    cfg.postgres.pool_check_idle_seconds = 60
    cfg.postgres.pool_acquire_timeout_seconds = 15
    cfg.postgres.pool_retry_delay_seconds = 0
    cfg.postgres.pool_watch_teleport_keys = True
    cfg.teleport.auth_home = str(tmp_path / "tsh")
    return cfg


@pytest.mark.asyncio
async def test_gateway_pool_driver_uses_pool_not_psql(tmp_path):
    from db_gateway.core import execute_gateway_query

    fake_result = PsqlResult(csv="ok\n1\n", row_count=1, elapsed_ms=5, truncated=False)
    pool_run = AsyncMock(return_value=fake_result)
    psql_run = AsyncMock()
    with patch("db_gateway.core.run_pool_query", pool_run), patch("db_gateway.core.run_psql", psql_run):
        result = await execute_gateway_query(
            cfg=_pool_cfg(tmp_path), database="billing", sql="SELECT 1 AS ok",
        )

    assert result.success is True
    psql_run.assert_not_awaited()
    kwargs = pool_run.await_args.kwargs
    assert kwargs["port"] == 56062
    assert kwargs["database"] == "billing"
    assert kwargs["auth_home"] == str(tmp_path / "tsh")
    assert kwargs["settings"].max_size == 3
    assert kwargs["settings"].max_idle_seconds == 240


@pytest.mark.asyncio
async def test_gateway_pool_driver_retries_after_connection_loss(tmp_path):
    from db_gateway.core import execute_gateway_query

    fake_result = PsqlResult(csv="ok\n1\n", row_count=1, elapsed_ms=5, truncated=False)
    pool_run = AsyncMock(side_effect=[TeleportAuthExpired("connection reset"), fake_result])
    cfg = _pool_cfg(tmp_path)
    cfg.postgres.pool_retry_delay_seconds = 0.01
    cfg.postgres.teleport_retry_delay_seconds = 99  # must not be used by the pool driver

    with patch("db_gateway.core.run_pool_query", pool_run):
        result = await asyncio.wait_for(
            execute_gateway_query(cfg=cfg, database="prod", sql="SELECT 1 AS ok"),
            timeout=5,
        )

    assert result.success is True
    assert pool_run.await_count == 2
    assert result.timing_ms["attempts"] == 2


@pytest.mark.asyncio
async def test_gateway_pool_driver_can_skip_key_watching(tmp_path):
    from db_gateway.core import execute_gateway_query

    fake_result = PsqlResult(csv="ok\n1\n", row_count=1, elapsed_ms=5, truncated=False)
    pool_run = AsyncMock(return_value=fake_result)
    cfg = _pool_cfg(tmp_path)
    cfg.postgres.pool_watch_teleport_keys = False
    with patch("db_gateway.core.run_pool_query", pool_run):
        await execute_gateway_query(cfg=cfg, database="prod", sql="SELECT 1 AS ok")

    assert pool_run.await_args.kwargs["auth_home"] is None


# ---------------------------------------------------------------------------
# ticket_id retired
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gateway_without_ticket_id_has_no_warnings(tmp_path):
    from db_gateway.core import execute_gateway_query

    cfg = _cfg(tmp_path)
    # Old configs may still carry the retired switches; they no longer do anything.
    cfg.db_gateway.require_ticket_id = True
    cfg.db_gateway.missing_ticket_alert_interval_seconds = 3600
    run = AsyncMock(return_value=_page_result(has_more=False, truncated=False))
    with patch("db_gateway.core.run_psql", run):
        result = await execute_gateway_query(cfg=cfg, database="prod", sql="SELECT 1")

    assert result.success is True
    assert result.warnings == []
    entry = _audit_entries(tmp_path)[-1]
    assert entry["status"] == "OK"
    assert "ticket_id" not in entry and "ticket_id_missing" not in entry


def test_gateway_query_no_longer_accepts_ticket_id_keyword():
    import inspect

    from db_gateway.core import execute_gateway_query

    assert "ticket_id" not in inspect.signature(execute_gateway_query).parameters


# ---------------------------------------------------------------------------
# explain=true
# ---------------------------------------------------------------------------


def _plan_result():
    return PsqlResult(
        csv="QUERY PLAN\nSeq Scan on t\n",
        row_count=1,
        elapsed_ms=3,
        truncated=False,
        columns=["QUERY PLAN"],
        rows=[["Seq Scan on t"]],
        column_types=["text"],
        has_more=False,
        total_rows=1,
        offset_skipped=0,
    )


@pytest.mark.asyncio
async def test_gateway_explain_passes_validated_body_to_driver(tmp_path):
    from db_gateway.core import execute_gateway_query

    run = AsyncMock(return_value=_plan_result())
    with patch("db_gateway.core.run_psql", run):
        result = await execute_gateway_query(
            cfg=_cfg(tmp_path), database="prod", sql="SELECT id FROM t WHERE x = 1;",
            explain=True,
        )

    kwargs = run.await_args.kwargs
    assert kwargs["explain"] is True
    # The driver gets the validated body; it prepends EXPLAIN itself.
    assert kwargs["sql"] == "SELECT id FROM t WHERE x = 1"
    payload = result.to_json()
    assert payload["success"] is True
    assert payload["columns"] == ["QUERY PLAN"]
    assert payload["csv"] == "QUERY PLAN\nSeq Scan on t\n"
    assert payload["warnings"] == []
    entry = _audit_entries(tmp_path)[-1]
    assert entry["explain"] is True and entry["status"] == "OK"


@pytest.mark.asyncio
async def test_gateway_explain_json_skips_describe(tmp_path):
    from db_gateway.core import execute_gateway_query

    run = AsyncMock(return_value=_plan_result())
    with patch("db_gateway.core.run_psql", run):
        result = await execute_gateway_query(
            cfg=_cfg(tmp_path), database="prod", sql="SELECT 1", explain="true",
            output_format="json",
        )

    assert run.await_args.kwargs["describe"] is False
    assert result.rows == [["Seq Scan on t"]]
    assert result.column_types == ["text"]


@pytest.mark.asyncio
async def test_gateway_without_explain_does_not_pass_explain_to_driver(tmp_path):
    from db_gateway.core import execute_gateway_query

    run = AsyncMock(return_value=_page_result(has_more=False, truncated=False))
    with patch("db_gateway.core.run_psql", run):
        await execute_gateway_query(cfg=_cfg(tmp_path), database="prod", sql="SELECT 1")

    assert "explain" not in run.await_args.kwargs


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sql",
    [
        "EXPLAIN SELECT 1",
        "EXPLAIN ANALYZE SELECT 1",
        "SELECT 1; DELETE FROM t",
        "DELETE FROM t",
    ],
)
async def test_gateway_explain_keeps_read_only_checks(tmp_path, sql):
    from db_gateway.core import execute_gateway_query

    run = AsyncMock()
    with patch("db_gateway.core.run_psql", run):
        result = await execute_gateway_query(
            cfg=_cfg(tmp_path), database="prod", sql=sql, explain=True,
        )

    assert result.code == "SQL_SAFETY_ERROR"
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_gateway_rejects_non_boolean_explain(tmp_path):
    from db_gateway.core import execute_gateway_query

    run = AsyncMock()
    with patch("db_gateway.core.run_psql", run):
        result = await execute_gateway_query(
            cfg=_cfg(tmp_path), database="prod", sql="SELECT 1", explain="maybe",
        )

    assert result.code == "INVALID_ARGUMENT"
    assert "explain" in (result.error or "")
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_gateway_explain_is_part_of_singleflight_key(tmp_path):
    from db_gateway.core import execute_gateway_query

    async def fake(**kwargs):
        await asyncio.sleep(0.05)
        return _plan_result() if kwargs.get("explain") else _page_result(has_more=False, truncated=False)

    run = AsyncMock(side_effect=fake)
    cfg = _cfg(tmp_path)
    with patch("db_gateway.core.run_psql", run):
        plain, plan = await asyncio.gather(
            execute_gateway_query(cfg=cfg, database="prod", sql="SELECT 1"),
            execute_gateway_query(cfg=cfg, database="prod", sql="SELECT 1", explain=True),
        )

    assert run.await_count == 2
    assert plan.columns == ["QUERY PLAN"]
    assert plain.columns == ["id"]


# ---------------------------------------------------------------------------
# Index hints on QUERY_TIMEOUT
# ---------------------------------------------------------------------------

_INDEX_ROWS = [
    ["public", "workflow_runs", "workflow_runs_pkey",
     "CREATE UNIQUE INDEX workflow_runs_pkey ON public.workflow_runs USING btree (id)"],
    ["public", "workflow_runs", "workflow_run_tenant_idx",
     "CREATE INDEX workflow_run_tenant_idx ON public.workflow_runs USING btree (tenant_id, created_at)"],
    ["public", "apps", "apps_pkey", "CREATE UNIQUE INDEX apps_pkey ON public.apps USING btree (id)"],
    ["public", "messages", "messages_pkey", "CREATE UNIQUE INDEX messages_pkey ON public.messages USING btree (id)"],
]
_TIMEOUT_SQL = (
    "WITH recent AS (SELECT * FROM messages) "
    "SELECT r.id FROM workflow_runs r JOIN apps a ON a.id = r.app_id, recent "
    "WHERE r.status = 'x'"
)


async def _timeout_query(tmp_path, sql=_TIMEOUT_SQL, cfg=None):
    from db_gateway.core import execute_gateway_query

    with patch("db_gateway.core.run_psql", AsyncMock(side_effect=asyncio.TimeoutError())):
        return await execute_gateway_query(cfg=cfg or _cfg(tmp_path), database="prod", sql=sql)


@pytest.mark.asyncio
async def test_timeout_error_lists_indexes_of_referenced_tables(tmp_path):
    from db_gateway import schema_info

    meta = AsyncMock(return_value=_INDEX_ROWS)
    with patch("db_gateway.core.run_metadata_query", meta):
        result = await _timeout_query(tmp_path)

    assert result.code == "QUERY_TIMEOUT"
    error = result.error or ""
    assert error.startswith("查询超时：psql 在 30s")
    assert "workflow_run_tenant_idx ON public.workflow_runs USING btree (tenant_id, created_at)" in error
    assert "apps_pkey" in error
    # CTE names are not tables, but the table read inside the CTE is.
    assert "messages_pkey" in error and "- recent" not in error
    assert "explain=true" in error
    kwargs = meta.await_args.kwargs
    assert kwargs["sql"] == schema_info.ALL_INDEXES_SQL
    assert kwargs["database"] == "langgenius" and kwargs["port"] == 56061
    assert kwargs["timeout"] <= 2
    assert "workflow_run_tenant_idx" in _audit_entries(tmp_path)[-1]["error"]


@pytest.mark.asyncio
async def test_timeout_index_hint_marks_tables_without_indexes(tmp_path):
    with patch("db_gateway.core.run_metadata_query", AsyncMock(return_value=_INDEX_ROWS)):
        result = await _timeout_query(tmp_path, sql="SELECT * FROM audit_events")

    assert "- audit_events: 未找到索引" in (result.error or "")


@pytest.mark.asyncio
async def test_timeout_index_cache_refreshes_at_most_hourly(tmp_path):
    from db_gateway import core

    meta = AsyncMock(return_value=_INDEX_ROWS)
    with patch("db_gateway.core.run_metadata_query", meta):
        await _timeout_query(tmp_path)
        await _timeout_query(tmp_path, sql="SELECT * FROM apps")
        assert meta.await_count == 1
        # Age the cache entry past the TTL: the next timeout refreshes it.
        for key, (fetched_at, rows) in list(core._INDEX_CACHE.items()):
            core._INDEX_CACHE[key] = (fetched_at - core._INDEX_CACHE_TTL_SECONDS - 1, rows)
        await _timeout_query(tmp_path)

    assert meta.await_count == 2


@pytest.mark.asyncio
async def test_timeout_index_lookup_failure_keeps_original_error(tmp_path):
    meta = AsyncMock(side_effect=RuntimeError("ERROR:  permission denied"))
    with patch("db_gateway.core.run_metadata_query", meta):
        first = await _timeout_query(tmp_path)
        second = await _timeout_query(tmp_path)

    original = "查询超时：psql 在 30s（另加计数预算）内未完成，已终止"
    assert first.code == "QUERY_TIMEOUT" and first.error == original
    assert second.error == original
    # Failed lookups back off instead of hitting the database on every timeout.
    assert meta.await_count == 1


@pytest.mark.asyncio
async def test_timeout_index_lookup_is_bounded_by_a_short_budget(tmp_path, monkeypatch):
    from db_gateway import core

    async def hang(**_kwargs):
        await asyncio.sleep(10)

    monkeypatch.setattr(core, "_INDEX_HINT_TOTAL_BUDGET_SECONDS", 0.05)
    with patch("db_gateway.core.run_metadata_query", AsyncMock(side_effect=hang)):
        result = await asyncio.wait_for(_timeout_query(tmp_path), timeout=2)

    assert result.code == "QUERY_TIMEOUT"
    assert result.error == "查询超时：psql 在 30s（另加计数预算）内未完成，已终止"


@pytest.mark.asyncio
async def test_timeout_without_table_names_skips_index_lookup(tmp_path):
    meta = AsyncMock(return_value=_INDEX_ROWS)
    with patch("db_gateway.core.run_metadata_query", meta):
        result = await _timeout_query(tmp_path, sql="SELECT pg_sleep(60)")

    meta.assert_not_awaited()
    assert result.code == "QUERY_TIMEOUT"


@pytest.mark.asyncio
async def test_timeout_index_lookup_uses_the_pool_driver(tmp_path):
    meta = AsyncMock(return_value=_INDEX_ROWS)
    with patch("db_gateway.core.run_metadata_query", meta), patch(
        "db_gateway.core.run_pool_query", AsyncMock(side_effect=asyncio.TimeoutError()),
    ):
        from db_gateway.core import execute_gateway_query

        result = await execute_gateway_query(
            cfg=_pool_cfg(tmp_path), database="billing", sql="SELECT * FROM apps",
        )

    assert "apps_pkey" in (result.error or "")
    kwargs = meta.await_args.kwargs
    assert kwargs["use_pool"] is True and kwargs["port"] == 56062
    assert kwargs["auth_home"] == str(tmp_path / "tsh")


# ---------------------------------------------------------------------------
# db_gateway_schema
# ---------------------------------------------------------------------------

_COLUMN_ROWS = [
    ["public", "workflow_runs", "id", "uuid", "uuid", None, "NO", "uuid_generate_v4()"],
    ["public", "workflow_runs", "tenant_id", "uuid", "uuid", None, "NO", None],
    ["public", "workflow_runs", "status", "character varying", "varchar", "255", "YES", None],
    ["public", "workflow_runs", "created_at", "timestamp without time zone", "timestamp", None, "NO", None],
]


def _fake_catalog(columns=_COLUMN_ROWS, indexes=_INDEX_ROWS[:2]):
    from db_gateway import schema_info

    async def fake(**kwargs):
        if kwargs["sql"] == schema_info.COLUMNS_SQL:
            return columns
        if kwargs["sql"] == schema_info.INDEXES_SQL:
            return indexes
        raise AssertionError(kwargs["sql"])

    return AsyncMock(side_effect=fake)


@pytest.mark.asyncio
async def test_schema_lookup_returns_columns_and_indexes(tmp_path):
    from db_gateway.core import execute_schema_lookup

    meta = _fake_catalog()
    with patch("db_gateway.core.run_metadata_query", meta):
        data = await execute_schema_lookup(
            cfg=_cfg(tmp_path), database="prod", tables=["workflow_runs"],
        )

    assert data["success"] is True and data["not_found"] == [] and data["warnings"] == []
    (table,) = data["tables"]
    assert (table["schema"], table["table"]) == ("public", "workflow_runs")
    assert table["columns"][0] == {
        "name": "id", "type": "uuid", "nullable": False, "default": "uuid_generate_v4()",
    }
    assert table["columns"][2]["type"] == "varchar(255)"
    assert table["columns"][3]["type"] == "timestamp"
    assert [i["name"] for i in table["indexes"]] == ["workflow_runs_pkey", "workflow_run_tenant_idx"]
    # Table names travel only as bind parameters.
    for call in meta.await_args_list:
        assert call.kwargs["params"] == (["workflow_runs"],)
        assert call.kwargs["database"] == "langgenius"
        assert "workflow_runs" not in call.kwargs["sql"]
    entry = _audit_entries(tmp_path)[-1]
    assert entry["tool"] == "db_gateway_schema" and entry["tables"] == ["workflow_runs"]


@pytest.mark.asyncio
async def test_schema_lookup_reports_missing_tables(tmp_path):
    from db_gateway.core import execute_schema_lookup

    with patch("db_gateway.core.run_metadata_query", _fake_catalog()):
        data = await execute_schema_lookup(
            cfg=_cfg(tmp_path), database="plugin", tables=["workflow_runs", "public.nope"],
        )

    assert data["success"] is True
    assert data["not_found"] == ["public.nope"]
    assert any("public.nope" in w for w in data["warnings"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("database", "tables", "code"),
    [
        ("nope", ["t"], "UNKNOWN_DATABASE"),
        ("prod", [], "INVALID_ARGUMENT"),
        ("prod", None, "INVALID_ARGUMENT"),
        ("prod", ["t; DROP TABLE x"], "INVALID_ARGUMENT"),
        ("prod", [f"t{i}" for i in range(21)], "INVALID_ARGUMENT"),
    ],
)
async def test_schema_lookup_rejects_bad_arguments(tmp_path, database, tables, code):
    from db_gateway.core import execute_schema_lookup

    meta = _fake_catalog()
    with patch("db_gateway.core.run_metadata_query", meta):
        data = await execute_schema_lookup(cfg=_cfg(tmp_path), database=database, tables=tables)

    assert data["success"] is False and data["code"] == code
    meta.assert_not_awaited()


@pytest.mark.asyncio
async def test_schema_lookup_retries_connection_loss_then_reports_teleport(tmp_path):
    from db_gateway.core import execute_schema_lookup

    meta = AsyncMock(side_effect=TeleportAuthExpired("connection reset"))
    with patch("db_gateway.core.run_metadata_query", meta):
        data = await execute_schema_lookup(cfg=_cfg(tmp_path), database="prod", tables=["t"])

    assert data["code"] == "TELEPORT_EXPIRED"
    assert meta.await_count == 2  # teleport_retry_attempts=1


# ---------------------------------------------------------------------------
# Audit log location
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_log_dir_setting_moves_the_audit_file(tmp_path):
    from db_gateway.core import execute_gateway_query

    cfg = _cfg(tmp_path)
    target = tmp_path / "mnt" / "logs"  # does not exist yet
    cfg.db_gateway.audit_log_dir = str(target)
    with patch(
        "db_gateway.core.run_psql",
        AsyncMock(return_value=_page_result(has_more=False, truncated=False)),
    ):
        await execute_gateway_query(cfg=cfg, database="prod", sql="SELECT 1")

    lines = (target / "db_gateway_audit.log").read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[-1])["status"] == "OK"
    assert not (tmp_path / ".state" / "logs" / "db_gateway_audit.log").exists()


def test_audit_path_defaults_to_state_logs_dir(tmp_path):
    from db_gateway.config import AppConfig
    from db_gateway.core import _audit_path

    cfg = AppConfig()
    cfg.resources.base_dir = str(tmp_path)
    assert _audit_path(cfg) == tmp_path / ".state" / "logs" / "db_gateway_audit.log"
    cfg.db_gateway.audit_log_dir = "/var/lib/mcp/logs"
    assert str(_audit_path(cfg)) == "/var/lib/mcp/logs/db_gateway_audit.log"
