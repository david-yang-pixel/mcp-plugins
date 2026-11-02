"""Core read-only DB gateway execution logic."""
from __future__ import annotations

import asyncio
import csv
import io
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from db_gateway.feishu import FeishuClient
from db_gateway.postgres import (
    PsqlResult,
    PsqlSafetyError,
    TeleportAuthExpired,
    audit_log,
    run_psql,
    sql_has_top_level_order_by,
    validate_read_only_sql,
)

try:
    from db_gateway.log import get_logger
    logger = get_logger(module="db_gateway.core")
except Exception:  # pragma: no cover
    import logging as _logging
    logger = _logging.getLogger("db_gateway.core")  # type: ignore[assignment]


_DB_KEY_TO_ATTR = {
    "prod": "db_prod",
    "billing": "db_billing",
    "plugin": "db_plugin",
}

_DB_KEY_TO_PORT_ATTR = {
    "prod": "port_prod",
    "billing": "port_billing",
    "plugin": "port_plugin",
}

_VALID_FORMATS = ("csv", "json")
_DEFAULT_COUNT_TIMEOUT_SECONDS = 5.0

_QUERY_STATE_LOCK = asyncio.Lock()
_QUERY_LIMITERS: dict[tuple[str, int, int], asyncio.Semaphore] = {}
_INFLIGHT_QUERIES: dict[tuple[Any, ...], asyncio.Task[PsqlResult]] = {}

# Missing ticket_id Feishu alert throttle (process-wide).
_MISSING_TICKET_ALERT_STATE: dict[str, Any] = {"last_sent": None, "suppressed": 0}
_BACKGROUND_TASKS: set[asyncio.Task[None]] = set()

@dataclass
class GatewayQueryResult:
    success: bool
    csv: str = ""
    row_count: int = 0
    elapsed_ms: int = 0
    truncated: bool = False
    code: str = ""
    error: str | None = None
    format: str = "csv"
    columns: list[str] = field(default_factory=list)
    rows: list[list[str | None]] | None = None
    column_types: list[str] | None = None
    total_rows: int | None = None
    has_more: bool = False
    next_offset: int | None = None
    offset: int = 0
    limit: int | None = None
    warnings: list[str] = field(default_factory=list)
    timing_ms: dict[str, float] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "success": self.success,
            "format": self.format,
            "csv": self.csv,
            "row_count": self.row_count,
            "total_rows": self.total_rows,
            "offset": self.offset,
            "limit": self.limit,
            "has_more": self.has_more,
            "next_offset": self.next_offset,
            "truncated": self.truncated,
            "columns": self.columns,
            "elapsed_ms": self.elapsed_ms,
            "timing_ms": self.timing_ms,
            "warnings": self.warnings,
            "code": self.code,
            "error": self.error,
        }
        if self.format == "json":
            data["rows"] = self.rows if self.rows is not None else []
            data["column_types"] = self.column_types
        return data


def _audit_path(cfg: Any) -> Path:
    return (
        Path(cfg.resources.base_dir)
        / cfg.resources.state_dir
        / "logs"
        / "db_gateway_audit.log"
    )


