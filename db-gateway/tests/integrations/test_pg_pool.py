"""Pooled driver against a real PostgreSQL server.

Uses DB_GATEWAY_TEST_PG=host:port:user when set; otherwise starts a throwaway
cluster with initdb (skipped when PostgreSQL server binaries are missing or the
tests run as root, where initdb refuses to run).
"""
from __future__ import annotations

import asyncio
import glob
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import psycopg
import pytest

from db_gateway.pg_pool import PoolManager, PoolSettings, run_pool_query
from db_gateway.postgres import TeleportAuthExpired, run_psql


def _initdb() -> str | None:
    found = shutil.which("initdb")
    if found:
        return found
    candidates = sorted(glob.glob("/usr/lib/postgresql/*/bin/initdb"))
    return candidates[-1] if candidates else None


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def pg_server(tmp_path_factory):
    configured = os.environ.get("DB_GATEWAY_TEST_PG")
    if configured:
        host, port, user = configured.split(":")
        yield {"host": host, "port": int(port), "user": user}
        return
    initdb = _initdb()
    if initdb is None:
        pytest.skip("PostgreSQL server binaries not found; set DB_GATEWAY_TEST_PG=host:port:user")
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("initdb cannot run as root; set DB_GATEWAY_TEST_PG=host:port:user")
    bindir = Path(initdb).parent
    data = tmp_path_factory.mktemp("pgdata")
    port = _free_port()
    subprocess.run([initdb, "-D", str(data), "-A", "trust", "-U", "postgres"], check=True, capture_output=True)
    subprocess.run(
        [
            str(bindir / "pg_ctl"), "-D", str(data), "-l", str(data / "log"), "-w",
            "-o", f"-p {port} -k {data} -c listen_addresses=127.0.0.1", "start",
        ],
        check=True,
        capture_output=True,
    )
    try:
        yield {"host": "127.0.0.1", "port": port, "user": "postgres"}
    finally:
        subprocess.run([str(bindir / "pg_ctl"), "-D", str(data), "-m", "immediate", "stop"], capture_output=True)


@pytest.fixture(scope="module")
def database(pg_server):
    name = f"dbgw_test_{os.getpid()}"
    admin = psycopg.connect(
        host=pg_server["host"], port=pg_server["port"], user=pg_server["user"],
        dbname="postgres", autocommit=True,
    )
    admin.execute(f"DROP DATABASE IF EXISTS {name}")
    admin.execute(f"CREATE DATABASE {name}")
    with psycopg.connect(
        host=pg_server["host"], port=pg_server["port"], user=pg_server["user"],
        dbname=name, autocommit=True,
    ) as conn:
        conn.execute("CREATE SEQUENCE seq")
        conn.execute("CREATE TABLE t AS SELECT g AS id, 'row ' || g AS label FROM generate_series(1, 25) g")
    yield {**pg_server, "database": name}
    admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
    admin.close()


def _kwargs(db, **overrides):
    base = {
        "host": db["host"],
        "port": db["port"],
        "user": db["user"],
        "password": "",
        "database": db["database"],
        "timeout": 10,
        "max_rows": 1000,
        "count_timeout": 5,
    }
    base.update(overrides)
    return base


@pytest.fixture
async def manager():
    mgr = PoolManager()
    yield mgr
    await mgr.close_all()


TYPES_SQL = (
    "SELECT 1 AS i, 1.50::numeric AS n, true AS b, NULL::text AS nul, ''::text AS empty, "
    "'2026-01-02 03:04:05+00'::timestamptz AS ts, '{\"a\":1}'::jsonb AS j, ARRAY[1,2] AS arr, "
    "E'a,b\\nc' AS multi, '\\x0102'::bytea AS by, interval '1 day 2 hours' AS iv, "
    "'50%' AS pct, 1e20::float8 AS f, '12.5'::numeric(6,2) AS money"
)


