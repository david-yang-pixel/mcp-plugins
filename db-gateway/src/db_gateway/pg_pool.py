"""Persistent PostgreSQL connections (psycopg pool) for the DB Gateway.

Every psql invocation opens a new connection through the local
``tsh proxy db --tunnel`` port, and that handshake (TLS to the Teleport proxy,
DB agent, Postgres auth) costs about two seconds per query. A pool keeps the
connections open, so a query only pays its own round trips.

Teleport certificates are renewed outside this service: tsh-auth-watchdog runs
``tsh logout --all`` + ``tsh login`` and the tsh-cert.path unit then restarts
the tsh-proxy-db* tunnels, which drops every connection made through them.
The pool therefore:

* watches ``<teleport.auth_home>/keys`` and drains all pools when anything in
  it changes, so the first query after a renewal does not hit a dead socket;
* treats connection-level failures as ``TeleportAuthExpired`` and drains the
  affected pool, letting the existing retry loop reconnect after a short delay;
* recycles idle connections (``pool_max_idle_seconds``) and probes ones that
  sat idle longer than ``pool_check_idle_seconds`` before handing them out.

Read-only guarantees match the psql path: the validated SQL runs inside a
``READ ONLY`` transaction with ``SET LOCAL statement_timeout`` and is always
rolled back, so no session state survives between queries.
"""
from __future__ import annotations

import asyncio
import os
import time
import weakref
from dataclasses import dataclass
from typing import Any, Optional

import psycopg
from psycopg import postgres
from psycopg.conninfo import make_conninfo
from psycopg.types.string import TextLoader
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from db_gateway.postgres import (
    PsqlResult,
    TeleportAuthExpired,
    rows_to_csv,
    validate_read_only_sql,
    wrap_query,
)

try:
    from db_gateway.log import get_logger
    logger = get_logger(module="integrations.pg_pool")
except Exception:  # pragma: no cover
    import logging as _logging
    logger = _logging.getLogger("integrations.pg_pool")  # type: ignore[assignment]


_CURSOR_NAME = "_db_gateway_cursor"
# Extra wall-clock budget on top of statement timeouts for acquiring a
# connection and network round trips.
_CLIENT_TIMEOUT_MARGIN_SECONDS = 15.0


@dataclass(frozen=True)
class PoolKey:
    host: str
    port: int
    user: str
    database: str


@dataclass(frozen=True)
class PoolSettings:
    min_size: int = 1
    max_size: int = 3
    max_idle_seconds: float = 240.0
    max_lifetime_seconds: float = 1800.0
    check_idle_seconds: float = 60.0
    acquire_timeout_seconds: float = 15.0
    connect_timeout_seconds: int = 10


def pool_settings_from_config(cfg: Any) -> PoolSettings:
    pg = cfg.postgres
    max_size = max(1, int(getattr(pg, "max_concurrent_queries", 3) or 3))
    return PoolSettings(
        min_size=max(0, min(int(getattr(pg, "pool_min_size", 1)), max_size)),
        max_size=max_size,
        max_idle_seconds=float(getattr(pg, "pool_max_idle_seconds", 240.0)),
        max_lifetime_seconds=float(getattr(pg, "pool_max_lifetime_seconds", 1800.0)),
        check_idle_seconds=float(getattr(pg, "pool_check_idle_seconds", 60.0)),
        acquire_timeout_seconds=float(getattr(pg, "pool_acquire_timeout_seconds", 15.0)),
    )


def keys_signature(auth_home: str) -> Optional[tuple[int, float]]:
    """(file count, newest mtime) of ``<auth_home>/keys``; None if absent.

    The whole directory is watched instead of one certificate file because the
    file names depend on the tsh version, cluster and user.
    """
    keys_dir = os.path.join(auth_home, "keys")
    count = 0
    newest = 0.0
    try:
        for root, _dirs, files in os.walk(keys_dir):
            for name in files:
                try:
                    mtime = os.stat(os.path.join(root, name)).st_mtime
                except OSError:
                    continue
                count += 1
                newest = max(newest, mtime)
    except OSError:
        return None
    if count == 0 and not os.path.isdir(keys_dir):
        return None
    return count, newest


