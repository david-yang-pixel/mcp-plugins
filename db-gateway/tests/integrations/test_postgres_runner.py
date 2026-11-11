from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from db_gateway.postgres import run_psql


@pytest.mark.asyncio
async def test_run_psql_kills_and_reaps_process_on_timeout():
    class FakeProcess:
        def __init__(self) -> None:
            self.killed = False
            self.communicate_calls = 0

        async def communicate(self):
            self.communicate_calls += 1
            if self.communicate_calls == 1:
                await asyncio.sleep(1)
            return b"", b""

        def kill(self) -> None:
            self.killed = True

    fake_proc = FakeProcess()

    with patch(
        "db_gateway.postgres.asyncio.create_subprocess_exec",
        AsyncMock(return_value=fake_proc),
    ):
        with pytest.raises(asyncio.TimeoutError):
            await run_psql(
                host="localhost",
                port=56061,
                user="read",
                password="pw",
                database="langgenius",
                sql="SELECT 1",
                timeout=0.01,
                max_rows=10,
            )

    assert fake_proc.killed is True
    assert fake_proc.communicate_calls == 2


# ---------------------------------------------------------------------------
# Output parsing / command construction with a mocked psql process.
# psql's stdout is simulated exactly as `psql --csv -q -P footer=off` with
# `\timing on` and our `\echo <marker> <step> :ROW_COUNT` separators prints it.
# ---------------------------------------------------------------------------

from db_gateway.postgres import PsqlSafetyError, TeleportAuthExpired  # noqa: E402

MARK = "MK"
NULL = f"{MARK}NULL"


class ScriptedProcess:
    def __init__(self, stdout: str, stderr: str = "", returncode: int = 0) -> None:
        self._stdout = stdout.encode("utf-8")
        self._stderr = stderr.encode("utf-8")
        self.returncode = returncode

    async def communicate(self):
        return self._stdout, self._stderr

    def kill(self) -> None:  # pragma: no cover - only used on timeout
        pass


async def _run(stdout: str, *, stderr: str = "", returncode: int = 0, **kwargs):
    proc = ScriptedProcess(stdout, stderr, returncode)
    spawn = AsyncMock(return_value=proc)
    params = {
        "host": "localhost",
        "port": 56061,
        "user": "read",
        "password": "pw",
        "database": "langgenius",
        "sql": "SELECT id, note FROM t",
        "timeout": 30,
        "max_rows": 1000,
    }
    params.update(kwargs)
    with patch("db_gateway.postgres._new_marker", return_value=MARK), patch(
        "db_gateway.postgres.asyncio.create_subprocess_exec", spawn,
    ):
        result = await run_psql(**params)
    cmd = list(spawn.await_args.args)
    return result, cmd, spawn.await_args.kwargs


def _sql_args(cmd: list[str]) -> list[str]:
    return [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "-c"]


@pytest.mark.asyncio
async def test_psql_command_uses_real_csv_without_footer():
    stdout = f"id\n1\nTime: 1.0 ms\n{MARK} fetch 1\nTime: 0.1 ms\n{MARK} count 0\n"
    _, cmd, kwargs = await _run(stdout, count_timeout=5)

    # `-A`/`-F ,` after --csv switched psql back to unaligned output, which printed
    # a "(N rows)" footer and broke CSV quoting.
    assert "--csv" in cmd
    assert "-A" not in cmd and "-F" not in cmd
    assert "footer=off" in cmd
    assert f"null={NULL}" in cmd
    assert "ON_ERROR_STOP=1" in cmd
    assert "-X" in cmd and "-w" in cmd
    assert kwargs["env"]["PGPASSWORD"] == "pw"
    assert kwargs["env"]["LC_MESSAGES"] == "C"
    sqls = _sql_args(cmd)
    assert sqls[0] == "\\timing on"
    main = sqls[1]
    assert main.startswith("BEGIN TRANSACTION READ ONLY; SET LOCAL statement_timeout = 30000; ")
    assert "DECLARE _db_gateway_cursor NO SCROLL CURSOR FOR SELECT * FROM (\nSELECT id, note FROM t\n) AS _db_gateway_q" in main
    assert main.endswith("; FETCH FORWARD 1001 FROM _db_gateway_cursor")
    assert "SET LOCAL statement_timeout = 5000; MOVE FORWARD ALL IN _db_gateway_cursor" in sqls