@pytest.mark.skipif(shutil.which("psql") is None, reason="psql not installed")
@pytest.mark.parametrize(
    ("sql", "extra"),
    [
        (TYPES_SQL, {}),
        ("SELECT id, label FROM t ORDER BY id", {"offset": 10, "limit": 5}),
        ("SELECT id FROM t ORDER BY id", {"offset": 20, "limit": 10}),
        ("SELECT id FROM t WHERE id < 0", {}),
        ("(SELECT 1 AS x) UNION ALL (SELECT 2)", {}),
    ],
)
async def test_pool_results_match_psql(database, manager, sql, extra):
    for describe in (False, True):
        kwargs = _kwargs(database, sql=sql, describe=describe, **extra)
        expected = await run_psql(**kwargs)
        actual = await run_pool_query(manager=manager, **kwargs)
        assert actual.csv == expected.csv
        assert actual.columns == expected.columns
        assert actual.rows == expected.rows
        assert actual.has_more == expected.has_more
        assert actual.total_rows == expected.total_rows
        assert actual.offset_skipped == expected.offset_skipped
        if describe:
            assert actual.column_types == expected.column_types


async def test_connection_is_reused(database, manager):
    first = await run_pool_query(manager=manager, **_kwargs(database, sql="SELECT 1"))
    second = await run_pool_query(manager=manager, **_kwargs(database, sql="SELECT 2"))

    assert first.timing_ms["pool_new_connections"] == 1
    assert second.timing_ms["pool_new_connections"] == 0
    assert second.rows == [["2"]]


async def test_transaction_is_read_only_and_rolled_back(database, manager):
    with pytest.raises(RuntimeError, match="read-only transaction"):
        await run_pool_query(manager=manager, **_kwargs(database, sql="SELECT nextval('seq')"))
    # The connection is clean afterwards (no aborted transaction left behind).
    ok = await run_pool_query(manager=manager, **_kwargs(database, sql="SELECT current_setting('transaction_read_only')"))
    assert ok.rows == [["on"]]


async def test_statement_timeout_is_a_timeout(database, manager):
    with pytest.raises(asyncio.TimeoutError):
        await run_pool_query(manager=manager, **_kwargs(database, sql="SELECT pg_sleep(3)", timeout=1))


async def test_query_error_is_reported(database, manager):
    with pytest.raises(RuntimeError, match="does not exist"):
        await run_pool_query(manager=manager, **_kwargs(database, sql="SELECT * FROM missing_table"))


async def test_count_timeout_keeps_the_page(database, manager):
    result = await run_pool_query(
        manager=manager,
        **_kwargs(
            database,
            sql="SELECT g, pg_sleep(0.01) FROM generate_series(1, 1000) g",
            limit=5,
            count_timeout=0.3,
        ),
    )
    assert result.row_count == 5
    assert result.has_more is True
    assert result.total_rows is None
    assert "计数超时" in result.total_rows_note


def _terminate_pool_backends(db) -> int:
    with psycopg.connect(
        host=db["host"], port=db["port"], user=db["user"], dbname="postgres", autocommit=True,
    ) as admin:
        rows = admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE application_name = 'db-gateway' AND datname = %s",
            (db["database"],),
        ).fetchall()
    time.sleep(0.2)
    return len(rows)


async def test_dropped_connection_fails_once_then_reconnects(database, manager):
    # Like a tsh-proxy-db restart: the pooled connection dies while idle.
    await run_pool_query(manager=manager, **_kwargs(database, sql="SELECT 1"))
    assert _terminate_pool_backends(database) >= 1

    with pytest.raises(TeleportAuthExpired):
        await run_pool_query(manager=manager, **_kwargs(database, sql="SELECT 1"))
    again = await run_pool_query(manager=manager, **_kwargs(database, sql="SELECT 3"))
    assert again.rows == [["3"]]


async def test_idle_check_replaces_dropped_connection_transparently(database, manager):
    settings = PoolSettings(check_idle_seconds=0)
    await run_pool_query(manager=manager, settings=settings, **_kwargs(database, sql="SELECT 1"))
    assert _terminate_pool_backends(database) >= 1

    result = await run_pool_query(manager=manager, settings=settings, **_kwargs(database, sql="SELECT 4"))
    assert result.rows == [["4"]]


async def test_teleport_key_change_drains_the_pool(database, manager, tmp_path):
    keys = tmp_path / "keys" / "teleport.example.com"
    keys.mkdir(parents=True)
    cert = keys / "agent-x509.pem"
    cert.write_text("old")
    kwargs = _kwargs(database, sql="SELECT 1", auth_home=str(tmp_path))

    await run_pool_query(manager=manager, **kwargs)
    reused = await run_pool_query(manager=manager, **kwargs)
    cert.write_text("renewed")
    os.utime(cert, (time.time() + 5, time.time() + 5))
    after_renewal = await run_pool_query(manager=manager, **kwargs)

    assert reused.timing_ms["pool_new_connections"] == 0
    assert after_renewal.timing_ms["pool_new_connections"] == 1


