"""PostgreSQL integration: read-only SQL validator + psql subprocess executor."""
from __future__ import annotations

import re
from contextlib import suppress

from db_gateway.sql_text import (
    SqlLexError,
    has_top_level_order_by,
    strip_statement_terminators,
    tokenize,
)

try:
    from db_gateway.log import get_logger
    logger = get_logger(module="integrations.postgres")
except Exception:  # pragma: no cover
    import logging as _logging
    logger = _logging.getLogger("integrations.postgres")  # type: ignore[assignment]


class PsqlSafetyError(ValueError):
    """Raised when SQL fails read-only safety validation."""


class TeleportAuthExpired(RuntimeError):
    """Raised when psql connection fails due to expired Teleport auth."""


# Forbidden keywords (case-insensitive, word-boundary match)
_FORBIDDEN_KEYWORDS = [
    "INSERT", "UPDATE", "DELETE", "MERGE", "UPSERT",
    "DROP", "TRUNCATE", "ALTER", "CREATE",
    "GRANT", "REVOKE",
    "COPY",     # COPY FROM PROGRAM can exec shell
    "EXECUTE",  # prepared statements may hide writes
    "CALL",     # stored procedures
    "VACUUM", "ANALYZE", "REINDEX",
    "SET", "LOCK", "SHOW",
]
_FORBIDDEN_RE = {kw: re.compile(rf"\b{kw}\b") for kw in _FORBIDDEN_KEYWORDS}
_ALLOWED_FIRST_WORDS = ("SELECT", "WITH")
# Advisory locks taken with the session-level functions survive ROLLBACK and stay
# on the connection; with pooled connections they would leak to later callers.
# The transaction-level variants are rejected too: a read-only support query has
# no use for them and they can still block other sessions.
_ADVISORY_LOCK_FUNCTION_RE = re.compile(r"^PG_(TRY_)?ADVISORY_", re.IGNORECASE)


def validate_read_only_sql(sql: str) -> str:
    """Validate SQL is a single read-only query. Raises PsqlSafetyError on violation.

    Returns the statement body with leading/trailing whitespace, comments and
    trailing semicolons removed, ready to be wrapped as a sub-query.

    Checks (comments, string literals and quoted identifiers are recognised by
    a real lexer, so ``'a;b'`` or ``'--'`` no longer confuse the checks):
    1. Unterminated string / identifier / comment / dollar quote is rejected.
    2. Only one statement: ';' may only appear at the very end.
    3. Keyword blacklist (word-boundary, case-insensitive) over everything except
       comments. String literals are still scanned on purpose (conservative).
    4. First significant word, after optional leading '(' , must be SELECT or WITH.
    """
    try:
        tokens = tokenize(sql or "")
    except SqlLexError as e:
        raise PsqlSafetyError(str(e)) from e

    body, body_tokens = strip_statement_terminators(sql or "", tokens)
    if not body_tokens:
        raise PsqlSafetyError("SQL 为空（只包含空白、注释或分号）")

    for tok in body_tokens:
        if tok.kind == "punct" and tok.text == ";":
            raise PsqlSafetyError(
                "检测到多条语句：只允许单条只读查询，分号只能出现在语句末尾"
            )

    code_text = " ".join(
        t.text.upper() for t in body_tokens if t.kind in ("word", "punct")
    )
    literal_text = " ".join(
        t.text.upper() for t in body_tokens if t.kind in ("string", "qident")
    )
    for kw in _FORBIDDEN_KEYWORDS:
        if _FORBIDDEN_RE[kw].search(code_text):
            raise PsqlSafetyError(
                f"检测到禁止的关键字 {kw}：只允许 SELECT/WITH 只读查询"
            )
        if _FORBIDDEN_RE[kw].search(literal_text):
            raise PsqlSafetyError(
                f"字符串常量或双引号标识符中包含禁止的关键字 {kw}"
                "（网关对常量也做保守拦截），请改写该常量"
            )

    for tok in body_tokens:
        if tok.kind == "word":
            name = tok.text
        elif tok.kind == "qident":
            name = re.sub(r'^(?:[Uu]&)?"|"$', "", tok.text).replace('""', '"')
        else:
            name = ""
        if name and _ADVISORY_LOCK_FUNCTION_RE.match(name):
            raise PsqlSafetyError(
                f"不允许调用 advisory lock 函数 {name}：会话级锁在回滚后仍会保留，"
                "会影响复用同一连接的其他查询"
            )

    sig = [t for t in body_tokens if not t.is_trivia]
    idx = 0
    while idx < len(sig) and sig[idx].kind == "punct" and sig[idx].text == "(":
        idx += 1
    if idx >= len(sig):
        raise PsqlSafetyError("SQL 只有括号，没有查询内容")
    first = sig[idx]
    first_text = first.text.upper() if first.kind == "word" else first.text
    if first.kind != "word" or first_text not in _ALLOWED_FIRST_WORDS:
        raise PsqlSafetyError(
            "仅允许以 SELECT 或 WITH 开头的只读查询（可带前置括号），"
            f"检测到: {first_text[:40]}"
        )
    return body


