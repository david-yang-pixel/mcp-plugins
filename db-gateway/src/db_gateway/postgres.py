"""PostgreSQL integration: read-only SQL validator + psql subprocess executor."""
from __future__ import annotations

import re
from contextlib import suppress

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


def validate_read_only_sql(sql: str) -> None:
    """Validate SQL is read-only. Raises PsqlSafetyError on violation.

    Triple-check:
    1. Strip comments (-- line and /* block */).
    2. Reject multiple statements (';' after trimming trailing).
    3. Keyword blacklist (word-boundary, case-insensitive).
    4. Require first significant word is SELECT or WITH.
    """
    # Strip line comments and block comments
    cleaned = re.sub(r"--[^\n]*", "", sql)
    cleaned = re.sub(r"/\*.*?\*/", "", cleaned, flags=re.DOTALL)

    # Reject multiple statements
    stripped = cleaned.strip().rstrip(";").strip()
    if ";" in stripped:
        raise PsqlSafetyError("SQL contains multiple statements; only one SELECT allowed")

    # Keyword blacklist
    upper = cleaned.upper()
    for kw in _FORBIDDEN_KEYWORDS:
        if re.search(rf"\b{kw}\b", upper):
            raise PsqlSafetyError(f"forbidden keyword detected: {kw}")

    # Must start with SELECT or WITH
    first_word = re.match(r"\s*(\w+)", cleaned)
    if not first_word:
        raise PsqlSafetyError("empty SQL")
    fw = first_word.group(1).upper()
    if fw not in ("SELECT", "WITH"):
        raise PsqlSafetyError(f"SQL must start with SELECT or WITH, got: {fw}")


import asyncio
import os
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class PsqlResult:
    csv: str
    row_count: int
    elapsed_ms: int
    truncated: bool


async def run_psql(
    *,
    host: str,
    port: int,
    user: str,
    password: str,
    database: str,
    sql: str,
    timeout: int,
    max_rows: int,
) -> PsqlResult:
    """Execute SQL via psql subprocess, return CSV result.

    Raises:
        TeleportAuthExpired: connection failed (likely auth/port-forward down)
        asyncio.TimeoutError: query exceeded timeout
        RuntimeError: other psql errors
    """
    # Wrap SQL in LIMIT guard (agent may forget LIMIT on SELECT *)
    wrapped = f"SELECT * FROM ({sql.rstrip(';').rstrip()}) _db_gateway_wrap LIMIT {max_rows + 1};"

    cmd = [
        "psql",
        "-h", host,
        "-p", str(port),
        "-U", user,
        "-d", database,
        "--csv",
        "-A",
        "-F", ",",
        "-c", wrapped,
    ]

    env = dict(os.environ)
    env["PGPASSWORD"] = password
    env["PGCONNECT_TIMEOUT"] = "10"
    # Teleport `tsh proxy db --tunnel` 已在内部做 mTLS，psql 本地连接无需 SSL
    env.setdefault("PGSSLMODE", "disable")

    start = datetime.now(timezone.utc)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        with suppress(ProcessLookupError):
            proc.kill()
        with suppress(Exception):
            await asyncio.wait_for(proc.communicate(), timeout=5)
        raise

    elapsed_ms = int((datetime.now(timezone.utc) - start).total_seconds() * 1000)

    if proc.returncode != 0:
        stderr_text = stderr.decode("utf-8", errors="replace")
        if any(sig in stderr_text for sig in (
            "Connection refused",
            "could not connect to server",
            "no such host",
            "connection reset",
        )):
            raise TeleportAuthExpired(stderr_text)
        raise RuntimeError(f"psql failed (rc={proc.returncode}): {stderr_text}")

    csv_text = stdout.decode("utf-8", errors="replace")
    lines = csv_text.splitlines()
    # --csv includes header row
    row_count = max(0, len(lines) - 1)
    truncated = row_count > max_rows
    if truncated:
        lines = lines[: max_rows + 1]
        csv_text = "\n".join(lines)
        row_count = max_rows

    return PsqlResult(
        csv=csv_text,
        row_count=row_count,
        elapsed_ms=elapsed_ms,
        truncated=truncated,
    )


import json
from pathlib import Path
from typing import Optional


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
) -> None:
    """Append one JSONL audit entry for a SQL execution."""
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "database": database,
        "sql": sql,
        "status": status,
        "ticket_id": ticket_id,
        "row_count": row_count,
        "elapsed_ms": elapsed_ms,
    }
    if error:
        entry["error"] = error
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