async def test_unreachable_tunnel_is_teleport_expired(manager):
    settings = PoolSettings(acquire_timeout_seconds=1.5, connect_timeout_seconds=1)
    with pytest.raises(TeleportAuthExpired):
        await run_pool_query(
            manager=manager,
            settings=settings,
            host="127.0.0.1",
            port=_free_port(),
            user="nobody",
            password="",
            database="nothing",
            sql="SELECT 1",
            timeout=5,
            max_rows=10,
        )


async def test_statement_timeout_keeps_the_pool(database, manager):
    """Bugbot: a server statement_timeout drained the whole pool."""
    await run_pool_query(manager=manager, **_kwargs(database, sql="SELECT 1"))
    with pytest.raises(asyncio.TimeoutError):
        await run_pool_query(manager=manager, **_kwargs(database, sql="SELECT pg_sleep(3)", timeout=1))

    after = await run_pool_query(manager=manager, **_kwargs(database, sql="SELECT 5"))
    assert after.rows == [["5"]]
    assert after.timing_ms["pool_new_connections"] == 0


async def test_drain_of_an_old_pool_keeps_the_new_one(database, manager):
    """Bugbot: a query failing on an old pool must not close a newer pool."""
    kwargs = _kwargs(database, sql="SELECT 1")
    await run_pool_query(manager=manager, **kwargs)
    key = manager.open_pools()[0]
    old = manager._pools[key]
    await manager.drain(key, "test", pool=old)
    await run_pool_query(manager=manager, **kwargs)
    new = manager._pools[key]

    await manager.drain(key, "late failure on the old pool", pool=old)

    assert manager._pools[key] is new
    reused = await run_pool_query(manager=manager, **kwargs)
    assert reused.timing_ms["pool_new_connections"] == 0


async def test_pool_reset_between_lookup_and_checkout_is_retried(database, manager, monkeypatch):
    """Bugbot: PoolClosed from a concurrently reset pool became QUERY_ERROR."""
    kwargs = _kwargs(database, sql="SELECT 6")
    await run_pool_query(manager=manager, **kwargs)
    real_pool_for = manager.pool_for
    calls = {"n": 0}

    async def racing_pool_for(key, **kw):
        pool = await real_pool_for(key, **kw)
        calls["n"] += 1
        if calls["n"] == 1:
            await manager.drain(key, "concurrent reset", pool=pool)  # closes the pool just handed out
        return pool

    monkeypatch.setattr(manager, "pool_for", racing_pool_for)
    result = await run_pool_query(manager=manager, **kwargs)

    assert result.rows == [["6"]]
    assert calls["n"] == 2


async def test_session_settings_do_not_leak_to_the_next_query(database, manager):
    """Security review: session GUCs set by one caller must not reach the next.

    The forced ROLLBACK undoes session-level SET/set_config made inside the
    transaction; advisory locks (which survive ROLLBACK) are rejected upfront.
    """
    settings = PoolSettings(min_size=1, max_size=1)  # same backend for both queries
    changed = await run_pool_query(
        manager=manager, settings=settings,
        **_kwargs(database, sql="SELECT set_config('search_path', 'evil', false), set_config('work_mem', '1MB', false)"),
    )
    assert changed.rows == [["evil", "1MB"]]

    after = await run_pool_query(
        manager=manager, settings=settings,
        **_kwargs(database, sql="SELECT current_setting('search_path'), current_setting('work_mem') <> '1MB'"),
    )
    assert after.timing_ms["pool_new_connections"] == 0
    assert after.rows == [['"$user", public', "t"]]

    from db_gateway.postgres import PsqlSafetyError

    with pytest.raises(PsqlSafetyError):
        await run_pool_query(manager=manager, settings=settings, **_kwargs(database, sql="SELECT pg_advisory_lock(42)"))


def _advisory_locks_sql() -> str:
    return "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND pid = pg_backend_pid()"