def _cfg_number(obj: Any, name: str, default: float) -> float:
    value = getattr(obj, name, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return value


def _cfg_flag(obj: Any, name: str, default: bool = False) -> bool:
    value = getattr(obj, name, default)
    return value if isinstance(value, bool) else default


def _parse_int(value: Any, name: str, *, minimum: int) -> int | None:
    """Parse an optional integer argument (accepts JSON numbers or digit strings)."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} 必须是整数")
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError(f"{name} 必须是整数")
        value = int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text.lstrip("-").isdigit():
            raise ValueError(f"{name} 必须是整数")
        value = int(text)
    if not isinstance(value, int):
        raise ValueError(f"{name} 必须是整数")
    if value < minimum:
        raise ValueError(f"{name} 不能小于 {minimum}")
    return value


def _csv_to_table(csv_text: str) -> tuple[list[str], list[list[str | None]]]:
    """Fallback parser for results that only carry CSV text (no NULL info)."""
    if not csv_text.strip():
        return [], []
    parsed = list(csv.reader(io.StringIO(csv_text)))
    return parsed[0], [list(row) for row in parsed[1:]]


def _notify_missing_ticket(cfg: Any, database: str) -> None:
    """Throttled Feishu alert for queries without ticket_id (opt-in)."""
    interval = _cfg_number(cfg.db_gateway, "missing_ticket_alert_interval_seconds", 0)
    if interval <= 0:
        return
    feishu_cfg = getattr(cfg, "feishu", None)
    webhook_url = getattr(feishu_cfg, "webhook_url", "")
    if not isinstance(webhook_url, str) or not webhook_url:
        return
    webhook_secret = getattr(feishu_cfg, "webhook_secret", "")
    if not isinstance(webhook_secret, str):
        webhook_secret = ""

    state = _MISSING_TICKET_ALERT_STATE
    now = time.monotonic()
    last_sent = state["last_sent"]
    if last_sent is not None and now - last_sent < interval:
        state["suppressed"] += 1
        return
    suppressed = int(state["suppressed"])
    state["last_sent"] = now
    state["suppressed"] = 0

    async def send() -> None:
        try:
            client = FeishuClient(webhook_url=webhook_url, webhook_secret=webhook_secret)
            await client.send_alert(
                title="DB Gateway 查询缺少 ticket_id",
                message=(
                    f"database: {database}\n"
                    f"上次提醒后另有 {suppressed} 次缺少 ticket_id 的查询（已合并，未单独提醒）。\n"
                    f"同类提醒最多每 {int(interval)} 秒一次；可开启 db_gateway.require_ticket_id 强制要求工单号。"
                ),
            )
        except Exception as e:  # never break the query path
            logger.warning("missing_ticket_alert_failed", error=str(e))

    task = asyncio.create_task(send())
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


def _schedule_query_cleanup(
    singleflight_key: tuple[Any, ...],
    task: asyncio.Task[PsqlResult],
) -> None:
    async def cleanup() -> None:
        async with _QUERY_STATE_LOCK:
            if _INFLIGHT_QUERIES.get(singleflight_key) is task:
                _INFLIGHT_QUERIES.pop(singleflight_key, None)

    asyncio.create_task(cleanup())


async def execute_gateway_query(
    *,
    cfg: Any,
    database: str,
    sql: str,
    ticket_id: str = "",
    offset: Any = 0,
    limit: Any = None,
    output_format: Any = "csv",
) -> GatewayQueryResult:
    """Validate and execute one read-only query page through local DB proxy."""
    started = time.perf_counter()
    sql = (sql or "").strip()
    ticket_id = (ticket_id or "").strip()
    warnings: list[str] = []

    def elapsed_timing() -> dict[str, float]:
        return {"total_ms": round((time.perf_counter() - started) * 1000, 1)}

    def fail(code: str, error: str, **kw: Any) -> GatewayQueryResult:
        logger.warning(
            "db_gateway_query_failed",
            database=database,
            code=code,
            ticket_id=ticket_id,
            ticket_id_missing=not ticket_id,
            **kw,
        )
        return GatewayQueryResult(
            success=False,
            code=code,
            error=error,
            format=fmt if fmt in _VALID_FORMATS else "csv",
            warnings=warnings,
            timing_ms=kw.get("timing_ms") or elapsed_timing(),
        )

    fmt = str(output_format or "csv").strip().lower()
    if database not in _DB_KEY_TO_ATTR:
        return fail(
            "UNKNOWN_DATABASE",
            f"unknown database key: {database}. Must be one of {list(_DB_KEY_TO_ATTR)}",
        )
    if fmt not in _VALID_FORMATS:
        return fail("INVALID_ARGUMENT", f"format 只支持 {list(_VALID_FORMATS)}，收到: {output_format!r}")

    max_rows = max(1, int(cfg.postgres.max_rows))
    try:
        page_offset = _parse_int(offset, "offset", minimum=0) or 0
        page_limit = _parse_int(limit, "limit", minimum=1)
    except ValueError as e:
        return fail("INVALID_ARGUMENT", str(e))
    if page_limit is None:
        page_limit = max_rows
    elif page_limit > max_rows:
        warnings.append(f"limit={page_limit} 超过上限 max_rows={max_rows}，已按 {max_rows} 返回")
        page_limit = max_rows

    audit_extra = {"offset": page_offset, "limit": page_limit, "format": fmt}
    audit_file = _audit_path(cfg)

    if not ticket_id:
        if _cfg_flag(cfg.db_gateway, "require_ticket_id"):
            audit_log(
                log_path=audit_file,
                database=database,
                sql=sql,
                status="REJECTED_TICKET_REQUIRED",
                ticket_id="",
                error="ticket_id is required",
                **audit_extra,
            )
            return fail(
                "TICKET_ID_REQUIRED",
                "网关已开启 require_ticket_id：必须提供 ticket_id（Zendesk 工单号）才能查询",
            )
        warnings.append(
            "未提供 ticket_id：本次查询已在审计日志中记录为缺少工单号，请传入 Zendesk 工单号"
        )
        _notify_missing_ticket(cfg, database)

    try:
        body = validate_read_only_sql(sql)
    except PsqlSafetyError as e:
        audit_log(
            log_path=audit_file,
            database=database,
            sql=sql,
            status="REJECTED_SQL_SAFETY",
            ticket_id=ticket_id,
            error=str(e),
            **audit_extra,
        )
        return fail("SQL_SAFETY_ERROR", f"SQL safety check failed: {e}")

    db_name = getattr(cfg.postgres, _DB_KEY_TO_ATTR[database])
    db_port = getattr(
        cfg.postgres,
        _DB_KEY_TO_PORT_ATTR[database],
        cfg.postgres.port,
    )

    error_status = ""
    error_code = ""
    error_text = ""
    try:
        result = await _run_psql_controlled(
            cfg=cfg,
            database_key=database,
            db_name=db_name,
            db_port=db_port,
            sql=body,
            offset=page_offset,
            limit=page_limit,
            describe=(fmt == "json"),
        )
    except PsqlSafetyError as e:
        error_status, error_code, error_text = "REJECTED_SQL_SAFETY", "SQL_SAFETY_ERROR", f"SQL safety check failed: {e}"
    except TeleportAuthExpired as e:
        error_status, error_code, error_text = "TELEPORT_EXPIRED", "TELEPORT_EXPIRED", str(e)
    except asyncio.TimeoutError:
        timeout = cfg.postgres.query_timeout_seconds
        error_status, error_code = "TIMEOUT", "QUERY_TIMEOUT"
        error_text = f"查询超时：psql 在 {timeout}s（另加计数预算）内未完成，已终止"
    except Exception as e:
        error_status, error_code, error_text = "ERROR", "QUERY_ERROR", str(e)
    if error_code:
        timing = elapsed_timing()
        audit_log(
            log_path=audit_file,
            database=database,
            sql=sql,
            status=error_status,
            ticket_id=ticket_id,
            error=error_text,
            timing_ms=timing,
            **audit_extra,
        )
        return fail(error_code, error_text, timing_ms=timing)

    has_more = bool(result.has_more if result.has_more is not None else result.truncated)
    if (page_offset > 0 or has_more) and not sql_has_top_level_order_by(body):
        warnings.append(
            "原查询没有顶层 ORDER BY：分页（offset/next_offset）结果顺序不保证稳定，"
            "请加 ORDER BY（建议包含唯一键）"
        )
    if (
        page_offset > 0
        and result.offset_skipped is not None
        and result.offset_skipped < page_offset
    ):
        warnings.append(
            f"offset={page_offset} 超出结果总行数 {result.offset_skipped}，本页无数据"
        )

    total_rows = result.total_rows
    if total_rows is None and result.total_rows_note:
        warnings.append(f"total_rows 为 null：{result.total_rows_note}")

    if fmt == "json":
        if result.rows is not None:
            columns, rows = list(result.columns), result.rows
        else:
            columns, rows = _csv_to_table(result.csv)
        csv_text = ""
    else:
        columns = list(result.columns) if result.columns else _csv_to_table(result.csv)[0]
        rows = None
        csv_text = result.csv

    timing = dict(result.timing_ms)
    timing.update(elapsed_timing())
    next_offset = page_offset + result.row_count if has_more else None

    audit_log(
        log_path=audit_file,
        database=database,
        sql=sql,
        status="OK",
        ticket_id=ticket_id,
        row_count=result.row_count,
        elapsed_ms=result.elapsed_ms,
        total_rows=total_rows,
        has_more=has_more,
        timing_ms=timing,
        **audit_extra,
    )
    logger.info(
        "db_gateway_query",
        database=database,
        ticket_id=ticket_id,
        ticket_id_missing=not ticket_id,
        row_count=result.row_count,
        total_rows=total_rows,
        offset=page_offset,
        limit=page_limit,
        format=fmt,
        has_more=has_more,
        timing_ms=timing,
    )
    return GatewayQueryResult(
        success=True,
        csv=csv_text,
        row_count=result.row_count,
        elapsed_ms=result.elapsed_ms,
        truncated=has_more,
        format=fmt,
        columns=columns,
        rows=rows,
        column_types=result.column_types if fmt == "json" else None,
        total_rows=total_rows,
        has_more=has_more,
        next_offset=next_offset,
        offset=page_offset,
        limit=page_limit,
        warnings=warnings,
        timing_ms=timing,
    )


async def _run_psql_controlled(
    *,
    cfg: Any,
    database_key: str,
    db_name: str,
    db_port: int,
    sql: str,
    offset: int = 0,
    limit: int | None = None,
    describe: bool = False,
) -> PsqlResult:
    max_concurrent = max(1, int(getattr(cfg.postgres, "max_concurrent_queries", 3) or 3))
    singleflight_enabled = bool(getattr(cfg.postgres, "singleflight_enabled", True))
    count_timeout = float(
        _cfg_number(cfg.postgres, "count_timeout_seconds", _DEFAULT_COUNT_TIMEOUT_SECONDS)
    )
    run_kwargs: dict[str, Any] = {
        "cfg": cfg,
        "database_key": database_key,
        "db_name": db_name,
        "db_port": db_port,
        "sql": sql,
        "offset": offset,
        "limit": limit,
        "count_timeout": count_timeout,
        "describe": describe,
        "max_concurrent": max_concurrent,
    }
    singleflight_key = (
        database_key,
        cfg.postgres.host,
        db_port,
        cfg.postgres.user,
        db_name,
        sql,
        int(cfg.postgres.max_rows),
        offset,
        limit,
        count_timeout,
        describe,
    )
    if not singleflight_enabled:
        return await _run_psql_with_limiter(**run_kwargs)

    owner = False
    async with _QUERY_STATE_LOCK:
        task = _INFLIGHT_QUERIES.get(singleflight_key)
        if task is None:
            task = asyncio.create_task(_run_psql_with_limiter(**run_kwargs))
            _INFLIGHT_QUERIES[singleflight_key] = task
            owner = True

    try:
        return await asyncio.shield(task)
    finally:
        if owner:
            if task.done():
                async with _QUERY_STATE_LOCK:
                    if _INFLIGHT_QUERIES.get(singleflight_key) is task:
                        _INFLIGHT_QUERIES.pop(singleflight_key, None)
            else:
                task.add_done_callback(
                    lambda finished: _schedule_query_cleanup(singleflight_key, finished)
                )


async def _run_psql_with_limiter(
    *,
    cfg: Any,
    database_key: str,
    db_name: str,
    db_port: int,
    sql: str,
    max_concurrent: int,
    offset: int = 0,
    limit: int | None = None,
    count_timeout: float = 0,
    describe: bool = False,
) -> PsqlResult:
    limiter = await _query_limiter(database_key, db_port, max_concurrent)
    wait_started = time.perf_counter()
    async with limiter:
        queue_wait_ms = round((time.perf_counter() - wait_started) * 1000, 1)
        result = await _run_psql_with_retries(
            cfg=cfg,
            db_name=db_name,
            db_port=db_port,
            sql=sql,
            offset=offset,
            limit=limit,
            count_timeout=count_timeout,
            describe=describe,
        )
    if isinstance(result.timing_ms, dict):
        result.timing_ms["queue_wait_ms"] = queue_wait_ms
    return result


async def _query_limiter(database_key: str, db_port: int, max_concurrent: int) -> asyncio.Semaphore:
    limiter_key = (database_key, db_port, max_concurrent)
    async with _QUERY_STATE_LOCK:
        limiter = _QUERY_LIMITERS.get(limiter_key)
        if limiter is None:
            limiter = asyncio.Semaphore(max_concurrent)
            _QUERY_LIMITERS[limiter_key] = limiter
        return limiter


async def _run_psql_with_retries(
    *,
    cfg: Any,
    db_name: str,
    db_port: int,
    sql: str,
    offset: int = 0,
    limit: int | None = None,
    count_timeout: float = 0,
    describe: bool = False,
) -> PsqlResult:
    attempts = max(0, int(getattr(cfg.postgres, "teleport_retry_attempts", 1) or 0)) + 1
    retry_delay = max(0.0, float(getattr(cfg.postgres, "teleport_retry_delay_seconds", 0.25) or 0.0))
    last_error: TeleportAuthExpired | None = None
    first_started = time.perf_counter()
    for attempt in range(attempts):
        try:
            result = await run_psql(
                host=cfg.postgres.host,
                port=db_port,
                user=cfg.postgres.user,
                password=cfg.postgres.password,
                database=db_name,
                sql=sql,
                timeout=cfg.postgres.query_timeout_seconds,
                max_rows=cfg.postgres.max_rows,
                offset=offset,
                limit=limit,
                count_timeout=count_timeout,
                describe=describe,
            )
            if attempt > 0 and isinstance(result.timing_ms, dict):
                # Time burnt by failed attempts + retry delay before this success.
                result.timing_ms["teleport_retry_ms"] = round(max(
                    0.0,
                    (time.perf_counter() - first_started) * 1000
                    - float(result.timing_ms.get("psql_process_ms", 0.0)),
                ), 1)
                result.timing_ms["attempts"] = attempt + 1
            return result
        except TeleportAuthExpired as e:
            last_error = e
            if attempt >= attempts - 1:
                raise
            if retry_delay:
                await asyncio.sleep(retry_delay)
    assert last_error is not None
    raise last_error