class PoolManager:
    """One psycopg pool per (host, port, user, database)."""

    def __init__(self) -> None:
        self._pools: dict[PoolKey, AsyncConnectionPool] = {}
        # Pending pool.open(wait=True) per key, awaited outside the lock so a
        # slow tunnel for one database does not block queries to the others.
        self._opening: dict[PoolKey, asyncio.Task] = {}
        self._lock = asyncio.Lock()
        self._keys_signature: Optional[tuple[int, float]] = None
        self._last_used: "weakref.WeakKeyDictionary[Any, float]" = weakref.WeakKeyDictionary()
        self._type_names: dict[tuple[int, int], str] = {}
        self._connections_seen: dict[PoolKey, int] = {}

    async def pool_for(
        self,
        key: PoolKey,
        *,
        password: str,
        settings: PoolSettings,
        auth_home: Optional[str],
    ) -> AsyncConnectionPool:
        async with self._lock:
            if auth_home:
                signature = keys_signature(auth_home)
                if self._keys_signature is not None and signature != self._keys_signature:
                    await self._close_all_locked("teleport keys changed")
                self._keys_signature = signature
            pool = self._pools.get(key)
            if pool is None:
                pool = self._new_pool(key, password=password, settings=settings)
                self._pools[key] = pool
                self._connections_seen[key] = 0
                # Wait for the min_size connections: opening without waiting lets
                # the first query trigger a second, parallel handshake.
                self._opening[key] = asyncio.create_task(
                    pool.open(wait=settings.min_size > 0, timeout=settings.acquire_timeout_seconds)
                )
                logger.info(
                    "pg_pool_opened",
                    host=key.host, port=key.port, database=key.database,
                    min_size=settings.min_size, max_size=settings.max_size,
                )
            opening = self._opening.get(key)
        if opening is not None:
            try:
                await asyncio.shield(opening)
            except Exception:
                await self.drain(key, "initial connections failed")
                raise
            finally:
                if opening.done():
                    self._opening.pop(key, None)
        return pool

    async def drain(self, key: PoolKey, reason: str) -> None:
        async with self._lock:
            pool = self._pools.pop(key, None)
            self._opening.pop(key, None)
        if pool is not None:
            logger.warning("pg_pool_drained", database=key.database, port=key.port, reason=reason)
            await _close_quietly(pool)

    async def close_all(self, reason: str = "shutdown") -> None:
        async with self._lock:
            await self._close_all_locked(reason)

    async def _close_all_locked(self, reason: str) -> None:
        pools, self._pools = list(self._pools.items()), {}
        self._opening.clear()
        for key, pool in pools:
            logger.warning("pg_pool_drained", database=key.database, port=key.port, reason=reason)
            await _close_quietly(pool)

    def open_pools(self) -> list[PoolKey]:
        return list(self._pools)

    def new_connections(self, key: PoolKey, pool: AsyncConnectionPool) -> int:
        """Connections the pool created since the previous call for this key."""
        created = int(pool.get_stats().get("connections_num", 0))
        previous = self._connections_seen.get(key, 0)
        self._connections_seen[key] = created
        return max(0, created - previous)

    def _new_pool(self, key: PoolKey, *, password: str, settings: PoolSettings) -> AsyncConnectionPool:
        conninfo = make_conninfo(
            host=key.host,
            port=key.port,
            user=key.user,
            password=password or None,
            dbname=key.database,
            # `tsh proxy db --tunnel` already does mTLS; same as the psql path.
            sslmode="disable",
            connect_timeout=settings.connect_timeout_seconds,
            application_name="db-gateway",
            client_encoding="UTF8",
        )
        return AsyncConnectionPool(
            conninfo,
            min_size=settings.min_size,
            max_size=settings.max_size,
            max_idle=settings.max_idle_seconds,
            max_lifetime=settings.max_lifetime_seconds,
            timeout=settings.acquire_timeout_seconds,
            kwargs={"autocommit": True},
            configure=self._configure,
            check=self._make_check(settings.check_idle_seconds),
            # No `reset` callback: with one, returned connections go through a
            # background worker and are not immediately reusable, so the next
            # query would grow the pool (and pay a new handshake) instead.
            open=False,
            name=f"db-gateway-{key.database}-{key.port}",
        )

    async def _configure(self, conn: psycopg.AsyncConnection) -> None:
        # Return every value as PostgreSQL's text output (what psql prints), so
        # the csv/json results are identical whichever driver ran the query.
        for info in postgres.types:
            conn.adapters.register_loader(info.oid, TextLoader)
            array_oid = getattr(info, "array_oid", 0)
            if array_oid:
                conn.adapters.register_loader(array_oid, TextLoader)
        self._last_used[conn] = time.monotonic()

    def _make_check(self, check_idle_seconds: float):
        async def check(conn: psycopg.AsyncConnection) -> None:
            # Probing every checkout would cost a round trip per query; only
            # connections idle long enough to have been dropped are probed.
            idle = time.monotonic() - self._last_used.get(conn, 0.0)
            if idle >= check_idle_seconds:
                await conn.execute("")

        return check

    def touch(self, conn: psycopg.AsyncConnection) -> None:
        self._last_used[conn] = time.monotonic()

    def cached_type_name(self, oid: int, typmod: int) -> Optional[str]:
        return self._type_names.get((oid, typmod))

    def cache_type_name(self, oid: int, typmod: int, name: str) -> None:
        self._type_names[(oid, typmod)] = name


