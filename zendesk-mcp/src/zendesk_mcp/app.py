from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Optional
from urllib.parse import quote

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from .mcp_schema import TOOL_SCHEMAS
from .settings import Settings, get_settings
from .tools import handle_tool_call


logger = logging.getLogger("zendesk_mcp")


def _check_auth(
    authorization: Optional[str],
    x_api_key: Optional[str],
    settings: Settings,
) -> None:
    if not settings.api_key:
        raise HTTPException(status_code=503, detail="ZENDESK_MCP_API_KEY is not configured")
    bearer = f"Bearer {settings.api_key}"
    if authorization == bearer:
        return
    if x_api_key == settings.api_key:
        return
    raise HTTPException(status_code=401, detail="Unauthorized")


def _attachment_block(data: dict[str, Any]) -> dict[str, Any] | None:
    if data.get("encoding") != "base64":
        return None
    encoded = data.get("data_base64")
    if not isinstance(encoded, str) or not encoded:
        return None
    mime_type = str(data.get("content_type") or "application/octet-stream")
    if mime_type.startswith("image/"):
        return {"type": "image", "data": encoded, "mimeType": mime_type}
    if mime_type.startswith("audio/"):
        return {"type": "audio", "data": encoded, "mimeType": mime_type}
    file_name = str(data.get("file_name") or "attachment")
    return {
        "type": "resource",
        "resource": {
            "uri": f"zendesk-attachment:///{quote(file_name, safe='')}",
            "mimeType": mime_type,
            "blob": encoded,
        },
    }


def _attachment_content(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if not payload.get("success"):
        return []
    candidates: list[dict[str, Any]] = []
    data = payload.get("data")
    if isinstance(data, dict):
        candidates.append(data)
    attachment_files = payload.get("_attachment_files")
    if isinstance(attachment_files, list):
        candidates.extend(item for item in attachment_files if isinstance(item, dict))
    return [block for item in candidates if (block := _attachment_block(item)) is not None]


def _tool_result(request_id: Any, payload: dict[str, Any]) -> dict[str, Any]:
    ok = bool(payload.get("success"))
    public_payload = {key: value for key, value in payload.items() if not key.startswith("_")}
    content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": json.dumps(public_payload, ensure_ascii=False),
        }
    ]
    content.extend(_attachment_content(payload))
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "content": content,
            "structuredContent": public_payload,
            "isError": not ok,
        },
    }


def _tools_list(request_id: Any) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {"tools": TOOL_SCHEMAS},
    }


def _jsonrpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {
            "code": code,
            "message": message,
        },
    }


def create_app() -> FastAPI:
    app = FastAPI(title="Zendesk MCP", version="0.1.0")

    @app.get("/health")
    async def health(settings: Settings = Depends(get_settings)) -> dict[str, Any]:
        return {
            "ok": True,
            "zendesk_configured": bool(
                settings.zendesk_subdomain
                and settings.zendesk_email
                and settings.zendesk_api_token
            ),
        }

    @app.get("/sse")
    @app.get("/mcp")
    async def sse(
        authorization: Optional[str] = Header(default=None),
        x_api_key: Optional[str] = Header(default=None),
        x_mcp_endpoint: Optional[str] = Header(default=None),
        settings: Settings = Depends(get_settings),
    ) -> StreamingResponse:
        _check_auth(authorization, x_api_key, settings)
        endpoint = x_mcp_endpoint or "/zendesk-mcp/mcp"

        async def event_generator():
            yield f"event: endpoint\ndata: {endpoint}\n\n"
            try:
                while True:
                    await asyncio.sleep(15)
                    yield ": ping\n\n"
            except (asyncio.CancelledError, ConnectionResetError):
                pass

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream; charset=utf-8",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/mcp", response_model=None)
    async def mcp(
        request: Request,
        authorization: Optional[str] = Header(default=None),
        x_api_key: Optional[str] = Header(default=None),
        settings: Settings = Depends(get_settings),
    ) -> Any:
        _check_auth(authorization, x_api_key, settings)
        try:
            body = await request.json()
        except Exception:
            return _jsonrpc_error(None, -32700, "invalid JSON")

        method = body.get("method")
        request_id = body.get("id")
        if request_id is None and isinstance(method, str) and method.startswith("notifications/"):
            return Response(status_code=202)

        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "zendesk-mcp", "version": "0.1.0"},
                },
            }
        if method == "tools/list":
            return _tools_list(request_id)
        if method == "ping":
            return {"jsonrpc": "2.0", "id": request_id, "result": {}}
        if method != "tools/call":
            return _jsonrpc_error(request_id, -32601, f"unsupported method: {method}")

        params = body.get("params") or {}
        name = params.get("name")
        if not any(tool["name"] == name for tool in TOOL_SCHEMAS):
            return _jsonrpc_error(request_id, -32601, f"unknown tool: {name}")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return _tool_result(
                request_id,
                {"success": False, "code": "BAD_ARGUMENTS", "error": "arguments must be an object"},
            )
        started = time.perf_counter()
        payload = await run_in_threadpool(handle_tool_call, name, arguments, settings=settings)
        logger.info(
            "tool_call name=%s success=%s elapsed=%.3f",
            name,
            payload.get("success"),
            time.perf_counter() - started,
        )
        return _tool_result(request_id, payload)

    return app


app = create_app()


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "zendesk_mcp.app:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        workers=settings.workers,
    )
