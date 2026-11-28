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
        "count_timeout": 0,  # production default: no count unless count_total=true
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
@pytest.mark.parametrize("count_timeout", [0, 5])
async def test_pool_results_match_psql(database, manager, sql, extra, count_timeout):
    for describe in (False, True):
        kwargs = _kwargs(database, sql=sql, describe=describe, count_timeout=count_timeout, **extra)
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


@pytest.mark.parametrize("count_timeout", [0, 5])
async def test_transaction_is_read_only_and_rolled_back(database, manager, count_timeout):
    with pytest.raises(RuntimeError, match="read-only transaction"):
        await run_pool_query(manager=manager, **_kwargs(database, sql="SELECT nextval('seq')", count_timeout=count_timeout))
    # The connection is clean afterwards (no aborted transaction left behind).
    ok = await run_pool_query(
        manager=manager,
        **_kwargs(database, sql="SELECT current_setting('transaction_read_only')", count_timeout=count_timeout),
    )
    assert ok.rows == [["on"]]


@pytest.mark.parametrize("count_timeout", [0, 5])
async def test_statement_timeout_is_a_timeout(database, manager, count_timeout):
    with pytest.raises(asyncio.TimeoutError):
        await run_pool_query(
            manager=manager, **_kwargs(database, sql="SELECT pg_sleep(3)", timeout=1, count_timeout=count_timeout),
        )


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


async def test_dropped_idle_connection_is_retried_on_a_fresh_one(database, manager):
    # Like a tsh-proxy-db restart: the pooled connection dies while idle. The
    # query fails right after checkout, so it is retried at once on a new
    # connection instead of failing (and draining the pool).
    await run_pool_query(manager=manager, **_kwargs(database, sql="SELECT 1"))
    assert _terminate_pool_backends(database) >= 1

    again = await run_pool_query(manager=manager, **_kwargs(database, sql="SELECT 3"))
    assert again.rows == [["3"]]
    assert again.timing_ms["pool_reconnects"] == 1


async def test_dropped_connection_is_retried_for_catalog_queries(database, manager):
    from db_gateway.pg_pool import run_metadata_query

    kwargs = dict(host=database["host"], port=database["port"], user=database["user"], password="",
                  database=database["database"], manager=manager)
    await run_metadata_query(**kwargs, sql="SELECT 1", params=None)
    assert _terminate_pool_backends(database) >= 1

    assert await run_metadata_query(**kwargs, sql="SELECT 7", params=None) == [["7"]]


async def test_lost_tunnel_still_reports_teleport_expired(slow_link, latency_proxy, manager):
    settings = PoolSettings(acquire_timeout_seconds=1.5, connect_timeout_seconds=1)
    await run_pool_query(manager=manager, settings=settings, **_kwargs(slow_link, sql="SELECT 1"))
    await latency_proxy.close()  # the tunnel is gone: the retry cannot connect either

    with pytest.raises(TeleportAuthExpired):
        await run_pool_query(manager=manager, settings=settings, **_kwargs(slow_link, sql="SELECT 1"))


async def test_idle_check_replaces_dropped_connection_transparently(database, manager):
    settings = PoolSettings(check_idle_seconds=0)
    await run_pool_query(manager=manager, settings=settings, **_kwargs(database, sql="SELECT 1"))
    assert _terminate_pool_backends(database) >= 1

    result = await run_pool_query(manager=manager, settings=settings, **_kwargs(database, sql="SELECT 4"))
    assert result.rows == [["4"]]


async def test_keepalive_lets_the_next_query_skip_the_idle_probe(slow_link, manager):
    """Idle connections are verified in the background, off the request path."""
    settings = PoolSettings(check_idle_seconds=0.3)
    kwargs = _kwargs(slow_link, sql="SELECT 1")
    await run_pool_query(manager=manager, settings=settings, **kwargs)
    await asyncio.sleep(0.4)  # idle long enough that checkout would probe it

    await manager.keepalive(older_than=0.2, probe_timeout=2)
    elapsed, page = await _elapsed(run_pool_query(manager=manager, settings=settings, **kwargs))

    assert page.timing_ms["pool_new_connections"] == 0
    assert elapsed < LINK_DELAY * 1.8, f"{elapsed:.3f}s: the query still probed the connection"


