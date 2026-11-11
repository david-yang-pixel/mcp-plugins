"""Core read-only DB gateway execution logic."""
from __future__ import annotations

import asyncio
import csv
import io
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from db_gateway import schema_info
from db_gateway.pg_pool import pool_settings_from_config, run_metadata_query, run_pool_query
from db_gateway.postgres import (
    PsqlResult,
    PsqlSafetyError,
    TeleportAuthExpired,
    audit_log,
    run_psql,
    sql_has_top_level_order_by,
    validate_read_only_sql,
)
from db_gateway.sql_text import SqlLexError, referenced_tables, tokenize

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

# Index hints appended to QUERY_TIMEOUT errors: every user index of a database,
# fetched with one cheap pg_indexes query and refreshed at most once per hour.
_INDEX_CACHE_TTL_SECONDS = 3600.0
# After a failed fetch, do not retry for this long (a burst of timeouts must not
# turn into a burst of catalog queries against a struggling database).
_INDEX_CACHE_FAILURE_BACKOFF_SECONDS = 60.0
_INDEX_HINT_STATEMENT_TIMEOUT_SECONDS = 2.0
_INDEX_HINT_TOTAL_BUDGET_SECONDS = 5.0
_INDEX_HINT_MAX_CHARS = 4000
_INDEX_CACHE: dict[tuple[Any, ...], tuple[float, list[list[Any]]]] = {}
_INDEX_CACHE_FAILED_AT: dict[tuple[Any, ...], float] = {}
_INDEX_CACHE_LOCKS: dict[tuple[Any, ...], asyncio.Lock] = {}

_SCHEMA_STATEMENT_TIMEOUT_SECONDS = 15.0

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


_AUDIT_FILE_NAME = "db_gateway_audit.log"


def _audit_path(cfg: Any) -> Path:
    """``db_gateway.audit_log_dir`` when set, else ``<base_dir>/<state_dir>/logs``."""
    configured = getattr(getattr(cfg, "db_gateway", None), "audit_log_dir", None)
    if isinstance(configured, str) and configured.strip():
        return Path(configured.strip()) / _AUDIT_FILE_NAME
    return (
        Path(cfg.resources.base_dir)
        / cfg.resources.state_dir
        / "logs"
        / _AUDIT_FILE_NAME
    )