@pytest.mark.asyncio
async def test_row_count_ignores_footer_and_counts_multiline_fields():
    rows = "\n".join([
        '1,"line1\nline2"',
        '2,"a,b"',
        '3,"say ""hi"""',
        "4,x",
        "5,x",
        "6,x",
        '7,"multi\nline\nvalue"',
    ])
    stdout = f"id,note\n{rows}\nTime: 2.5 ms\n{MARK} fetch 7\nTime: 0.1 ms\n{MARK} count 0\n"
    result, _, _ = await _run(stdout, count_timeout=5)

    assert result.row_count == 7
    assert result.total_rows == 7
    assert result.has_more is False and result.truncated is False
    assert result.columns == ["id", "note"]
    assert result.rows[0] == ["1", "line1\nline2"]
    assert result.rows[2] == ["3", 'say "hi"']
    assert "(7 rows)" not in result.csv
    assert result.csv.startswith('id,note\n1,"line1\nline2"\n2,"a,b"\n')


@pytest.mark.asyncio
async def test_null_is_distinguished_from_empty_string():
    stdout = f"n,e,lit\n{NULL},,\\N\nTime: 1.0 ms\n{MARK} fetch 1\n"
    result, _, _ = await _run(stdout, count_timeout=0)

    assert result.rows == [[None, "", "\\N"]]
    # CSV keeps psql semantics: NULL and '' are both empty fields.
    assert result.csv == "n,e,lit\n,,\\N\n"


@pytest.mark.asyncio
async def test_pagination_uses_cursor_offset_and_counts_rest():
    page = "\n".join(str(i) for i in range(11, 17))  # limit 5 + 1 probe row
    stdout = (
        f"Time: 1.2 ms\n{MARK} skip 10\n"
        f"x\n{page}\nTime: 2.0 ms\n{MARK} fetch 6\n"
        f"Time: 3.0 ms\n{MARK} count 7\n"
    )
    result, cmd, _ = await _run(stdout, offset=10, limit=5, count_timeout=5)

    sqls = _sql_args(cmd)
    assert sqls[1].endswith("; MOVE FORWARD 10 IN _db_gateway_cursor")
    assert sqls[2] == f"\\echo {MARK} skip :ROW_COUNT"
    assert sqls[3] == "FETCH FORWARD 6 FROM _db_gateway_cursor"
    assert result.row_count == 5
    assert [r[0] for r in result.rows] == ["11", "12", "13", "14", "15"]
    assert result.has_more is True and result.truncated is True
    assert result.offset_skipped == 10
    assert result.total_rows == 10 + 6 + 7
    assert result.timing_ms["query_ms"] == 3.2
    assert result.timing_ms["count_ms"] == 3.0
    assert "connect_and_overhead_ms" in result.timing_ms
    assert "psql_process_ms" in result.timing_ms


@pytest.mark.asyncio
async def test_limit_is_capped_by_max_rows():
    stdout = f"x\n1\nTime: 1.0 ms\n{MARK} fetch 1\n"
    _, cmd, _ = await _run(stdout, limit=5000, max_rows=100, count_timeout=0)

    assert _sql_args(cmd)[1].endswith("FETCH FORWARD 101 FROM _db_gateway_cursor")


@pytest.mark.asyncio
async def test_offset_beyond_end_reports_exact_total():
    stdout = f"Time: 1.0 ms\n{MARK} skip 23\nx\nTime: 1.0 ms\n{MARK} fetch 0\nTime: 0.1 ms\n{MARK} count 0\n"
    result, _, _ = await _run(stdout, offset=30, limit=5, count_timeout=5)

    assert result.row_count == 0
    assert result.has_more is False
    assert result.offset_skipped == 23
    assert result.total_rows == 23


@pytest.mark.asyncio
async def test_count_timeout_keeps_page_and_returns_null_total():
    page = "\n".join(str(i) for i in range(1, 7))
    stdout = f"x\n{page}\nTime: 2.0 ms\n{MARK} fetch 6\nTime: 1000.4 ms (00:01.000)\n"
    result, _, _ = await _run(
        stdout,
        stderr="ERROR:  canceling statement due to statement timeout\n",
        returncode=1,
        limit=5,
        count_timeout=1,
    )

    assert result.row_count == 5
    assert result.has_more is True
    assert result.total_rows is None
    assert "超时" in (result.total_rows_note or "")
    assert result.timing_ms["count_ms"] == 1000.4


@pytest.mark.asyncio
async def test_count_disabled_skips_move_all():
    page = "\n".join(str(i) for i in range(1, 7))
    stdout = f"x\n{page}\nTime: 2.0 ms\n{MARK} fetch 6\n"
    result, cmd, _ = await _run(stdout, limit=5, count_timeout=0)

    assert not any("MOVE FORWARD ALL" in s for s in _sql_args(cmd))
    assert result.has_more is True
    assert result.total_rows is None
    assert "count_timeout_seconds=0" in (result.total_rows_note or "")