@pytest.mark.parametrize(
    "sql",
    [
        # Security review: Unicode-escaped identifiers slip past the denylist ...
        'SELECT U&"\\0070g_advisory_lock"(7)',
        # ... and query_to_xml runs SQL from a string the validator cannot see.
        "SELECT query_to_xml('select pg_advisory_lock(8)', false, false, '')",
    ],
)
async def test_advisory_locks_never_survive_a_pooled_query(database, manager, sql):
    settings = PoolSettings(min_size=1, max_size=1)  # same backend for both queries
    await run_pool_query(manager=manager, settings=settings, **_kwargs(database, sql=sql))

    after = await run_pool_query(manager=manager, settings=settings, **_kwargs(database, sql=_advisory_locks_sql()))

    assert after.timing_ms["pool_new_connections"] == 0
    assert after.rows == [["0"]]


async def test_advisory_lock_is_released_after_a_failed_query(database, manager):
    settings = PoolSettings(min_size=1, max_size=1)
    with pytest.raises(RuntimeError):
        await run_pool_query(
            manager=manager, settings=settings,
            # The division by zero only happens after query_to_xml took the lock.
            **_kwargs(
                database,
                sql="SELECT 1 / (CASE WHEN query_to_xml('select pg_advisory_lock(9)', false, false, '') IS NOT NULL THEN 0 END)",
            ),
        )

    after = await run_pool_query(manager=manager, settings=settings, **_kwargs(database, sql=_advisory_locks_sql()))
    assert after.rows == [["0"]]


async def test_warm_query_uses_two_round_trips(database, manager, monkeypatch):
    """Each execute() is one round trip; the pool must not send statement by statement."""
    await run_pool_query(manager=manager, **_kwargs(database, sql="SELECT 1"))
    calls: list[str] = []
    real_cursor_execute = psycopg.AsyncCursor.execute
    real_conn_execute = psycopg.AsyncConnection.execute

    async def cursor_execute(self, query, *args, **kwargs):
        calls.append(str(query))
        return await real_cursor_execute(self, query, *args, **kwargs)

    async def conn_execute(self, query, *args, **kwargs):
        calls.append(str(query))
        return await real_conn_execute(self, query, *args, **kwargs)

    monkeypatch.setattr(psycopg.AsyncCursor, "execute", cursor_execute)
    monkeypatch.setattr(psycopg.AsyncConnection, "execute", conn_execute)

    small = await run_pool_query(manager=manager, **_kwargs(database, sql="SELECT id FROM t ORDER BY id"))
    assert small.total_rows == 25
    assert len(calls) == 2

    calls.clear()
    paged = await run_pool_query(manager=manager, **_kwargs(database, sql="SELECT id FROM t ORDER BY id", offset=5, limit=5))
    assert paged.total_rows == 25 and paged.has_more is True
    assert len(calls) == 2  # setup+fetch, then count+unlock+rollback


# ---------------------------------------------------------------------------
# Error hints, EXPLAIN, catalog lookups (db_gateway_schema / timeout hints)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def workflow_db(database):
    with psycopg.connect(
        host=database["host"], port=database["port"], user=database["user"],
        dbname=database["database"], autocommit=True,
    ) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS workflow_runs (id uuid PRIMARY KEY, tenant_id uuid NOT NULL, "
            "status varchar(255) DEFAULT 'running', created_at timestamp, tags text[])"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS workflow_run_tenant_idx ON workflow_runs (tenant_id, created_at)"
        )
    return database


async def test_pool_error_includes_hint_and_position(workflow_db, manager):
    with pytest.raises(RuntimeError) as info:
        await run_pool_query(
            manager=manager,
            **_kwargs(workflow_db, sql="SELECT id\nFROM workflow_runs WHERE stauts = 'x'"),
        )
    text = str(info.value)
    assert 'column "stauts" does not exist' in text
    assert 'HINT:  Perhaps you meant to reference the column "workflow_runs.status".' in text
    assert "POSITION:  36 (line 2, column 26 of the submitted SQL)" in text


async def test_pool_uuid_text_mismatch_has_cast_hint(workflow_db, manager):
    with pytest.raises(RuntimeError, match="explicit type casts"):
        await run_pool_query(
            manager=manager,
            **_kwargs(workflow_db, sql="SELECT 1 FROM workflow_runs WHERE tenant_id = 'x'::text"),
        )


