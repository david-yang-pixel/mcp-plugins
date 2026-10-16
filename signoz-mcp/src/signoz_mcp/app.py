from __future__ import annotations

import asyncio
import copy
import json
import time
from dataclasses import dataclass
from typing import Any, Optional

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse, StreamingResponse

from .settings import Settings, get_settings
from .structured_tools import STRUCTURED_TOOLS, build_structured_sz_args
from .sz_runner import run_sz


@dataclass
class _CacheEntry:
    expires_at: float
    payload: dict[str, Any]


_RUNTIME_LOCK = asyncio.Lock()
_RUN_LIMITERS: dict[int, asyncio.Semaphore] = {}
_RESULT_CACHE: dict[tuple[str, ...], _CacheEntry] = {}
_INFLIGHT_RUNS: dict[tuple[str, ...], asyncio.Task[dict[str, Any]]] = {}


def _check_auth(
    authorization: Optional[str],
    settings: Settings,
) -> None:
    if not settings.api_key:
        raise HTTPException(status_code=503, detail="SIGNOZ_MCP_API_KEY is not configured")
    expected = f"Bearer {settings.api_key}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")


def _tool_result(
    request_id: Any,
    payload: dict[str, Any],
) -> dict[str, Any]:
    ok = bool(payload.get("success"))
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(payload, ensure_ascii=False),
                },
            ],
            "structuredContent": payload,
            "isError": not ok,
        },
    }


def _tools_list(request_id: Any) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "tools": [
                {
                    "name": "sz_run",
                    "description": (
                        "Run an allowed read-only server-side sz command with JSON output. "
                        "Raw SQL must be one SELECT/WITH/SHOW/DESCRIBE/EXPLAIN query."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "args": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Arguments passed to sz, excluding --json and -q.",
                            },
                        },
                        "required": ["args"],
                    },
                },
                *STRUCTURED_TOOLS,
            ],
        },
    }


def _reset_runtime_state() -> None:
    _RUN_LIMITERS.clear()
    _RESULT_CACHE.clear()
    _INFLIGHT_RUNS.clear()


def _args_key(args: Any) -> tuple[str, ...] | None:
    if not isinstance(args, list) or not args:
        return None
    key: list[str] = []
    for arg in args:
        if not isinstance(arg, str) or "\x00" in arg:
            return None
        key.append(arg)
    return tuple(key)


def _cache_ttl(args_key: tuple[str, ...], settings: Settings) -> float:
    if not args_key:
        return 0.0
    if args_key[0] not in settings.cacheable_command_set:
        return 0.0
    return max(0.0, float(settings.cache_ttl_seconds or 0.0))


def _prune_result_cache(now: float, max_entries: int) -> None:
    for key, entry in list(_RESULT_CACHE.items()):
        if entry.expires_at <= now:
            _RESULT_CACHE.pop(key, None)
    while len(_RESULT_CACHE) >= max_entries:
        oldest_key = min(_RESULT_CACHE, key=lambda key: _RESULT_CACHE[key].expires_at)
        _RESULT_CACHE.pop(oldest_key, None)


async def _run_limiter(settings: Settings) -> asyncio.Semaphore:
    max_concurrent = max(1, int(settings.max_concurrent_runs or 1))
    async with _RUNTIME_LOCK:
        limiter = _RUN_LIMITERS.get(max_concurrent)
        if limiter is None:
            limiter = asyncio.Semaphore(max_concurrent)
            _RUN_LIMITERS[max_concurrent] = limiter
        return limiter


async def _run_sz_limited(args: Any, settings: Settings) -> dict[str, Any]:
    limiter = await _run_limiter(settings)
    async with limiter:
        return await run_sz(args, settings)


def _schedule_inflight_cleanup(
    args_key: tuple[str, ...],
    task: asyncio.Task[dict[str, Any]],
) -> None:
    async def cleanup() -> None:
        async with _RUNTIME_LOCK:
            if _INFLIGHT_RUNS.get(args_key) is task:
                _INFLIGHT_RUNS.pop(args_key, None)

    asyncio.create_task(cleanup())


async def _run_sz_controlled(args: Any, settings: Settings) -> dict[str, Any]:
    args_key = _args_key(args)
    if args_key is None:
        return await _run_sz_limited(args, settings)

    ttl = _cache_ttl(args_key, settings)
    if ttl:
        now = time.monotonic()
        async with _RUNTIME_LOCK:
            entry = _RESULT_CACHE.get(args_key)
            if entry and entry.expires_at > now:
                return copy.deepcopy(entry.payload)
            if entry:
                _RESULT_CACHE.pop(args_key, None)

    if not settings.singleflight_enabled:
        payload = await _run_sz_limited(args, settings)
    else:
        owner = False
        async with _RUNTIME_LOCK:
            task = _INFLIGHT_RUNS.get(args_key)
            if task is None:
                task = asyncio.create_task(_run_sz_limited(args, settings))
                _INFLIGHT_RUNS[args_key] = task
                owner = True
        try:
            payload = await asyncio.shield(task)
        finally:
            if owner:
                if task.done():
                    async with _RUNTIME_LOCK:
                        if _INFLIGHT_RUNS.get(args_key) is task:
                            _INFLIGHT_RUNS.pop(args_key, None)
                else:
                    task.add_done_callback(
                        lambda finished: _schedule_inflight_cleanup(args_key, finished)
                    )

    max_cache_entries = max(0, int(settings.cache_max_entries or 0))
    if ttl and max_cache_entries and payload.get("success"):
        async with _RUNTIME_LOCK:
            now = time.monotonic()
            _prune_result_cache(now, max_cache_entries)
            _RESULT_CACHE[args_key] = _CacheEntry(
                expires_at=now + ttl,
                payload=copy.deepcopy(payload),
            )
    return copy.deepcopy(payload)


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
    app = FastAPI(title="SigNoz MCP", version="0.2.0")

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"ok": True}

    @app.get("/sse")
    @app.get("/mcp")
    async def sse(
        authorization: Optional[str] = Header(default=None),
        x_mcp_endpoint: Optional[str] = Header(default=None),
        settings: Settings = Depends(get_settings),
    ) -> StreamingResponse:
        _check_auth(authorization, settings)
        endpoint = x_mcp_endpoint or "/signoz-mcp/mcp"

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
        settings: Settings = Depends(get_settings),
    ) -> Any:
        _check_auth(authorization, settings)
        body = await request.json()
        method = body.get("method")
        request_id = body.get("id")

        # MCP clients send notifications during startup. Notifications have no
        # request id, so they must not return JSON-RPC errors or HTTP 400.
        if request_id is None and isinstance(method, str) and method.startswith("notifications/"):
            return Response(status_code=202)

        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "signoz-mcp", "version": "0.2.0"},
                },
            }
        if method == "tools/list":
            return _tools_list(request_id)
        if method == "ping":
            return {"jsonrpc": "2.0", "id": request_id, "result": {}}
        if method != "tools/call":
            return _jsonrpc_error(request_id, -32601, f"unsupported method: {method}")

        params = body.get("params") or {}
        tool_name = params.get("name")
        arguments = params.get("arguments") or {}
        try:
            if tool_name == "sz_run":
                args = arguments.get("args")
            else:
                args = build_structured_sz_args(str(tool_name or ""), arguments)
            payload = await _run_sz_controlled(args, settings)
        except ValueError as e:
            payload = {"success": False, "code": "BAD_ARGUMENTS", "error": str(e)}
        return _tool_result(request_id, payload)

    return app


app = create_app()


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "signoz_mcp.app:app",
        host=settings.host,
        port=settings.port,
        reload=False,
    )