@pytest.mark.asyncio
async def test_describe_returns_column_types():
    stdout = (
        f"Column,Type\nid,integer\ncreated_at,timestamp with time zone\nTime: 0.8 ms\n{MARK} describe 0\n"
        f"id,created_at\n1,2026-01-01 00:00:00+00\nTime: 1.0 ms\n{MARK} fetch 1\n"
    )
    result, cmd, _ = await _run(stdout, describe=True, count_timeout=0)

    sqls = _sql_args(cmd)
    assert sqls[1] == "\\gdesc"
    assert sqls[2].startswith("SELECT * FROM (\n")
    assert result.column_types == ["integer", "timestamp with time zone"]
    assert result.timing_ms["describe_ms"] == 0.8


@pytest.mark.asyncio
async def test_main_query_error_raises_runtime_error():
    with pytest.raises(RuntimeError, match="does not exist"):
        await _run("", stderr='ERROR:  column "nosuch" does not exist\n', returncode=1)


@pytest.mark.asyncio
async def test_connection_refused_raises_teleport_expired():
    with pytest.raises(TeleportAuthExpired):
        await _run(
            "",
            stderr='psql: error: connection to server at "localhost" port 56061 failed: Connection refused\n',
            returncode=2,
        )


@pytest.mark.asyncio
async def test_unsafe_sql_never_spawns_psql():
    spawn = AsyncMock()
    with patch("db_gateway.postgres.asyncio.create_subprocess_exec", spawn):
        with pytest.raises(PsqlSafetyError):
            await run_psql(
                host="localhost", port=1, user="u", password="p", database="d",
                sql="SELECT 1; DELETE FROM t", timeout=1, max_rows=10,
            )
    spawn.assert_not_awaited()


@pytest.mark.asyncio
async def test_explain_runs_explain_without_cursor_or_count():
    stdout = (
        "QUERY PLAN\n"
        "Seq Scan on t  (cost=0.00..1.01 rows=1 width=4)\n"
        '"  Filter: (id = 1)"\n'
        f"Time: 0.4 ms\n{MARK} fetch 0\n"
    )
    result, cmd, _ = await _run(stdout, sql="SELECT id FROM t WHERE id = 1;", explain=True, count_timeout=5)

    sqls = _sql_args(cmd)
    assert sqls[0] == "\\timing on"
    assert sqls[1] == (
        "BEGIN TRANSACTION READ ONLY; SET LOCAL statement_timeout = 30000; "
        "EXPLAIN SELECT id FROM t WHERE id = 1\n"
    )
    assert not any("DECLARE" in s or "MOVE" in s or "ANALYZE" in s for s in sqls)
    assert result.columns == ["QUERY PLAN"]
    assert result.rows == [["Seq Scan on t  (cost=0.00..1.01 rows=1 width=4)"], ["  Filter: (id = 1)"]]
    assert result.total_rows == 2 and result.has_more is False
    assert result.column_types == ["text"]
    assert result.csv.startswith("QUERY PLAN\nSeq Scan on t")


@pytest.mark.asyncio
async def test_explain_pages_plan_lines_in_memory():
    plan = "\n".join(f"line {i}" for i in range(5))
    stdout = f"QUERY PLAN\n{plan}\nTime: 0.4 ms\n{MARK} fetch 0\n"
    result, _, _ = await _run(stdout, explain=True, offset=1, limit=2)

    assert result.rows == [["line 1"], ["line 2"]]
    assert result.has_more is True and result.total_rows == 5 and result.offset_skipped == 1


@pytest.mark.asyncio
async def test_explain_still_validates_sql_before_spawning():
    spawn = AsyncMock()
    with patch("db_gateway.postgres.asyncio.create_subprocess_exec", spawn):
        for sql in ("EXPLAIN ANALYZE SELECT 1", "SELECT 1; DELETE FROM t"):
            with pytest.raises(PsqlSafetyError):
                await run_psql(
                    host="localhost", port=1, user="u", password="p", database="d",
                    sql=sql, timeout=1, max_rows=10, explain=True,
                )
    spawn.assert_not_awaited()


@pytest.mark.asyncio
async def test_psql_error_text_keeps_hint():
    stderr = (
        'ERROR:  column "stauts" does not exist\n'
        "LINE 2: SELECT * FROM t WHERE stauts = 1\n"
        "                              ^\n"
        'HINT:  Perhaps you meant to reference the column "t.status".\n'
    )
    with pytest.raises(RuntimeError, match="HINT:  Perhaps you meant"):
        await _run("", stderr=stderr, returncode=1)
