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


# ---------------------------------------------------------------------------
# Pagination / total_rows / format / ticket_id / timing
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
            ticket_id="123",
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
    assert entry["ticket_id_missing"] is False
    assert "queue_wait_ms" in entry["timing_ms"]


@pytest.mark.asyncio
async def test_gateway_warns_when_paging_without_order_by(tmp_path):
    from db_gateway.core import execute_gateway_query

    with patch("db_gateway.core.run_psql", AsyncMock(return_value=_page_result())):
        result = await execute_gateway_query(
            cfg=_cfg(tmp_path),
            database="prod",
            sql="SELECT id FROM tenants",
            ticket_id="123",
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
            cfg=_cfg(tmp_path), database="prod", sql="SELECT id FROM t", ticket_id="1",
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
            cfg=_cfg(tmp_path), database="prod", sql="SELECT id FROM t ORDER BY id",
            ticket_id="1", offset=10, limit=2,
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
            cfg=cfg, database="prod", sql="SELECT 1", ticket_id="1", limit="5000",
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
            cfg=_cfg(tmp_path), database="prod", sql="SELECT 1", ticket_id="1", **kwargs,
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
            cfg=_cfg(tmp_path), database="prod", sql="SELECT id, note FROM t",
            ticket_id="1", output_format="JSON",
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
            cfg=_cfg(tmp_path), database="prod", sql="SELECT 1", ticket_id="1",
            output_format="json",
        )

    assert result.columns == ["id", "name"]
    assert result.rows == [["1", "a,b"]]


@pytest.mark.asyncio
async def test_gateway_csv_format_is_default_and_has_no_rows_key(tmp_path):
    from db_gateway.core import execute_gateway_query

    with patch("db_gateway.core.run_psql", AsyncMock(return_value=_page_result())):
        result = await execute_gateway_query(
            cfg=_cfg(tmp_path), database="prod", sql="SELECT id FROM t ORDER BY id", ticket_id="1",
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
            sql="(SELECT 1) UNION ALL (SELECT 2)", ticket_id="1",
        )

    assert result.success is True
    run.assert_awaited_once()


@pytest.mark.asyncio
async def test_gateway_sql_safety_rejection_has_reason_and_is_audited(tmp_path):
    from db_gateway.core import execute_gateway_query

    run = AsyncMock()
    with patch("db_gateway.core.run_psql", run):
        result = await execute_gateway_query(
            cfg=_cfg(tmp_path), database="prod", sql="EXPLAIN SELECT 1", ticket_id="1",
        )

    assert result.code == "SQL_SAFETY_ERROR"
    assert "SELECT 或 WITH" in (result.error or "") and "EXPLAIN" in (result.error or "")
    run.assert_not_awaited()
    assert _audit_entries(tmp_path)[-1]["status"] == "REJECTED_SQL_SAFETY"


@pytest.mark.asyncio
async def test_gateway_requires_ticket_id_when_enabled(tmp_path):
    from db_gateway.core import execute_gateway_query

    cfg = _cfg(tmp_path)
    cfg.db_gateway.require_ticket_id = True
    run = AsyncMock()
    with patch("db_gateway.core.run_psql", run):
        result = await execute_gateway_query(
            cfg=cfg, database="prod", sql="SELECT 1", ticket_id="  ",
        )

    assert result.success is False
    assert result.code == "TICKET_ID_REQUIRED"
    run.assert_not_awaited()
    entry = _audit_entries(tmp_path)[-1]
    assert entry["status"] == "REJECTED_TICKET_REQUIRED"
    assert entry["ticket_id_missing"] is True


@pytest.mark.asyncio
async def test_gateway_missing_ticket_warns_and_audits_when_not_required(tmp_path):
    from db_gateway.core import execute_gateway_query

    cfg = _cfg(tmp_path)
    cfg.db_gateway.require_ticket_id = False
    run = AsyncMock(return_value=_page_result(has_more=False, truncated=False))
    with patch("db_gateway.core.run_psql", run):
        result = await execute_gateway_query(cfg=cfg, database="prod", sql="SELECT 1")

    assert result.success is True
    assert any("ticket_id" in w for w in result.warnings)
    entry = _audit_entries(tmp_path)[-1]
    assert entry["status"] == "OK"
    assert entry["ticket_id"] == ""
    assert entry["ticket_id_missing"] is True


@pytest.mark.asyncio
async def test_gateway_missing_ticket_feishu_alert_is_throttled(tmp_path):
    from db_gateway.core import execute_gateway_query

    cfg = _cfg(tmp_path)
    cfg.db_gateway.require_ticket_id = False
    cfg.db_gateway.missing_ticket_alert_interval_seconds = 3600
    cfg.feishu.webhook_url = "https://example.invalid/hook"
    cfg.feishu.webhook_secret = ""
    core_module._MISSING_TICKET_ALERT_STATE.update({"last_sent": None, "suppressed": 0})
    send = AsyncMock()
    run = AsyncMock(return_value=_page_result(has_more=False, truncated=False))
    try:
        with patch("db_gateway.core.run_psql", run), patch(
            "db_gateway.core.FeishuClient.send_alert", send,
        ):
            for i in range(3):
                await execute_gateway_query(cfg=cfg, database="prod", sql=f"SELECT {i}")
            await asyncio.sleep(0)
            await asyncio.sleep(0)
    finally:
        state = dict(core_module._MISSING_TICKET_ALERT_STATE)
        core_module._MISSING_TICKET_ALERT_STATE.update({"last_sent": None, "suppressed": 0})

    assert send.await_count == 1
    assert state["suppressed"] == 2


@pytest.mark.asyncio
async def test_gateway_missing_ticket_alert_disabled_by_default(tmp_path):
    from db_gateway.core import execute_gateway_query

    cfg = _cfg(tmp_path)
    cfg.feishu.webhook_url = "https://example.invalid/hook"
    send = AsyncMock()
    with patch(
        "db_gateway.core.run_psql",
        AsyncMock(return_value=_page_result(has_more=False, truncated=False)),
    ), patch("db_gateway.core.FeishuClient.send_alert", send):
        await execute_gateway_query(cfg=cfg, database="prod", sql="SELECT 1")
        await asyncio.sleep(0)

    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_gateway_timeout_has_explicit_code(tmp_path):
    from db_gateway.core import execute_gateway_query

    with patch("db_gateway.core.run_psql", AsyncMock(side_effect=asyncio.TimeoutError())):
        result = await execute_gateway_query(
            cfg=_cfg(tmp_path), database="prod", sql="SELECT 1", ticket_id="1",
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
            cfg=_cfg(tmp_path), database="prod", sql="SELECT 7", ticket_id="1",
        )

    assert result.success is True
    assert result.timing_ms["attempts"] == 2
    assert "teleport_retry_ms" in result.timing_ms