@pytest.mark.parametrize("use_pool", [True, False])
async def test_explain_returns_plan_without_executing(workflow_db, manager, use_pool):
    if not use_pool and shutil.which("psql") is None:
        pytest.skip("psql not installed")
    # pg_sleep in the select list would take 5s if the statement were executed.
    sql = "SELECT pg_sleep(5), id FROM workflow_runs WHERE tenant_id = gen_random_uuid() -- note"
    started = time.monotonic()
    if use_pool:
        result = await run_pool_query(manager=manager, explain=True, **_kwargs(workflow_db, sql=sql))
    else:
        result = await run_psql(explain=True, **_kwargs(workflow_db, sql=sql))
    assert time.monotonic() - started < 3
    assert result.columns == ["QUERY PLAN"]
    plan = "\n".join(row[0] for row in result.rows)
    assert "workflow_runs" in plan and "actual time" not in plan
    assert result.total_rows == len(result.rows) and result.has_more is False


async def test_explain_error_is_reported_with_hint(workflow_db, manager):
    with pytest.raises(RuntimeError, match="HINT:  Perhaps you meant"):
        await run_pool_query(
            manager=manager, explain=True,
            **_kwargs(workflow_db, sql="SELECT stauts FROM workflow_runs"),
        )
    # The connection is clean afterwards.
    ok = await run_pool_query(manager=manager, **_kwargs(workflow_db, sql="SELECT 1 AS x"))
    assert ok.rows == [["1"]]


@pytest.mark.parametrize("use_pool", [True, False])
async def test_schema_lookup_shows_workflow_runs_columns_and_indexes(workflow_db, manager, use_pool, tmp_path, monkeypatch):
    from db_gateway import pg_pool
    from db_gateway.config import AppConfig
    from db_gateway.core import execute_schema_lookup

    monkeypatch.setattr(pg_pool, "MANAGER", manager)
    cfg = AppConfig()
    cfg.resources.base_dir = str(tmp_path)
    cfg.postgres.host = workflow_db["host"]
    cfg.postgres.user = workflow_db["user"]
    cfg.postgres.db_prod = workflow_db["database"]
    cfg.postgres.port_prod = workflow_db["port"]
    cfg.postgres.driver = "pool" if use_pool else "psql"
    cfg.postgres.pool_watch_teleport_keys = False

    data = await execute_schema_lookup(cfg=cfg, database="prod", tables=["workflow_runs", "missing_one"])

    assert data["success"] is True, data
    (table,) = data["tables"]
    assert [c["name"] for c in table["columns"]] == ["id", "tenant_id", "status", "created_at", "tags"]
    assert {c["name"]: c["type"] for c in table["columns"]}["status"] == "varchar(255)"
    assert {c["name"]: c["type"] for c in table["columns"]}["tags"] == "text[]"
    assert {i["name"] for i in table["indexes"]} == {"workflow_runs_pkey", "workflow_run_tenant_idx"}
    assert data["not_found"] == ["missing_one"]


async def test_timeout_error_lists_indexes_from_the_database(workflow_db, manager, tmp_path, monkeypatch):
    from db_gateway import core, pg_pool
    from db_gateway.config import AppConfig

    monkeypatch.setattr(pg_pool, "MANAGER", manager)
    core._INDEX_CACHE.clear()
    core._INDEX_CACHE_FAILED_AT.clear()
    cfg = AppConfig()
    cfg.resources.base_dir = str(tmp_path)
    cfg.postgres.host = workflow_db["host"]
    cfg.postgres.user = workflow_db["user"]
    cfg.postgres.db_prod = workflow_db["database"]
    cfg.postgres.port_prod = workflow_db["port"]
    cfg.postgres.driver = "pool"
    cfg.postgres.pool_watch_teleport_keys = False
    cfg.postgres.query_timeout_seconds = 1
    cfg.postgres.count_timeout_seconds = 0

    try:
        result = await core.execute_gateway_query(
            cfg=cfg, database="prod",
            sql="SELECT pg_sleep(3), id FROM workflow_runs RIGHT JOIN (SELECT 1) x ON true",
        )
    finally:
        core._INDEX_CACHE.clear()
    assert result.code == "QUERY_TIMEOUT"
    assert "workflow_run_tenant_idx ON public.workflow_runs USING btree (tenant_id, created_at)" in result.error
