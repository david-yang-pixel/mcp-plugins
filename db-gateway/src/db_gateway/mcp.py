"""MCP JSON-RPC handlers for DB Gateway."""
from __future__ import annotations

import json
from typing import Any

from aiohttp import web

from db_gateway.core import execute_gateway_query
from db_gateway.mcp_schema import TOOL_SCHEMA


MCP_PROTOCOL_VERSION = "2025-06-18"

def _jsonrpc_result(req_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _jsonrpc_error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": code, "message": message},
    }


async def handle_mcp_message(message: dict[str, Any], *, cfg: Any) -> dict[str, Any] | None:
    """Handle one MCP JSON-RPC message.

    Returns None for accepted notifications, which HTTP transport maps to 202.
    """
    req_id = message.get("id")
    method = message.get("method")
    if req_id is None:
        return None

    if method == "initialize":
        requested_version = (
            (message.get("params") or {}).get("protocolVersion")
            or MCP_PROTOCOL_VERSION
        )
        return _jsonrpc_result(req_id, {
            "protocolVersion": str(requested_version),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "db-gateway-mcp", "version": "0.1.0"},
        })

    if method == "tools/list":
        return _jsonrpc_result(req_id, {"tools": [TOOL_SCHEMA]})

    if method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        if name != "db_gateway_query":
            return _jsonrpc_error(req_id, -32601, f"unknown tool: {name}")

        args = params.get("arguments") or {}
        data = await execute_gateway_query(
            cfg=cfg,
            database=str(args.get("database") or ""),
            sql=str(args.get("sql") or ""),
            ticket_id=str(args.get("ticket_id") or ""),
        )
        payload = data.to_json()
        return _jsonrpc_result(req_id, {
            "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
            "isError": not payload.get("success", False),
        })

    return _jsonrpc_error(req_id, -32601, f"unknown method: {method}")


def mcp_sse_response(message: dict[str, Any] | None = None) -> web.Response:
    """Return a tiny SSE response for clients that probe GET /mcp."""
    data = ""
    if message is not None:
        data = f"event: message\ndata: {json.dumps(message, ensure_ascii=False)}\n\n"
    else:
        data = ": db-gateway-mcp ready\n\n"
    return web.Response(
        text=data,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