async def _close_quietly(pool: AsyncConnectionPool) -> None:
    try:
        await pool.close(timeout=5)
    except Exception as e:  # noqa: BLE001 - closing must never fail a query
        logger.warning("pg_pool_close_failed", error=str(e))


MANAGER = PoolManager()


def _ms(seconds: float) -> float:
    return round(seconds * 1000, 1)


def _is_connection_failure(error: BaseException, conn: Optional[psycopg.AsyncConnection]) -> bool:
    if isinstance(error, psycopg.errors.QueryCanceled):
        return False
    if conn is not None and conn.broken:
        return True
    return isinstance(error, psycopg.OperationalError) and getattr(error, "sqlstate", None) is None


def _error_text(error: psycopg.Error) -> str:
    diag = getattr(error, "diag", None)
    primary = getattr(diag, "message_primary", None) if diag is not None else None
    text = primary or str(error).strip() or type(error).__name__
    return f"ERROR:  {text}"


async def _column_types(
    manager: PoolManager, conn: psycopg.AsyncConnection, pgresult: Any,
) -> list[str]:
    fields = [(pgresult.ftype(i), pgresult.fmod(i)) for i in range(pgresult.nfields)]
    missing = sorted({f for f in fields if manager.cached_type_name(*f) is None})
    if missing:
        cur = await conn.execute(
            "SELECT o, m, format_type(o, m) FROM unnest(%s::oid[], %s::int4[]) AS t(o, m)",
            ([o for o, _ in missing], [m for _, m in missing]),
        )
        for oid, typmod, name in await cur.fetchall():
            manager.cache_type_name(int(oid), int(typmod), name or "")
    return [manager.cached_type_name(*f) or "" for f in fields]


async def run_pool_query(
    *,
    host: str,
    port: int,
    user: str,
    password: str,
    database: str,
    sql: str,
    timeout: float,
    max_rows: int,
    offset: int = 0,
    limit: int | None = None,
    count_timeout: float = 0,
    describe: bool = False,
    settings: PoolSettings | None = None,
    auth_home: str | None = None,
    manager: PoolManager | None = None,
) -> PsqlResult:
    """Execute one read-only query page on a pooled connection.

    Same contract as ``postgres.run_psql``: cursor in a READ ONLY transaction,
    ``MOVE offset`` -> ``FETCH limit+1`` -> ``MOVE ALL`` for ``total_rows``. The
    count runs only when the page is full; otherwise the total is exact.

    Raises:
        PsqlSafetyError: SQL failed read-only validation
        TeleportAuthExpired: no usable connection (tunnel down, auth expired)
        asyncio.TimeoutError: statement timeout or client-side deadline
        RuntimeError: other query errors
    """
    body = validate_read_only_sql(sql)
    wrapped = wrap_query(body)
    validate_read_only_sql(wrapped)

    manager = manager or MANAGER
    settings = settings or PoolSettings()
    max_rows = max(1, int(max_rows))
    page_limit = max(1, min(int(limit) if limit else max_rows, max_rows))
    offset = max(0, int(offset or 0))
    count_timeout = max(0.0, float(count_timeout or 0))
    key = PoolKey(host=host, port=int(port), user=user, database=database)
    deadline = float(timeout) + count_timeout + _CLIENT_TIMEOUT_MARGIN_SECONDS

    started = time.perf_counter()
    try:
        return await asyncio.wait_for(
            _run(
                manager, key, password=password, settings=settings, auth_home=auth_home,
                wrapped=wrapped, timeout=float(timeout), page_limit=page_limit,
                offset=offset, count_timeout=count_timeout, describe=describe,
                started=started,
            ),
            timeout=deadline,
        )
    except asyncio.TimeoutError:
        # The connection may be mid-query; start clean for the next caller.
        await manager.drain(key, "client deadline exceeded")
        raise