def sql_has_top_level_order_by(sql: str) -> bool:
    """Best-effort check used for pagination warnings; never raises."""
    try:
        return has_top_level_order_by(tokenize(sql or ""))
    except SqlLexError:
        return False


import asyncio
import csv
import io
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


_CURSOR_NAME = "_db_gateway_cursor"
_WRAP_ALIAS = "_db_gateway_q"
_TIMING_LINE = re.compile(r"^Time: ([0-9]+(?:[.,][0-9]+)?) ms")
_TELEPORT_SIGNATURES = (
    "Connection refused",
    "could not connect to server",
    "no such host",
    "connection reset",
)


@dataclass
class PsqlResult:
    csv: str
    row_count: int
    elapsed_ms: int
    truncated: bool
    columns: list[str] = field(default_factory=list)
    # Parsed data rows; values are psql text output, ``None`` means SQL NULL.
    # ``None`` (the attribute) means "not parsed" (e.g. a hand-built result).
    rows: list[list[str | None]] | None = None
    column_types: list[str] | None = None
    has_more: bool | None = None
    total_rows: int | None = None
    total_rows_note: str | None = None
    offset_skipped: int | None = None
    timing_ms: dict[str, float] = field(default_factory=dict)


def _new_marker() -> str:
    """Random per-call token; data rows can never collide with it in practice."""
    return f"__db_gateway_{secrets.token_hex(8)}__"


def wrap_query(body: str) -> str:
    """Wrap a validated statement body as a sub-query.

    Newlines around the body keep a trailing ``-- comment`` from swallowing
    the closing parenthesis.
    """
    return f"SELECT * FROM (\n{body}\n) AS {_WRAP_ALIAS}"


def build_psql_command(
    *,
    host: str,
    port: int,
    user: str,
    database: str,
    wrapped_sql: str,
    marker: str,
    null_marker: str,
    offset: int,
    fetch_count: int,
    statement_timeout_ms: int,
    count_timeout_ms: int,
    describe: bool,
) -> list[str]:
    """Build one psql invocation (one connection) for page + optional count.

    User SQL only ever appears inside ``-c`` strings that start with SQL, which
    psql sends to the server verbatim (no meta-command or ``:var`` processing).
    Meta-commands (``\\timing``, ``\\echo``, ``\\gdesc``) are separate ``-c``
    options built only from trusted text.
    """
    cur = _CURSOR_NAME
    cmd = [
        "psql",
        "-X",            # ignore ~/.psqlrc
        "-q",            # no command tags (BEGIN/SET/...) on stdout
        "-w",            # never prompt for a password
        "--csv",         # real CSV quoting; do NOT combine with -A/-F (they switch back to unaligned)
        "-v", "ON_ERROR_STOP=1",
        "-P", "footer=off",
        "-P", f"null={null_marker}",
        "-h", host,
        "-p", str(port),
        "-U", user,
        "-d", database,
        "-c", "\\timing on",
    ]
    if describe:
        # \gdesc with an empty buffer describes the *next* query without executing it.
        cmd += ["-c", "\\gdesc", "-c", wrapped_sql, "-c", f"\\echo {marker} describe 0"]
    setup = (
        "BEGIN TRANSACTION READ ONLY; "
        f"SET LOCAL statement_timeout = {int(statement_timeout_ms)}; "
        f"DECLARE {cur} NO SCROLL CURSOR FOR {wrapped_sql}"
    )
    fetch = f"FETCH FORWARD {int(fetch_count)} FROM {cur}"
    if offset > 0:
        cmd += [
            "-c", f"{setup}; MOVE FORWARD {int(offset)} IN {cur}",
            "-c", f"\\echo {marker} skip :ROW_COUNT",
            "-c", fetch,
        ]
    else:
        cmd += ["-c", f"{setup}; {fetch}"]
    cmd += ["-c", f"\\echo {marker} fetch :ROW_COUNT"]
    if count_timeout_ms > 0:
        # Counting continues the same cursor: no second execution of the query,
        # and it is bounded by its own statement_timeout.
        cmd += [
            "-c", f"SET LOCAL statement_timeout = {int(count_timeout_ms)}; MOVE FORWARD ALL IN {cur}",
            "-c", f"\\echo {marker} count :ROW_COUNT",
        ]
    return cmd