async def test_keepalive_replaces_a_dead_idle_connection(database, manager):
    await run_pool_query(manager=manager, **_kwargs(database, sql="SELECT 1"))
    assert _terminate_pool_backends(database) >= 1

    await manager.keepalive(older_than=0, probe_timeout=2)
    after = await run_pool_query(manager=manager, **_kwargs(database, sql="SELECT 8"))

    assert after.rows == [["8"]]
    assert after.timing_ms["pool_reconnects"] == 0  # already replaced in the background


async def test_keepalive_does_not_grow_the_pool(database, manager):
    settings = PoolSettings(min_size=1, max_size=5)
    await run_pool_query(manager=manager, settings=settings, **_kwargs(database, sql="SELECT 1"))
    (key,) = manager.open_pools()

    for _ in range(3):
        await manager.keepalive(older_than=0, probe_timeout=2)
    await asyncio.sleep(0.3)  # give a (wrongly) growing pool time to connect

    assert manager._pools[key].get_stats()["pool_size"] == 1


async def test_keepalive_gives_up_on_a_stalled_connection(slow_link, latency_proxy, manager):
    await run_pool_query(manager=manager, **_kwargs(slow_link, sql="SELECT 1"))
    latency_proxy.pause()  # responses never arrive: a black-holed connection

    elapsed, _ = await _elapsed(manager.keepalive(older_than=0, probe_timeout=0.5))
    latency_proxy.resume()

    assert elapsed < 1.5, f"{elapsed:.3f}s: the probe did not time out"
    after = await run_pool_query(manager=manager, **_kwargs(slow_link, sql="SELECT 9"))
    assert after.rows == [["9"]]


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


@pytest.mark.parametrize("count_timeout", [0, 5])
async def test_session_settings_do_not_leak_to_the_next_query(database, manager, count_timeout):
    """Security review: session GUCs set by one caller must not reach the next.

    The forced ROLLBACK undoes session-level SET/set_config made inside the
    transaction; advisory locks (which survive ROLLBACK) are rejected upfront.
    """
    settings = PoolSettings(min_size=1, max_size=1)  # same backend for both queries
    changed = await run_pool_query(
        manager=manager, settings=settings,
        **_kwargs(
            database, count_timeout=count_timeout,
            sql="SELECT set_config('search_path', 'evil', false), set_config('work_mem', '1MB', false)",
        ),
    )
    assert changed.rows == [["evil", "1MB"]]

    after = await run_pool_query(
        manager=manager, settings=settings,
        **_kwargs(
            database, count_timeout=count_timeout,
            sql="SELECT current_setting('search_path'), current_setting('work_mem') <> '1MB'",
        ),
    )
    assert after.timing_ms["pool_new_connections"] == 0
    assert after.rows == [['"$user", public', "t"]]

    from db_gateway.postgres import PsqlSafetyError

    with pytest.raises(PsqlSafetyError):
        await run_pool_query(manager=manager, settings=settings, **_kwargs(database, sql="SELECT pg_advisory_lock(42)"))


@pytest.mark.parametrize("count_timeout", [0, 5])
async def test_advisory_lock_taken_by_a_full_page_is_released(database, manager, count_timeout):
    settings = PoolSettings(min_size=1, max_size=1)
    page = await run_pool_query(
        manager=manager, settings=settings,
        **_kwargs(
            database, limit=3, count_timeout=count_timeout,
            sql="SELECT g, query_to_xml('select pg_advisory_lock(11)', false, false, '') IS NOT NULL FROM generate_series(1, 50) g",
        ),
    )
    assert page.has_more is True

    after = await run_pool_query(manager=manager, settings=settings, **_kwargs(database, sql=_advisory_locks_sql()))
    assert after.rows == [["0"]]


def _advisory_locks_sql() -> str:
    return "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND pid = pg_backend_pid()"