def _cfg_number(obj: Any, name: str, default: float) -> float:
    value = getattr(obj, name, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return value


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


def _parse_bool(value: Any, name: str) -> bool:
    """Optional boolean argument (JSON true/false; also "true"/"false"/1/0)."""
    if value is None or value is False or value is True:
        return bool(value)
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("", "false", "0", "no"):
            return False
        if text in ("true", "1", "yes"):
            return True
    raise ValueError(f"{name} 必须是布尔值 true/false")


def _csv_to_table(csv_text: str) -> tuple[list[str], list[list[str | None]]]:
    """Fallback parser for results that only carry CSV text (no NULL info)."""
    if not csv_text.strip():
        return [], []
    parsed = list(csv.reader(io.StringIO(csv_text)))
    return parsed[0], [list(row) for row in parsed[1:]]


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
    offset: Any = 0,
    limit: Any = None,
    output_format: Any = "csv",
    explain: Any = False,
) -> GatewayQueryResult:
    """Validate and execute one read-only query page through local DB proxy.

    ``explain=True``: after the unchanged read-only checks, the gateway runs
    ``EXPLAIN <sql>`` (no ANALYZE, the statement is never executed) and returns
    the plan lines as rows of the ``QUERY PLAN`` column.
    """
    started = time.perf_counter()
    sql = (sql or "").strip()
    warnings: list[str] = []

    def elapsed_timing() -> dict[str, float]:
        return {"total_ms": round((time.perf_counter() - started) * 1000, 1)}

    def fail(code: str, error: str, **kw: Any) -> GatewayQueryResult:
        logger.warning(
            "db_gateway_query_failed",
            database=database,
            code=code,
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
    try:
        explain_plan = _parse_bool(explain, "explain")
    except ValueError as e:
        return fail("INVALID_ARGUMENT", str(e))

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

    audit_extra: dict[str, Any] = {"offset": page_offset, "limit": page_limit, "format": fmt}
    if explain_plan:
        audit_extra["explain"] = True
    audit_file = _audit_path(cfg)

    try:
        body = validate_read_only_sql(sql)
    except PsqlSafetyError as e:
        audit_log(
            log_path=audit_file,
            database=database,
            sql=sql,
            status="REJECTED_SQL_SAFETY",
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
            describe=(fmt == "json" and not explain_plan),
            explain=explain_plan,
        )
    except PsqlSafetyError as e:
        error_status, error_code, error_text = "REJECTED_SQL_SAFETY", "SQL_SAFETY_ERROR", f"SQL safety check failed: {e}"
    except TeleportAuthExpired as e:
        error_status, error_code, error_text = "TELEPORT_EXPIRED", "TELEPORT_EXPIRED", str(e)
    except asyncio.TimeoutError:
        timeout = cfg.postgres.query_timeout_seconds
        error_status, error_code = "TIMEOUT", "QUERY_TIMEOUT"
        error_text = f"查询超时：psql 在 {timeout}s（另加计数预算）内未完成，已终止"
        error_text += await _timeout_index_hint(
            cfg=cfg, database_key=database, db_name=db_name, db_port=db_port, body=body,
        )
    except Exception as e:
        error_status, error_code, error_text = "ERROR", "QUERY_ERROR", str(e)
    if error_code:
        timing = elapsed_timing()
        audit_log(
            log_path=audit_file,
            database=database,
            sql=sql,
            status=error_status,
            error=error_text,
            timing_ms=timing,
            **audit_extra,
        )
        return fail(error_code, error_text, timing_ms=timing)

    has_more = bool(result.has_more if result.has_more is not None else result.truncated)
    if (page_offset > 0 or has_more) and not explain_plan and not sql_has_top_level_order_by(body):
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
        explain=explain_plan,
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
    explain: bool = False,
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
        "explain": explain,
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
        explain,
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
    explain: bool = False,
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
            explain=explain,
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
    explain: bool = False,
) -> PsqlResult:
    use_pool = getattr(cfg.postgres, "driver", "psql") == "pool"
    attempts = max(0, int(getattr(cfg.postgres, "teleport_retry_attempts", 1) or 0)) + 1
    delay_attr = "pool_retry_delay_seconds" if use_pool else "teleport_retry_delay_seconds"
    retry_delay = max(0.0, float(getattr(cfg.postgres, delay_attr, 0.25) or 0.0))
    last_error: TeleportAuthExpired | None = None
    first_started = time.perf_counter()
    query_kwargs: dict[str, Any] = {
        "host": cfg.postgres.host,
        "port": db_port,
        "user": cfg.postgres.user,
        "password": cfg.postgres.password,
        "database": db_name,
        "sql": sql,
        "timeout": cfg.postgres.query_timeout_seconds,
        "max_rows": cfg.postgres.max_rows,
        "offset": offset,
        "limit": limit,
        "count_timeout": count_timeout,
        "describe": describe,
    }
    if explain:
        query_kwargs["explain"] = True
    if use_pool:
        teleport = getattr(cfg, "teleport", None)
        watch = bool(getattr(cfg.postgres, "pool_watch_teleport_keys", True))
        query_kwargs["settings"] = pool_settings_from_config(cfg)
        query_kwargs["auth_home"] = getattr(teleport, "auth_home", None) if watch else None
    for attempt in range(attempts):
        attempt_started = time.perf_counter()
        try:
            if use_pool:
                result = await run_pool_query(**query_kwargs)
            else:
                result = await run_psql(**query_kwargs)
            if attempt > 0 and isinstance(result.timing_ms, dict):
                # Time burnt by failed attempts + retry delay before this success.
                result.timing_ms["teleport_retry_ms"] = round(max(
                    0.0, (attempt_started - first_started) * 1000,
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


def _db_target(cfg: Any, database_key: str) -> tuple[str, int]:
    db_name = getattr(cfg.postgres, _DB_KEY_TO_ATTR[database_key])
    db_port = getattr(cfg.postgres, _DB_KEY_TO_PORT_ATTR[database_key], cfg.postgres.port)
    return db_name, db_port


def _metadata_kwargs(cfg: Any, *, db_name: str, db_port: int) -> dict[str, Any]:
    use_pool = getattr(cfg.postgres, "driver", "psql") == "pool"
    kwargs: dict[str, Any] = {
        "host": cfg.postgres.host,
        "port": db_port,
        "user": cfg.postgres.user,
        "password": cfg.postgres.password,
        "database": db_name,
        "use_pool": use_pool,
    }
    if use_pool:
        teleport = getattr(cfg, "teleport", None)
        watch = bool(getattr(cfg.postgres, "pool_watch_teleport_keys", True))
        kwargs["settings"] = pool_settings_from_config(cfg)
        kwargs["auth_home"] = getattr(teleport, "auth_home", None) if watch else None
    return kwargs


async def _cached_indexes(
    cfg: Any, *, database_key: str, db_name: str, db_port: int,
) -> list[list[Any]] | None:
    """All user indexes of one database; cached in-process for an hour."""
    cache_key = (database_key, cfg.postgres.host, db_port, cfg.postgres.user, db_name)
    now = time.monotonic()
    cached = _INDEX_CACHE.get(cache_key)
    if cached is not None and now - cached[0] < _INDEX_CACHE_TTL_SECONDS:
        return cached[1]
    lock = _INDEX_CACHE_LOCKS.setdefault(cache_key, asyncio.Lock())
    async with lock:
        now = time.monotonic()
        cached = _INDEX_CACHE.get(cache_key)
        if cached is not None and now - cached[0] < _INDEX_CACHE_TTL_SECONDS:
            return cached[1]
        failed_at = _INDEX_CACHE_FAILED_AT.get(cache_key)
        if failed_at is not None and now - failed_at < _INDEX_CACHE_FAILURE_BACKOFF_SECONDS:
            return cached[1] if cached is not None else None
        try:
            rows = await run_metadata_query(
                **_metadata_kwargs(cfg, db_name=db_name, db_port=db_port),
                sql=schema_info.ALL_INDEXES_SQL,
                params=None,
                timeout=_INDEX_HINT_STATEMENT_TIMEOUT_SECONDS,
            )
        except Exception:
            _INDEX_CACHE_FAILED_AT[cache_key] = time.monotonic()
            raise
        _INDEX_CACHE[cache_key] = (time.monotonic(), rows)
        _INDEX_CACHE_FAILED_AT.pop(cache_key, None)
        return rows


def _format_index_hint(tables: list[tuple[str, list[str]]]) -> str:
    lines = [
        "",
        "查询涉及表的现有索引（超时多因 WHERE/JOIN/ORDER BY 未命中索引；"
        "可传 explain=true 查看执行计划，或用 db_gateway_schema 查看列与索引）：",
    ]
    for label, defs in tables:
        if defs:
            lines.append(f"- {label}:")
            lines.extend(f"    {d}" for d in defs)
        else:
            lines.append(f"- {label}: 未找到索引（表不存在、无索引或名称是别名/CTE）")
    text = "\n".join(lines)
    if len(text) > _INDEX_HINT_MAX_CHARS:
        text = text[: _INDEX_HINT_MAX_CHARS - 20].rstrip() + "\n...（已截断）"
    return text


async def _timeout_index_hint(
    *, cfg: Any, database_key: str, db_name: str, db_port: int, body: str,
) -> str:
    """Index list of the tables a timed-out query reads, or "" on any failure.

    Never raises (except cancellation): the original timeout error must stay
    intact whatever happens here.
    """
    try:
        refs = referenced_tables(tokenize(body))
    except SqlLexError:
        return ""
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("timeout_index_hint_parse_failed", error=str(e)[:200])
        return ""
    if not refs:
        return ""
    try:
        rows = await asyncio.wait_for(
            _cached_indexes(cfg, database_key=database_key, db_name=db_name, db_port=db_port),
            timeout=_INDEX_HINT_TOTAL_BUDGET_SECONDS,
        )
    except Exception as e:  # includes asyncio.TimeoutError
        logger.warning(
            "timeout_index_hint_failed",
            database=database_key,
            error=f"{type(e).__name__}: {str(e)[:200]}",
        )
        return ""
    if rows is None:
        return ""
    try:
        return _format_index_hint(schema_info.indexes_for_tables(refs, rows))
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("timeout_index_hint_format_failed", error=str(e)[:200])
        return ""


async def execute_schema_lookup(
    *,
    cfg: Any,
    database: str,
    tables: Any,
) -> dict[str, Any]:
    """Columns (information_schema.columns) and indexes (pg_indexes) per table.

    Read-only catalog queries with the table names as bind parameters.
    """
    started = time.perf_counter()

    def elapsed() -> int:
        return int((time.perf_counter() - started) * 1000)

    def fail(code: str, error: str, *, audit: bool = True) -> dict[str, Any]:
        logger.warning("db_gateway_schema_failed", database=database, code=code)
        if audit and database in _DB_KEY_TO_ATTR:
            audit_log(
                log_path=_audit_path(cfg), database=database, sql="", status=code,
                error=error, tool="db_gateway_schema", tables=requested,
            )
        return {
            "success": False,
            "database": database,
            "code": code,
            "error": error,
            "elapsed_ms": elapsed(),
        }

    requested: list[str] = []
    if database not in _DB_KEY_TO_ATTR:
        return fail(
            "UNKNOWN_DATABASE",
            f"unknown database key: {database}. Must be one of {list(_DB_KEY_TO_ATTR)}",
            audit=False,
        )
    try:
        refs = schema_info.parse_table_refs(tables)
    except ValueError as e:
        return fail("INVALID_ARGUMENT", str(e), audit=False)
    requested = [f"{r.schema}.{r.table}" if r.schema else r.table for r in refs]
    names = schema_info.lookup_names(refs)
    db_name, db_port = _db_target(cfg, database)
    timeout = min(
        float(_cfg_number(cfg.postgres, "query_timeout_seconds", _SCHEMA_STATEMENT_TIMEOUT_SECONDS)),
        _SCHEMA_STATEMENT_TIMEOUT_SECONDS,
    )
    kwargs = _metadata_kwargs(cfg, db_name=db_name, db_port=db_port)
    max_concurrent = max(1, int(getattr(cfg.postgres, "max_concurrent_queries", 3) or 3))
    attempts = max(0, int(getattr(cfg.postgres, "teleport_retry_attempts", 1) or 0)) + 1
    delay_attr = "pool_retry_delay_seconds" if kwargs["use_pool"] else "teleport_retry_delay_seconds"
    retry_delay = max(0.0, float(_cfg_number(cfg.postgres, delay_attr, 0.25)))

    async def lookup() -> tuple[list[list[Any]], list[list[Any]]]:
        limiter = await _query_limiter(database, db_port, max_concurrent)
        async with limiter:
            for attempt in range(attempts):
                try:
                    columns = await run_metadata_query(
                        **kwargs, sql=schema_info.COLUMNS_SQL, params=(names,), timeout=timeout,
                    )
                    indexes = await run_metadata_query(
                        **kwargs, sql=schema_info.INDEXES_SQL, params=(names,), timeout=timeout,
                    )
                    return columns, indexes
                except TeleportAuthExpired:
                    if attempt >= attempts - 1:
                        raise
                    if retry_delay:
                        await asyncio.sleep(retry_delay)
        raise AssertionError("unreachable")

    try:
        column_rows, index_rows = await lookup()
    except TeleportAuthExpired as e:
        return fail("TELEPORT_EXPIRED", str(e))
    except asyncio.TimeoutError:
        return fail("QUERY_TIMEOUT", f"查询表结构超时（{timeout:g}s）")
    except Exception as e:
        return fail("QUERY_ERROR", str(e))

    found, missing = schema_info.build_schema_tables(refs, column_rows, index_rows)
    warnings: list[str] = []
    if missing:
        warnings.append(
            f"未找到这些表（或当前账号无权限查看）：{', '.join(missing)}。"
            "表名区分大小写需加双引号；可写 schema.table"
        )
    audit_log(
        log_path=_audit_path(cfg), database=database, sql="", status="OK",
        row_count=len(found), elapsed_ms=elapsed(), tool="db_gateway_schema",
        tables=requested,
    )
    logger.info("db_gateway_schema", database=database, tables=requested, found=len(found))
    return {
        "success": True,
        "database": database,
        "tables": found,
        "not_found": missing,
        "warnings": warnings,
        "elapsed_ms": elapsed(),
        "code": "",
        "error": None,
    }
