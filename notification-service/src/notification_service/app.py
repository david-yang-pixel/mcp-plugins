from __future__ import annotations

from typing import Any, Optional

import uvicorn
from fastapi import Depends, FastAPI, Header, Request, Response
from fastapi.responses import PlainTextResponse

from . import tools as notification_tools
from .mcp import (
    check_auth,
    initialize_result,
    jsonrpc_error,
    tool_result,
    tools_list_result,
)
from .settings import Settings, get_settings


SERVICE_DEFS = {
    'notification-service': {
        "name": "notification-service",
        "tools": notification_tools.TOOLS,
        "handler": notification_tools.handle,
        "api_key_attr": "notification_api_key",
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
    async def sse(
        authorization: Optional[str] = Header(default=None),
        settings: Settings = Depends(get_settings),
    ) -> PlainTextResponse:
        _check_service_auth(spec, authorization, settings)
        return PlainTextResponse(
            "event: endpoint\ndata: /mcp\n\n",
            media_type="text/event-stream",
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
    return create_service_app('notification-service')


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


notification_app = create_service_app("notification-service")
app = create_app()


def _run(app_path: str, *, host: str, port: int) -> None:
    uvicorn.run(app_path, host=host, port=port, reload=False)






def main_notification() -> None:
    settings = get_settings()
    _run(
        "notification_service.app:notification_app",
        host=settings.notification_host,
        port=settings.notification_port,
    )




if __name__ == "__main__":
    main_notification()
