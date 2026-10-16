from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from contextlib import suppress
from pathlib import Path
from typing import Any

from .settings import Settings


_ALLOWED_COMMANDS = {"errors", "traceback", "query", "schema", "services"}
_READ_ONLY_SQL_START_RE = re.compile(r"^(SELECT|WITH|SHOW|DESCRIBE|EXPLAIN)\b", re.IGNORECASE)
_SCHEMA_TABLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")


def _base_command(sz_path: str) -> list[str]:
    path = Path(sz_path)
    if path.exists():
        return [sys.executable, str(path)]
    return [sz_path]


def _validate_args(args: Any) -> list[str]:
    if not isinstance(args, list) or not args:
        raise ValueError("args must be a non-empty string array")
    validated: list[str] = []
    for arg in args:
        if not isinstance(arg, str):
            raise ValueError("args must contain only strings")
        if "\x00" in arg:
            raise ValueError("args must not contain null bytes")
        if len(arg) > 65536:
            raise ValueError("individual args must not exceed 65536 characters")
        validated.append(arg)

    command = validated[0]
    if command not in _ALLOWED_COMMANDS:
        raise ValueError(f"unsupported sz command: {command}")
    if command == "query":
        if len(validated) < 2:
            raise ValueError("query requires SQL")
        sql = validated[1].strip()
        if (
            not _READ_ONLY_SQL_START_RE.match(sql)
            or ";" in sql
            or re.search(r"\bINTO\s+OUTFILE\b", sql, re.IGNORECASE)
        ):
            raise ValueError("raw SQL must be a single read-only query")
    if command == "schema" and len(validated) >= 2:
        if not _SCHEMA_TABLE_RE.fullmatch(validated[1]):
            raise ValueError("schema table name is invalid")
    return validated


async def _kill_process(proc: asyncio.subprocess.Process) -> None:
    with suppress(ProcessLookupError):
        proc.kill()
    with suppress(Exception):
        await asyncio.wait_for(proc.communicate(), timeout=5)


async def run_sz(args: Any, settings: Settings) -> dict[str, Any]:
    validated_args = _validate_args(args)
    env = dict(os.environ)
    if settings.signoz_url:
        env["SIGNOZ_URL"] = settings.signoz_url
    if settings.signoz_api_key:
        env["SIGNOZ_API_KEY"] = settings.signoz_api_key

    cmd = [*_base_command(settings.sz_path), *validated_args, "--json", "-q"]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(),
            timeout=settings.timeout_seconds,
        )
    except asyncio.TimeoutError:
        await _kill_process(proc)
        return {
            "success": False,
            "code": "SZ_TIMEOUT",
            "error": f"sz timed out after {settings.timeout_seconds}s",
        }
    except Exception as e:
        return {"success": False, "code": "SZ_SPAWN_FAILED", "error": str(e)}

    stderr = stderr_b.decode("utf-8", errors="replace").strip()
    stdout = stdout_b.decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        return {
            "success": False,
            "code": "SZ_NONZERO",
            "error": stderr[:1500] or f"sz exited with {proc.returncode}",
        }
    if not stdout:
        return {"success": False, "code": "SZ_EMPTY_OUTPUT", "error": "sz returned no output"}
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as e:
        return {
            "success": False,
            "code": "SZ_BAD_JSON",
            "error": f"sz returned invalid JSON: {e}",
        }
    return {"success": True, "data": data}