def _split_segments(
    stdout_text: str, marker: str,
) -> tuple[dict[str, tuple[list[str], int | None]], list[str]]:
    segments: dict[str, tuple[list[str], int | None]] = {}
    current: list[str] = []
    prefix = marker + " "
    for line in stdout_text.split("\n"):
        if line.startswith(prefix):
            parts = line[len(prefix):].split()
            step = parts[0] if parts else ""
            count = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
            segments[step] = (current, count)
            current = []
        else:
            current.append(line)
    return segments, current


def _pop_timing(lines: list[str]) -> tuple[list[str], float | None]:
    """Strip psql's trailing ``Time: x ms`` line (one per SQL command)."""
    content = list(lines)
    while content and content[-1] == "":
        content.pop()
    if content:
        m = _TIMING_LINE.match(content[-1])
        if m:
            content.pop()
            return content, round(float(m.group(1).replace(",", ".")), 1)
    return content, None


def _parse_csv(lines: list[str], null_marker: str) -> tuple[list[str], list[list[str | None]]]:
    text = "\n".join(lines)
    if not text.strip():
        return [], []
    parsed = list(csv.reader(io.StringIO(text + "\n")))
    header = parsed[0]
    rows = [[None if v == null_marker else v for v in row] for row in parsed[1:]]
    return header, rows