@pytest.mark.parametrize("count_timeout", [0, 5])
@pytest.mark.parametrize(
    "sql",
    [
        # Security review: Unicode-escaped identifiers slip past the denylist ...
        'SELECT U&"\\0070g_advisory_lock"(7)',
        # ... and query_to_xml runs SQL from a string the validator cannot see.
        "SELECT query_to_xml('select pg_advisory_lock(8)', false, false, '')",
    ],
)
async def test_advisory_locks_never_survive_a_pooled_query(database, manager, sql, count_timeout):
    settings = PoolSettings(min_size=1, max_size=1)  # same backend for both queries
    await run_pool_query(manager=manager, settings=settings, **_kwargs(database, sql=sql, count_timeout=count_timeout))

    after = await run_pool_query(manager=manager, settings=settings, **_kwargs(database, sql=_advisory_locks_sql()))

    assert after.timing_ms["pool_new_connections"] == 0
    assert after.rows == [["0"]]


@pytest.mark.parametrize("count_timeout", [0, 5])
async def test_advisory_lock_is_released_after_a_failed_query(database, manager, count_timeout):
    settings = PoolSettings(min_size=1, max_size=1)
    with pytest.raises(RuntimeError):
        await run_pool_query(
            manager=manager, settings=settings,
            # The division by zero only happens after query_to_xml took the lock.
            **_kwargs(
                database, count_timeout=count_timeout,
                sql="SELECT 1 / (CASE WHEN query_to_xml('select pg_advisory_lock(9)', false, false, '') IS NOT NULL THEN 0 END)",
            ),
        )

    after = await run_pool_query(manager=manager, settings=settings, **_kwargs(database, sql=_advisory_locks_sql()))
    assert after.rows == [["0"]]


def _spy_round_trips(monkeypatch) -> list[str]:
    """Record every execute(): each one is a round trip to the server."""
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
    return calls


async def test_warm_query_without_count_is_one_round_trip(database, manager, monkeypatch):
    """No count requested (the default): setup, fetch, unlock and ROLLBACK go in one batch."""
    await run_pool_query(manager=manager, **_kwargs(database, sql="SELECT 1", count_timeout=0))
    calls = _spy_round_trips(monkeypatch)

    small = await run_pool_query(manager=manager, **_kwargs(database, sql="SELECT id FROM t ORDER BY id", count_timeout=0))
    assert small.total_rows == 25 and small.has_more is False
    assert len(calls) == 1

    calls.clear()
    paged = await run_pool_query(
        manager=manager, **_kwargs(database, sql="SELECT id FROM t ORDER BY id", offset=5, limit=5, count_timeout=0),
    )
    assert paged.rows == [[str(i)] for i in range(6, 11)]
    assert paged.has_more is True and paged.offset_skipped == 5
    assert paged.total_rows is None and "count_total=true" in paged.total_rows_note
    assert len(calls) == 1


async def test_requested_count_adds_one_round_trip(database, manager, monkeypatch):
    await run_pool_query(manager=manager, **_kwargs(database, sql="SELECT 1"))
    calls = _spy_round_trips(monkeypatch)

    paged = await run_pool_query(
        manager=manager, **_kwargs(database, sql="SELECT id FROM t ORDER BY id", offset=5, limit=5, count_timeout=5),
    )
    assert paged.total_rows == 25 and paged.has_more is True
    assert len(calls) == 2  # setup+fetch, then count+unlock+rollback


async def test_describe_without_count_matches_the_count_path(database, manager):
    sql = "SELECT id, label, 1.5::numeric(4,2) AS n FROM t ORDER BY id"
    counted = await run_pool_query(manager=manager, describe=True, **_kwargs(database, sql=sql, limit=3, count_timeout=5))
    single = await run_pool_query(manager=manager, describe=True, **_kwargs(database, sql=sql, limit=3, count_timeout=0))
    assert single.column_types == counted.column_types == ["integer", "text", "numeric(4,2)"]
    assert single.rows == counted.rows


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


