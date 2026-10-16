"""Core read-only DB gateway execution logic."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from db_gateway.postgres import (
    PsqlResult,
    PsqlSafetyError,
    TeleportAuthExpired,
    audit_log,
    run_psql,
    validate_read_only_sql,
)


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

_QUERY_STATE_LOCK = asyncio.Lock()
_QUERY_LIMITERS: dict[tuple[str, int, int], asyncio.Semaphore] = {}
_INFLIGHT_QUERIES: dict[tuple[Any, ...], asyncio.Task[PsqlResult]] = {}


@dataclass
class GatewayQueryResult:
    success: bool
    csv: str = ""
    row_count: int = 0
    elapsed_ms: int = 0
    truncated: bool = False
    code: str = ""
    error: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "csv": self.csv,
            "row_count": self.row_count,
            "elapsed_ms": self.elapsed_ms,
            "truncated": self.truncated,
            "code": self.code,
            "error": self.error,
        }


def _audit_path(cfg: Any) -> Path:
    return (
        Path(cfg.resources.base_dir)
        / cfg.resources.state_dir
        / "logs"
        / "db_gateway_audit.log"
    )


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
) -> GatewayQueryResult:
    """Validate and execute one read-only query through local DB proxy."""
    sql = (sql or "").strip()
    if database not in _DB_KEY_TO_ATTR:
        return GatewayQueryResult(
            success=False,
            code="UNKNOWN_DATABASE",
            error=f"unknown database key: {database}. Must be one of {list(_DB_KEY_TO_ATTR)}",
        )

    try:
        validate_read_only_sql(sql)
    except PsqlSafetyError as e:
        return GatewayQueryResult(
            success=False,
            code="SQL_SAFETY_ERROR",
            error=f"SQL safety check failed: {e}",
        )

    db_name = getattr(cfg.postgres, _DB_KEY_TO_ATTR[database])
    db_port = getattr(
        cfg.postgres,
        _DB_KEY_TO_PORT_ATTR[database],
        cfg.postgres.port,
    )

    try:
        result = await _run_psql_controlled(
            cfg=cfg,
            database_key=database,
            db_name=db_name,
            db_port=db_port,
            sql=sql,
        )
    except TeleportAuthExpired as e:
        audit_log(
            log_path=_audit_path(cfg),
            database=database,
            sql=sql,
            status="TELEPORT_EXPIRED",
            ticket_id=ticket_id,
            error=str(e),
        )
        return GatewayQueryResult(
            success=False,
            code="TELEPORT_EXPIRED",
            error=str(e),
        )
    except Exception as e:
        audit_log(
            log_path=_audit_path(cfg),
            database=database,
            sql=sql,
            status="ERROR",
            ticket_id=ticket_id,
            error=str(e),
        )
        return GatewayQueryResult(
            success=False,
            code="QUERY_ERROR",
            error=str(e),
        )

    audit_log(
        log_path=_audit_path(cfg),
        database=database,
        sql=sql,
        status="OK",
        ticket_id=ticket_id,
        row_count=result.row_count,
        elapsed_ms=result.elapsed_ms,
    )
    return GatewayQueryResult(
        success=True,
        csv=result.csv,
        row_count=result.row_count,
        elapsed_ms=result.elapsed_ms,
        truncated=result.truncated,
    )


async def _run_psql_controlled(
    *,
    cfg: Any,
    database_key: str,
    db_name: str,
    db_port: int,
    sql: str,
) -> PsqlResult:
    max_concurrent = max(1, int(getattr(cfg.postgres, "max_concurrent_queries", 3) or 3))
    singleflight_enabled = bool(getattr(cfg.postgres, "singleflight_enabled", True))
    singleflight_key = (
        database_key,
        cfg.postgres.host,
        db_port,
        cfg.postgres.user,
        db_name,
        sql,
        int(cfg.postgres.max_rows),
    )
    if not singleflight_enabled:
        return await _run_psql_with_limiter(
            cfg=cfg,
            database_key=database_key,
            db_name=db_name,
            db_port=db_port,
            sql=sql,
            max_concurrent=max_concurrent,
        )

    owner = False
    async with _QUERY_STATE_LOCK:
        task = _INFLIGHT_QUERIES.get(singleflight_key)
        if task is None:
            task = asyncio.create_task(
                _run_psql_with_limiter(
                    cfg=cfg,
                    database_key=database_key,
                    db_name=db_name,
                    db_port=db_port,
                    sql=sql,
                    max_concurrent=max_concurrent,
                )
            )
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
) -> PsqlResult:
    limiter = await _query_limiter(database_key, db_port, max_concurrent)
    async with limiter:
        return await _run_psql_with_retries(
            cfg=cfg,
            db_name=db_name,
            db_port=db_port,
            sql=sql,
        )


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
) -> PsqlResult:
    attempts = max(0, int(getattr(cfg.postgres, "teleport_retry_attempts", 1) or 0)) + 1
    retry_delay = max(0.0, float(getattr(cfg.postgres, "teleport_retry_delay_seconds", 0.25) or 0.0))
    last_error: TeleportAuthExpired | None = None
    for attempt in range(attempts):
        try:
            return await run_psql(
                host=cfg.postgres.host,
                port=db_port,
                user=cfg.postgres.user,
                password=cfg.postgres.password,
                database=db_name,
                sql=sql,
                timeout=cfg.postgres.query_timeout_seconds,
                max_rows=cfg.postgres.max_rows,
            )
        except TeleportAuthExpired as e:
            last_error = e
            if attempt >= attempts - 1:
                raise
            if retry_delay:
                await asyncio.sleep(retry_delay)
    assert last_error is not None
    raise last_error