def rows_to_csv(columns: list[str], rows: list[list[str | None]]) -> str:
    """CSV text compatible with psql --csv: NULL is written as an empty field."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow(["" if v is None else v for v in row])
    return buf.getvalue()


def _first_error_line(stderr_text: str) -> str:
    for line in stderr_text.splitlines():
        line = line.strip()
        if line:
            return line[:300]
    return "unknown error"


def _psql_env(password: str) -> dict[str, str]:
    env = dict(os.environ)
    env["PGPASSWORD"] = password
    env["PGCONNECT_TIMEOUT"] = "10"
    # Teleport `tsh proxy db --tunnel` 已在内部做 mTLS，psql 本地连接无需 SSL
    env.setdefault("PGSSLMODE", "disable")
    env.setdefault("PGCLIENTENCODING", "UTF8")
    # Stable English messages / '.' decimal point so error signatures and
    # `Time: x ms` lines stay parseable whatever the container locale is.
    lc_all = env.pop("LC_ALL", None)
    if lc_all:
        env.setdefault("LC_CTYPE", lc_all)
    env["LC_MESSAGES"] = "C"
    env["LC_NUMERIC"] = "C"
    return env


def _ms(seconds: float) -> float:
    return round(seconds * 1000, 1)


async def run_psql(
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
) -> PsqlResult:
    """Execute one read-only query page via a single psql process.

    The query runs inside ``BEGIN TRANSACTION READ ONLY`` through a cursor:
    ``MOVE offset`` -> ``FETCH limit+1`` (has_more probe) -> optional
    ``MOVE ALL`` (bounded by ``count_timeout``) to compute ``total_rows``.

    Raises:
        PsqlSafetyError: SQL (or its wrapped form) failed read-only validation
        TeleportAuthExpired: connection failed (likely auth/port-forward down)
        asyncio.TimeoutError: psql exceeded ``timeout + count_timeout``
        RuntimeError: other psql errors
    """
    body = validate_read_only_sql(sql)
    wrapped = wrap_query(body)
    validate_read_only_sql(wrapped)  # the wrapped form must stay a single read-only query

    max_rows = max(1, int(max_rows))
    page_limit = max(1, min(int(limit) if limit else max_rows, max_rows))
    offset = max(0, int(offset or 0))
    count_timeout = max(0.0, float(count_timeout or 0))
    marker = _new_marker()
    null_marker = f"{marker}NULL"

    cmd = build_psql_command(
        host=host,
        port=port,
        user=user,
        database=database,
        wrapped_sql=wrapped,
        marker=marker,
        null_marker=null_marker,
        offset=offset,
        fetch_count=page_limit + 1,
        statement_timeout_ms=int(float(timeout) * 1000),
        count_timeout_ms=int(count_timeout * 1000),
        describe=describe,
    )
    env = _psql_env(password)

    started = time.perf_counter()
    spawned = started
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        spawned = time.perf_counter()
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=float(timeout) + count_timeout,
        )
    except asyncio.TimeoutError:
        with suppress(ProcessLookupError):
            proc.kill()
        with suppress(Exception):
            await asyncio.wait_for(proc.communicate(), timeout=5)
        raise
    finished = time.perf_counter()
    elapsed_ms = int((finished - started) * 1000)

    stdout_text = stdout.decode("utf-8", errors="replace")
    stderr_text = stderr.decode("utf-8", errors="replace")
    segments, tail = _split_segments(stdout_text, marker)

    if "fetch" not in segments:
        if proc.returncode != 0:
            if any(sig in stderr_text for sig in _TELEPORT_SIGNATURES):
                raise TeleportAuthExpired(stderr_text)
            raise RuntimeError(f"psql failed (rc={proc.returncode}): {stderr_text}")
        raise RuntimeError(f"psql output is missing the result marker: {stderr_text[:500]}")

    parse_started = time.perf_counter()
    timing: dict[str, float] = {
        "psql_spawn_ms": _ms(spawned - started),
        "psql_process_ms": _ms(finished - started),
    }
    server_ms = 0.0
    timing_complete = True

    column_types: list[str] | None = None
    if describe and "describe" in segments:
        desc_lines, desc_ms = _pop_timing(segments["describe"][0])
        _, desc_rows = _parse_csv(desc_lines, null_marker)
        column_types = [row[1] or "" for row in desc_rows if len(row) >= 2]
        if desc_ms is not None:
            timing["describe_ms"] = desc_ms
            server_ms += desc_ms

    query_ms = 0.0
    skipped: int | None = 0
    if offset > 0:
        _, skip_ms = _pop_timing(segments.get("skip", ([], None))[0])
        skipped = segments.get("skip", ([], None))[1]
        if skip_ms is None:
            timing_complete = False
        query_ms += skip_ms or 0.0
    fetch_lines, fetch_ms = _pop_timing(segments["fetch"][0])
    if fetch_ms is None:
        timing_complete = False
    query_ms += fetch_ms or 0.0
    timing["query_ms"] = round(query_ms, 1)
    server_ms += query_ms

    columns, data_rows = _parse_csv(fetch_lines, null_marker)
    fetched = len(data_rows)
    has_more = fetched > page_limit
    rows = data_rows[:page_limit]

    total_rows: int | None = None
    total_note: str | None = None
    if count_timeout > 0:
        if "count" in segments:
            _, count_ms = _pop_timing(segments["count"][0])
            rest = segments["count"][1]
        else:
            _, count_ms = _pop_timing(tail)
            rest = None
            if "statement timeout" in stderr_text:
                total_note = f"计数超时（超过 {count_timeout:g}s），总行数未知；可加过滤条件或用 next_offset 继续翻页"
            else:
                total_note = f"计数阶段失败，总行数未知：{_first_error_line(stderr_text)}"
        if count_ms is not None:
            timing["count_ms"] = count_ms
            server_ms += count_ms
        if rest is not None and skipped is not None:
            total_rows = skipped + fetched + rest
    if total_rows is None and not has_more and skipped is not None:
        # Cursor exhausted inside this page: the total is exact without counting.
        total_rows = skipped + fetched
        total_note = None
    if total_rows is None and total_note is None:
        total_note = "未计算总行数（count_timeout_seconds=0）" if count_timeout <= 0 else "总行数未知"

    if timing_complete:
        timing["connect_and_overhead_ms"] = round(max(0.0, timing["psql_process_ms"] - server_ms), 1)

    csv_text = rows_to_csv(columns, rows) if columns else ""
    timing["parse_ms"] = _ms(time.perf_counter() - parse_started)

    return PsqlResult(
        csv=csv_text,
        row_count=len(rows),
        elapsed_ms=elapsed_ms,
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


def audit_log(
    *,
    log_path: Path,
    database: str,
    sql: str,
    status: str,
    ticket_id: str,
    row_count: int = 0,
    elapsed_ms: int = 0,
    error: Optional[str] = None,
    **extra: Any,
) -> None:
    """Append one JSONL audit entry for a SQL execution.

    ``extra`` fields (e.g. ticket_id_missing, offset, limit, timing_ms) are
    added when not None.
    """
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    entry: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "database": database,
        "sql": sql,
        "status": status,
        "ticket_id": ticket_id,
        "ticket_id_missing": not (ticket_id or "").strip(),
        "row_count": row_count,
        "elapsed_ms": elapsed_ms,
    }
    for key, value in extra.items():
        if value is not None:
            entry[key] = value
    if error:
        entry["error"] = error
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
