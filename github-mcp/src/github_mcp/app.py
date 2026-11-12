from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

import uvicorn
from fastapi import Depends, FastAPI, Header, Request, Response
from fastapi.responses import PlainTextResponse, StreamingResponse

from . import tools as github_tools
from .mcp import (
    audit_tool_call,
    check_auth_scope,
    configure_logging,
    failure,
    initialize_result,
    is_write_tool,
    jsonrpc_error,
    tool_result,
    tools_list_result,
)
from .settings import Settings, get_settings


SERVICE_DEFS = {
    'github-mcp': {
        "name": "github-mcp",
        "tools": github_tools.TOOLS,
        "handler": github_tools.handle,
        "api_key_attr": "github_api_key",
        "write_api_key_attr": "github_write_api_key",
    }
}


def create_service_app(service_name: str) -> FastAPI:
    spec = _service(service_name)
    app = FastAPI(title=spec["name"], version="0.1.0")

    @app.get("/health")
    async def health(settings: Settings = Depends(get_settings)) -> dict[str, Any]:
        return {
            "ok": True,
            "service": spec["name"],
            "api_key_configured": bool(getattr(settings, spec["api_key_attr"])),
            "write_enabled": bool(getattr(settings, "write_active", False)),
        }

    @app.get("/sse")
    @app.get("/mcp")
    async def sse(
        request: Request,
        authorization: Optional[str] = Header(default=None),
        settings: Settings = Depends(get_settings),
    ) -> StreamingResponse:
        _check_service_auth(spec, authorization, settings)
        endpoint = request.headers.get("X-MCP-Endpoint") or f"/{spec['name']}/mcp"

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
        scope = _check_service_auth(spec, authorization, settings)
        try:
            body = await request.json()
        except Exception:
            return jsonrpc_error(None, -32700, "invalid JSON")

        method = body.get("method")
        request_id = body.get("id")
        if request_id is None and isinstance(method, str) and method.startswith("notifications/"):
            return Response(status_code=202)
        if method == "initialize":
            return initialize_result(request_id, service_name=spec["name"])
        if method == "tools/list":
            return tools_list_result(request_id, _visible_tools(spec, scope, settings))
        if method == "ping":
            return {"jsonrpc": "2.0", "id": request_id, "result": {}}
        if method != "tools/call":
            return jsonrpc_error(request_id, -32601, f"unsupported method: {method}")

        params = body.get("params") or {}
        if not isinstance(params, dict):
            params = {}
        started = time.monotonic()
        response: Any = None
        tool = _find_tool(spec, params.get("name"))
        write = bool(tool and is_write_tool(tool))
        try:
            response = await _call_tool(spec, request_id, params, settings, scope)
            return response
        finally:
            audit_tool_call(
                tool=params.get("name"),
                arguments=params.get("arguments"),
                started=started,
                response=response,
                write=write,
                scope=scope,
            )

    return app


def _find_tool(spec: dict[str, Any], name: Any) -> Optional[dict[str, Any]]:
    return next((tool for tool in spec["tools"] if tool["name"] == name), None)


def _visible_tools(spec: dict[str, Any], scope: str, settings: Settings) -> list[dict[str, Any]]:
    show_write = scope == "write" and bool(getattr(settings, "write_active", False))
    return [tool for tool in spec["tools"] if show_write or not is_write_tool(tool)]


async def _call_tool(
    spec: dict[str, Any],
    request_id: Any,
    params: dict[str, Any],
    settings: Settings,
    scope: str = "read",
) -> dict[str, Any]:
    name = params.get("name")
    tool = _find_tool(spec, name)
    if tool is None:
        return jsonrpc_error(request_id, -32601, f"unknown tool: {name}")
    if is_write_tool(tool):
        if scope != "write":
            return tool_result(
                request_id,
                failure("FORBIDDEN_SCOPE", f"{name} is a write tool; connect with the write API key"),
            )
        if not getattr(settings, "write_active", False):
            return tool_result(
                request_id,
                failure("WRITE_DISABLED", "write tools are disabled on this server"),
            )
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        return tool_result(
            request_id,
            {"success": False, "code": "BAD_ARGUMENTS", "error": "arguments must be an object"},
        )
    payload = await spec["handler"](str(name), arguments, settings)
    return tool_result(request_id, payload)


def create_app() -> FastAPI:
    return create_service_app('github-mcp')


def _service(service: str) -> dict[str, Any]:
    if service not in SERVICE_DEFS:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail=f"unknown service: {service}")
    return SERVICE_DEFS[service]


def _check_service_auth(
    spec: dict[str, Any],
    authorization: Optional[str],
    settings: Settings,
) -> str:
    return check_auth_scope(
        authorization=authorization,
        read_key=str(getattr(settings, spec["api_key_attr"])),
        write_key=str(getattr(settings, spec.get("write_api_key_attr", ""), "") or ""),
        service_name=spec["name"],
    )


github_app = create_service_app("github-mcp")
app = create_app()


def _run(app_path: str, *, host: str, port: int) -> None:
    uvicorn.run(app_path, host=host, port=port, reload=False)


def main_github() -> None:
    configure_logging()
    settings = get_settings()
    _run("github_mcp.app:github_app", host=settings.github_host, port=settings.github_port)








if __name__ == "__main__":
    main_github()
