from __future__ import annotations

import asyncio
from typing import Any, Optional

import uvicorn
from fastapi import Depends, FastAPI, Header, Request, Response
from fastapi.responses import PlainTextResponse, StreamingResponse

from . import tools as github_tools
from .mcp import (
    check_auth,
    initialize_result,
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
        _check_service_auth(spec, authorization, settings)
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
            return tools_list_result(request_id, spec["tools"])
        if method == "ping":
            return {"jsonrpc": "2.0", "id": request_id, "result": {}}
        if method != "tools/call":
            return jsonrpc_error(request_id, -32601, f"unsupported method: {method}")

        params = body.get("params") or {}
        name = params.get("name")
        if not any(tool["name"] == name for tool in spec["tools"]):
            return jsonrpc_error(request_id, -32601, f"unknown tool: {name}")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return tool_result(
                request_id,
                {"success": False, "code": "BAD_ARGUMENTS", "error": "arguments must be an object"},
            )
        payload = await spec["handler"](str(name), arguments, settings)
        return tool_result(request_id, payload)

    return app


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
) -> None:
    check_auth(
        authorization=authorization,
        api_key=str(getattr(settings, spec["api_key_attr"])),
        service_name=spec["name"],
    )


github_app = create_service_app("github-mcp")
app = create_app()


def _run(app_path: str, *, host: str, port: int) -> None:
    uvicorn.run(app_path, host=host, port=port, reload=False)


def main_github() -> None:
    settings = get_settings()
    _run("github_mcp.app:github_app", host=settings.github_host, port=settings.github_port)








if __name__ == "__main__":
    main_github()