async def _run(
    manager: PoolManager,
    key: PoolKey,
    *,
    password: str,
    settings: PoolSettings,
    auth_home: str | None,
    wrapped: str,
    timeout: float,
    page_limit: int,
    offset: int,
    count_timeout: float,
    describe: bool,
    started: float,
) -> PsqlResult:
    conn: Optional[psycopg.AsyncConnection] = None
    timing: dict[str, float] = {}
    try:
        pool = await manager.pool_for(key, password=password, settings=settings, auth_home=auth_home)
        async with pool.connection() as conn:
            acquired = time.perf_counter()
            timing["pool_acquire_ms"] = _ms(acquired - started)
            timing["pool_new_connections"] = float(manager.new_connections(key, pool))
            try:
                async with conn.transaction(force_rollback=True):
                    return await _query_in_transaction(
                        manager, conn, wrapped=wrapped, timeout=timeout,
                        page_limit=page_limit, offset=offset,
                        count_timeout=count_timeout, describe=describe,
                        timing=timing, started=started,
                    )
            finally:
                manager.touch(conn)
    except PoolTimeout as e:
        await manager.drain(key, "no connection within acquire timeout")
        raise TeleportAuthExpired(
            f"could not connect to server: no PostgreSQL connection to {key.host}:{key.port} "
            f"within {settings.acquire_timeout_seconds:g}s ({e})"
        ) from e
    except psycopg.errors.QueryCanceled as e:
        raise asyncio.TimeoutError(_error_text(e)) from e
    except psycopg.Error as e:
        if _is_connection_failure(e, conn):
            await manager.drain(key, f"connection failure: {str(e).strip()[:200]}")
            raise TeleportAuthExpired(f"connection reset: {str(e).strip()}") from e
        raise RuntimeError(_error_text(e)) from e
    except OSError as e:
        await manager.drain(key, f"connection failure: {e}")
        raise TeleportAuthExpired(f"Connection refused: {e}") from e


async def _query_in_transaction(
    manager: PoolManager,
    conn: psycopg.AsyncConnection,
    *,
    wrapped: str,
    timeout: float,
    page_limit: int,
    offset: int,
    count_timeout: float,
    describe: bool,
    timing: dict[str, float],
    started: float,
) -> PsqlResult:
    query_started = time.perf_counter()
    await conn.execute("SET TRANSACTION READ ONLY")
    await conn.execute(f"SET LOCAL statement_timeout = {int(timeout * 1000)}")
    await conn.execute(f"DECLARE {_CURSOR_NAME} NO SCROLL CURSOR FOR {wrapped}")
    skipped = 0
    if offset > 0:
        moved = await conn.execute(f"MOVE FORWARD {int(offset)} IN {_CURSOR_NAME}")
        skipped = max(0, moved.rowcount)
    cur = await conn.execute(f"FETCH FORWARD {page_limit + 1} FROM {_CURSOR_NAME}")
    data_rows = [list(row) for row in await cur.fetchall()]
    columns = [col.name for col in cur.description or []]
    pgresult = cur.pgresult
    timing["query_ms"] = _ms(time.perf_counter() - query_started)

    column_types: list[str] | None = None
    if describe and pgresult is not None:
        describe_started = time.perf_counter()
        column_types = await _column_types(manager, conn, pgresult)
        timing["describe_ms"] = _ms(time.perf_counter() - describe_started)

    fetched = len(data_rows)
    has_more = fetched > page_limit
    rows = data_rows[:page_limit]
    total_rows: int | None = None
    total_note: str | None = None
    if not has_more:
        # Cursor exhausted inside this page: exact total without counting.
        total_rows = skipped + fetched
    elif count_timeout > 0:
        count_started = time.perf_counter()
        try:
            await conn.execute(f"SET LOCAL statement_timeout = {int(count_timeout * 1000)}")
            rest = await conn.execute(f"MOVE FORWARD ALL IN {_CURSOR_NAME}")
            total_rows = skipped + fetched + max(0, rest.rowcount)
        except psycopg.errors.QueryCanceled:
            total_note = (
                f"计数超时（超过 {count_timeout:g}s），总行数未知；可加过滤条件或用 next_offset 继续翻页"
            )
        except psycopg.Error as e:
            if _is_connection_failure(e, conn):
                raise
            total_note = f"计数阶段失败，总行数未知：{_error_text(e)}"
        timing["count_ms"] = _ms(time.perf_counter() - count_started)
    else:
        total_note = "未计算总行数（count_timeout_seconds=0）"

    parse_started = time.perf_counter()
    csv_text = rows_to_csv(columns, rows) if columns else ""
    timing["parse_ms"] = _ms(time.perf_counter() - parse_started)
    elapsed = time.perf_counter() - started
    return PsqlResult(
        csv=csv_text,
        row_count=len(rows),
        elapsed_ms=int(elapsed * 1000),
        truncated=has_more,
        columns=columns,
        rows=rows,
        column_types=column_types,
        has_more=has_more,
        total_rows=total_rows,
        total_rows_note=total_note,
        offset_skipped=skipped,
        timing_ms=timing,
    )
