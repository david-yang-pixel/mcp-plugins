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