@pytest.mark.parametrize("count_timeout", [0, 5])
async def test_pool_error_includes_hint_and_position(workflow_db, manager, count_timeout):
    with pytest.raises(RuntimeError) as info:
        await run_pool_query(
            manager=manager,
            **_kwargs(workflow_db, sql="SELECT id\nFROM workflow_runs WHERE stauts = 'x'", count_timeout=count_timeout),
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


# ---------------------------------------------------------------------------
# Round trips on a slow link: elapsed time ~= round trips x latency
# ---------------------------------------------------------------------------

LINK_DELAY = 0.15  # seconds added to every server -> client chunk


class LatencyProxy:
    """TCP proxy to the test server that delays server responses by ``delay``.

    Chunks keep their order and are delivered ``delay`` after they arrive (pure
    latency, not per-chunk delay), so a request/response costs one ``delay``.
    ``pause()`` stalls server -> client traffic (a black-holed connection),
    ``drop_all()`` closes every proxied connection (a tunnel restart) and
    ``close()`` also stops listening (the tunnel is gone).
    """

    def __init__(self, host: str, port: int, delay: float) -> None:
        self.host, self.target_port, self.delay = host, port, delay
        self.port = 0
        self._server: asyncio.base_events.Server | None = None
        self._tasks: set[asyncio.Task] = set()
        self._writers: set[asyncio.StreamWriter] = set()
        self._flowing = asyncio.Event()
        self._flowing.set()
        self.client_bytes = bytearray()  # everything clients sent, to count statements

    async def start(self) -> "LatencyProxy":
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    def pause(self) -> None:
        self._flowing.clear()

    def resume(self) -> None:
        self._flowing.set()

    async def drop_all(self) -> None:
        for writer in list(self._writers):
            writer.close()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._writers.clear()
        self._tasks.clear()

    async def close(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()  # stop listening first ...
        await self.drop_all()  # ... then drop live connections: 3.12 wait_closed() waits for them
        if server is not None:
            await server.wait_closed()

    async def _handle(self, client_reader, client_writer):
        server_reader, server_writer = await asyncio.open_connection(self.host, self.target_port)
        self._writers.update({client_writer, server_writer})

        async def forward(reader, writer):
            try:
                while data := await reader.read(65536):
                    self.client_bytes += data
                    writer.write(data)
                    await writer.drain()
            except (ConnectionError, asyncio.CancelledError):
                pass
            finally:
                writer.close()

        async def delayed(reader, writer):
            queue: asyncio.Queue = asyncio.Queue()

            async def deliver():
                while True:
                    due, data = await queue.get()
                    if data is None:
                        break
                    await asyncio.sleep(max(0.0, due - time.monotonic()))
                    await self._flowing.wait()
                    writer.write(data)
                    await writer.drain()

            sender = asyncio.create_task(deliver())
            try:
                while data := await reader.read(65536):
                    await queue.put((time.monotonic() + self.delay, data))
            except (ConnectionError, asyncio.CancelledError):
                pass
            finally:
                await queue.put((0.0, None))
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)
                writer.close()

        pumps = [asyncio.create_task(forward(client_reader, server_writer)),
                 asyncio.create_task(delayed(server_reader, client_writer))]
        self._tasks.update(pumps)
        await asyncio.gather(*pumps, return_exceptions=True)


@pytest.fixture
async def latency_proxy(database):
    proxy = await LatencyProxy(database["host"], database["port"], LINK_DELAY).start()
    yield proxy
    await proxy.close()


@pytest.fixture
async def slow_link(database, latency_proxy):
    """The test database reached through ``latency_proxy``."""
    yield {**database, "port": latency_proxy.port}


async def _elapsed(coro) -> tuple[float, object]:
    started = time.monotonic()
    result = await coro
    return time.monotonic() - started, result


async def test_query_page_is_one_round_trip_on_a_slow_link(slow_link, manager):
    kwargs = _kwargs(slow_link, sql="SELECT id FROM t ORDER BY id", limit=5, count_timeout=0)
    await run_pool_query(manager=manager, **kwargs)  # opens the connection
    elapsed, page = await _elapsed(run_pool_query(manager=manager, **kwargs))
    assert page.has_more is True and page.timing_ms["pool_new_connections"] == 0
    assert elapsed < LINK_DELAY * 1.8, f"{elapsed:.3f}s: more than one round trip"


async def test_metadata_query_is_one_round_trip(slow_link, manager):
    from db_gateway.pg_pool import run_metadata_query

    kwargs = dict(host=slow_link["host"], port=slow_link["port"], user=slow_link["user"], password="",
                  database=slow_link["database"], manager=manager)
    await run_metadata_query(**kwargs, sql="SELECT 1", params=None)  # opens the connection
    elapsed, rows = await _elapsed(run_metadata_query(**kwargs, sql="SELECT %s::int + 1", params=(1,)))
    assert rows == [['2']]  # pool connections load every value as text
    assert elapsed < LINK_DELAY * 1.8, f"{elapsed:.3f}s: more than one round trip"


async def test_schema_lookup_is_one_round_trip(slow_link, workflow_db, manager, tmp_path, monkeypatch):
    from db_gateway import pg_pool
    from db_gateway.config import AppConfig
    from db_gateway.core import execute_schema_lookup

    monkeypatch.setattr(pg_pool, "MANAGER", manager)
    cfg = AppConfig()
    cfg.resources.base_dir = str(tmp_path)
    cfg.postgres.host = slow_link["host"]
    cfg.postgres.user = slow_link["user"]
    cfg.postgres.db_prod = slow_link["database"]
    cfg.postgres.port_prod = slow_link["port"]
    cfg.postgres.driver = "pool"

    await execute_schema_lookup(cfg=cfg, database="prod", tables=["workflow_runs"])  # opens the connection
    elapsed, data = await _elapsed(execute_schema_lookup(cfg=cfg, database="prod", tables=["workflow_runs"]))
    assert data["success"] is True and data["tables"][0]["indexes"]
    assert elapsed < LINK_DELAY * 1.8, f"{elapsed:.3f}s: columns and indexes not fetched in one round trip"


async def test_metadata_query_error_leaves_the_connection_clean(database, manager):
    from db_gateway.pg_pool import run_metadata_query

    kwargs = dict(host=database["host"], port=database["port"], user=database["user"], password="",
                  database=database["database"], manager=manager,
                  settings=PoolSettings(min_size=1, max_size=1))
    with pytest.raises(RuntimeError, match="does not exist"):
        await run_metadata_query(**kwargs, sql="SELECT * FROM missing_table", params=None)
    rows = await run_metadata_query(
        **kwargs, sql="SELECT current_setting('transaction_read_only'), pg_backend_pid() > 0", params=None,
    )
    assert rows == [["on", "t"]]


async def test_metadata_statement_timeout_is_reported_and_recovers(database, manager):
    from db_gateway.pg_pool import StatementTimeout, run_metadata_query

    kwargs = dict(host=database["host"], port=database["port"], user=database["user"], password="",
                  database=database["database"], manager=manager,
                  settings=PoolSettings(min_size=1, max_size=1))
    with pytest.raises(StatementTimeout):
        await run_metadata_query(**kwargs, sql="SELECT pg_sleep(3)", params=None, timeout=0.5)
    assert await run_metadata_query(**kwargs, sql="SELECT 1", params=None) == [["1"]]


async def test_failed_metadata_pipeline_does_not_poison_the_connection(database, manager):
    """Code review: a pipeline that fails after psycopg queued a named prepared
    statement must not leave a cache entry for a statement the server skipped."""
    from db_gateway.pg_pool import StatementTimeout, run_metadata_queries, run_metadata_query

    kwargs = dict(host=database["host"], port=database["port"], user=database["user"], password="",
                  database=database["database"], manager=manager, timeout=1.0,
                  settings=PoolSettings(min_size=1, max_size=1))

    def pair(sleep: float):
        return [("SELECT pg_sleep(%s::float)", (sleep,)), ("SELECT %s::int + 1", (1,))]

    for _ in range(6):  # other single catalog lookups on the same connection
        await run_metadata_query(**kwargs, sql="SELECT 1 WHERE %s::int = 1", params=(1,))
    for _ in range(5):
        await run_metadata_queries(**kwargs, queries=pair(0))
    with pytest.raises(StatementTimeout):
        await run_metadata_queries(**kwargs, queries=pair(2))  # first query times out

    for _ in range(3):
        assert (await run_metadata_queries(**kwargs, queries=pair(0)))[1] == [["2"]]


async def test_metadata_deadline_covers_every_batched_statement(database, manager, monkeypatch):
    """Code review: each batched statement has its own statement_timeout, so the
    client deadline must grow with the batch instead of firing first."""
    from db_gateway import pg_pool
    from db_gateway.pg_pool import run_metadata_queries

    seen: list[float] = []
    real_wait_for = asyncio.wait_for

    async def spy(aw, timeout):
        # pg_pool.asyncio is the global module: only record the gateway's own deadline.
        if "run_metadata_queries" in getattr(aw, "__qualname__", ""):
            seen.append(timeout)
        return await real_wait_for(aw, timeout)

    monkeypatch.setattr(pg_pool.asyncio, "wait_for", spy)
    settings = PoolSettings(min_size=1, max_size=1, acquire_timeout_seconds=2)
    await run_metadata_queries(
        host=database["host"], port=database["port"], user=database["user"], password="",
        database=database["database"], manager=manager, settings=settings, timeout=3.0,
        queries=[("SELECT 1", None), ("SELECT 2", None), ("SELECT 3", None)],
    )
    assert len(seen) == 1 and seen[0] >= 3 * 3.0 + settings.acquire_timeout_seconds



# ---------------------------------------------------------------------------
# Code review of the keepalive / stale-retry changes
# ---------------------------------------------------------------------------


def _pending_executes() -> list[asyncio.Task]:
    return [t for t in asyncio.all_tasks()
            if not t.done() and "execute" in getattr(t.get_coro(), "__qualname__", "")]


async def test_cancelled_probe_leaves_no_query_running(slow_link, latency_proxy, manager):
    """A probe cancelled from outside must not leave its execute() task reading a
    socket whose fd the pool is about to close (and the loop to reuse)."""
    await run_pool_query(manager=manager, **_kwargs(slow_link, sql="SELECT 1"))
    latency_proxy.pause()
    keepalive = asyncio.create_task(manager.keepalive(older_than=0, probe_timeout=5))
    await asyncio.sleep(0.3)  # the probe is waiting for an answer that never comes
    keepalive.cancel()
    with pytest.raises(asyncio.CancelledError):
        await keepalive
    await asyncio.sleep(0)

    assert _pending_executes() == []
    latency_proxy.resume()
    after = await run_pool_query(manager=manager, **_kwargs(slow_link, sql="SELECT 10"))
    assert after.rows == [["10"]]


async def test_timed_out_probe_leaves_no_query_running(slow_link, latency_proxy, manager):
    await run_pool_query(manager=manager, **_kwargs(slow_link, sql="SELECT 1"))
    latency_proxy.pause()
    await manager.keepalive(older_than=0, probe_timeout=0.3)
    assert _pending_executes() == []
    latency_proxy.resume()


async def test_statement_that_kills_its_connection_runs_at_most_twice(slow_link, latency_proxy, manager, tmp_path, monkeypatch):
    """Code review: the stale retry must not multiply a statement that breaks its
    own connection (base behaviour: one run + the core retry)."""
    from db_gateway import core, pg_pool
    from db_gateway.config import AppConfig

    monkeypatch.setattr(pg_pool, "MANAGER", manager)
    cfg = AppConfig()
    cfg.resources.base_dir = str(tmp_path)
    cfg.postgres.host = slow_link["host"]
    cfg.postgres.user = slow_link["user"]
    cfg.postgres.db_prod = slow_link["database"]
    cfg.postgres.port_prod = slow_link["port"]
    cfg.postgres.driver = "pool"
    cfg.postgres.pool_retry_delay_seconds = 0
    await core.execute_gateway_query(cfg=cfg, database="prod", sql="SELECT 1")  # reused connection

    marker = b"pg_terminate_backend"
    before = latency_proxy.client_bytes.count(marker)
    result = await core.execute_gateway_query(
        cfg=cfg, database="prod", sql="SELECT pg_terminate_backend(pg_backend_pid())",
    )

    assert result.success is False
    assert latency_proxy.client_bytes.count(marker) - before <= 2


async def test_keepalive_does_not_run_the_checkout_probe(slow_link, manager):
    """Taking a connection for keepalive must not trigger the pool's check callback
    (it would probe, and a dead connection would make the pool grow)."""
    settings = PoolSettings(check_idle_seconds=0)  # the check callback probes on every checkout
    await run_pool_query(manager=manager, settings=settings, **_kwargs(slow_link, sql="SELECT 1"))

    elapsed, _ = await _elapsed(manager.keepalive(older_than=3600, probe_timeout=2))
    assert elapsed < LINK_DELAY / 2, f"{elapsed:.3f}s: keepalive probed through the check callback"


async def test_keepalive_skips_pools_that_may_shrink_to_zero(slow_link, manager):
    settings = PoolSettings(min_size=0, max_size=2)
    await run_pool_query(manager=manager, settings=settings, **_kwargs(slow_link, sql="SELECT 1"))

    elapsed, _ = await _elapsed(manager.keepalive(older_than=0, probe_timeout=2))
    assert elapsed < LINK_DELAY / 2  # left alone: keepalive would stop max_idle shrinking it


async def test_lost_tunnel_fails_fast_after_a_stale_connection(slow_link, latency_proxy, manager, monkeypatch):
    """Code review: the immediate retry must not wait the full acquire timeout on a
    tunnel that is gone, on top of the core's own retry."""
    from db_gateway import pg_pool

    monkeypatch.setattr(pg_pool, "_STALE_RETRY_ACQUIRE_SECONDS", 0.5)
    settings = PoolSettings(acquire_timeout_seconds=4, connect_timeout_seconds=1)
    await run_pool_query(manager=manager, settings=settings, **_kwargs(slow_link, sql="SELECT 1"))
    await latency_proxy.close()

    started = time.monotonic()
    with pytest.raises(TeleportAuthExpired):
        await run_pool_query(manager=manager, settings=settings, **_kwargs(slow_link, sql="SELECT 1"))
    assert time.monotonic() - started < 2.5


async def test_several_dead_connections_recover_in_one_request(slow_link, latency_proxy, manager):
    """Re-review: with two idle connections dead (tunnel restart), the immediate
    retry must not land on the second dead one and give up."""
    settings = PoolSettings(min_size=1, max_size=3)
    kwargs = _kwargs(slow_link, sql="SELECT pg_sleep(0.3)")
    await asyncio.gather(*(run_pool_query(manager=manager, settings=settings, **kwargs) for _ in range(2)))
    (key,) = manager.open_pools()
    assert manager._pools[key].get_stats()["pool_available"] >= 2
    await latency_proxy.drop_all()  # every pooled connection is now dead

    after = await run_pool_query(manager=manager, settings=settings, **_kwargs(slow_link, sql="SELECT 11"))
    assert after.rows == [["11"]]
    assert after.timing_ms["pool_reconnects"] == 1


async def test_several_dead_connections_recover_for_catalog_queries(slow_link, latency_proxy, manager):
    from db_gateway.pg_pool import run_metadata_query

    settings = PoolSettings(min_size=1, max_size=3)
    await asyncio.gather(*(run_pool_query(manager=manager, settings=settings,
                                          **_kwargs(slow_link, sql="SELECT pg_sleep(0.3)")) for _ in range(2)))
    await latency_proxy.drop_all()

    rows = await run_metadata_query(
        host=slow_link["host"], port=slow_link["port"], user=slow_link["user"], password="",
        database=slow_link["database"], manager=manager, settings=settings, sql="SELECT 12", params=None,
    )
    assert rows == [["12"]]
