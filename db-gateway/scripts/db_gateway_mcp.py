#!/usr/bin/env python3
"""Minimal stdio MCP server for DB Gateway.

Exposes the tools of the HTTP MCP endpoint (schemas shared with it):
  - db_gateway_query(database, sql, offset=0, limit=None, format="csv", explain=false)
    -> POST <DB_GATEWAY_URL>/query
  - db_gateway_schema(database, tables) -> POST <DB_GATEWAY_URL>/schema

The MCP server itself does not hold Teleport credentials. It forwards requests
to the DB Gateway HTTP API, which owns Teleport, DB proxy, renewal, SQL safety,
and audit logging.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib import error as urlerror
from urllib import request

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from db_gateway.mcp_schema import SCHEMA_TOOL_SCHEMA, TOOL_SCHEMA, TOOLS


def _send(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _result(req_id: Any, result: dict[str, Any]) -> None:
    _send({"jsonrpc": "2.0", "id": req_id, "result": result})


def _error(req_id: Any, code: int, message: str) -> None:
    _send({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})


def _gateway_query(args: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "database": args.get("database"),
        "sql": args.get("sql"),
    }
    # Only forward optional args when given, so older gateways keep working.
    # Unknown arguments (e.g. the retired ticket_id) are dropped.
    for key in ("offset", "limit", "format", "explain"):
        if args.get(key) is not None:
            payload[key] = args[key]
    return _gateway_post("/query", payload)


def _gateway_schema(args: dict[str, Any]) -> dict[str, Any]:
    return _gateway_post("/schema", {
        "database": args.get("database"),
        "tables": args.get("tables"),
    })


def _gateway_post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    base_url = os.environ.get("DB_GATEWAY_URL", "").rstrip("/")
    api_key = os.environ.get("DB_GATEWAY_API_KEY", "")
    # Gateway budget = query_timeout_seconds (30) + count_timeout_seconds (5) + connect.
    timeout = float(os.environ.get("DB_GATEWAY_TIMEOUT", "45"))
    if not base_url:
        return {
            "success": False,
            "code": "NOT_CONFIGURED",
            "error": "DB_GATEWAY_URL is not configured",
        }
    if not api_key:
        return {
            "success": False,
            "code": "NOT_CONFIGURED",
            "error": "DB_GATEWAY_API_KEY is not configured",
        }

    headers = {"X-API-Key": api_key}
    body = json.dumps(payload).encode("utf-8")
    req = request.Request(
        f"{base_url}{path}",
        data=body,
        headers={**headers, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", errors="replace")
            return json.loads(text or "{}")
    except urlerror.HTTPError as e:
        text = e.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(text)
        except Exception:
            data = {"error": text[:500]}
        if isinstance(data, dict) and "success" in data:
            return data
        return {
            "success": False,
            "code": f"HTTP_{e.code}",
            "error": data.get("error") if isinstance(data, dict) else str(data),
        }
    except Exception as e:
        return {"success": False, "code": "HTTP_EXCEPTION", "error": str(e)}


def _handle(req: dict[str, Any]) -> None:
    req_id = req.get("id")
    method = req.get("method")
    if req_id is None:
        return

    if method == "initialize":
        _result(req_id, {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "db-gateway-mcp", "version": "0.1.0"},
        })
        return

    if method == "tools/list":
        _result(req_id, {"tools": TOOLS})
        return

    if method == "tools/call":
        params = req.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            _error(req_id, -32602, "arguments must be an object")
            return
        if name == TOOL_SCHEMA["name"]:
            data = _gateway_query(args)
        elif name == SCHEMA_TOOL_SCHEMA["name"]:
            data = _gateway_schema(args)
        else:
            _error(req_id, -32601, f"unknown tool: {name}")
            return
        _result(req_id, {
            "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}],
            "isError": not bool(data.get("success")),
        })
        return

    _error(req_id, -32601, f"unknown method: {method}")


def main() -> None:
    for line in sys.stdin:
        try:
            req = json.loads(line)
        except Exception:
            continue
        try:
            _handle(req)
        except Exception as e:
            if isinstance(req, dict) and req.get("id") is not None:
                _error(req.get("id"), -32000, str(e))


if __name__ == "__main__":
    main()
