from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from typing import Any, Optional

import uvicorn
from fastapi import Depends, FastAPI, Header, Request, Response
from fastapi.responses import PlainTextResponse, StreamingResponse

from . import tools as docs_tools
from .mcp import (
    check_auth,
    initialize_result,
    jsonrpc_error,
    tool_result,
    tools_list_result,
)
from .settings import Settings, get_settings

# One JSON line per tools/call, same shape as difyctl-mcp's audit log.
audit = logging.getLogger("docs_tool_service.audit")

# Argument values that are safe to log. Everything else (search queries, URLs,
# unknown keys) is recorded by key name only, with a null value.
AUDIT_VALUE_KEYS = frozenset(
    {
        "source",
        "language",
        "lang",
        "version",
        "edition",
        "section",
        "limit",
        "offset",
        "max_chars",
        "cursor",
    }
)
AUDIT_MAX_VALUE_CHARS = 64

SERVICE_DEFS = {
    'docs-tool-service': {
        "name": "docs-tool-service",
        "tools": docs_tools.TOOLS,
        "handler": docs_tools.handle,
        "api_key_attr": "docs_api_key",
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

        started = time.monotonic()
        params = body.get("params") or {}
        name = params.get("name") if isinstance(params, dict) else None
        arguments = (params.get("arguments") or {}) if isinstance(params, dict) else {}
        response: Any = None
        try:
            if not any(tool["name"] == name for tool in spec["tools"]):
                response = jsonrpc_error(request_id, -32601, f"unknown tool: {name}")
                return response
            if not isinstance(arguments, dict):
                response = tool_result(
                    request_id,
                    {
                        "success": False,
                        "code": "BAD_ARGUMENTS",
                        "error": "arguments must be an object",
                    },
                )
                return response
            payload = await spec["handler"](str(name), arguments, settings)
            response = tool_result(request_id, payload)
            return response
        finally:
            _audit_tool_call(name, arguments, response, started)

    return app


def _audit_tool_call(name: Any, arguments: Any, response: Any, started: float) -> None:
    """Log one JSON line for a tools/call. Never raises: logging must not break a call."""
    try:
        error: Any = None
        response_chars = 0
        if isinstance(response, dict) and isinstance(response.get("result"), dict):
            result = response["result"]
            payload = result.get("structuredContent")
            outcome = "failed" if result.get("isError") else "success"
            if outcome == "failed" and isinstance(payload, dict):
                error = payload.get("code") or "TOOL_ERROR"
            response_chars = sum(
                len(item.get("text") or "")
                for item in result.get("content") or []
                if isinstance(item, dict)
            )
        elif isinstance(response, dict) and isinstance(response.get("error"), dict):
            outcome, error = "failed", response["error"].get("code")
            response_chars = len(str(response["error"].get("message") or ""))
        else:
            # The handler raised; the exception propagates after this line.
            outcome, error = "failed", "INTERNAL_ERROR"
        audit.info(
            json.dumps(
                {
                    "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "tool": str(name)[:100] if name is not None else None,
                    "duration_ms": int((time.monotonic() - started) * 1000),
                    "outcome": outcome,
                    "error": error,
                    "response_chars": response_chars,
                    "args": _audit_args(arguments),
                },
                ensure_ascii=False,
            )
        )
    except Exception:  # noqa: BLE001 - audit logging is best effort
        pass


def _audit_args(arguments: Any) -> dict[str, Any] | None:
    """Argument key names; values only for allow-listed, scalar, short arguments."""
    if not isinstance(arguments, dict):
        return None
    logged: dict[str, Any] = {}
    for key in sorted(str(key)[:100] for key in arguments)[:32]:
        value = arguments.get(key)
        if key in AUDIT_VALUE_KEYS and (
            isinstance(value, (bool, int, float))
            or (isinstance(value, str) and len(value) <= AUDIT_MAX_VALUE_CHARS)
        ):
            logged[key] = value
        else:
            logged[key] = None
    return logged


def configure_audit_logging(stream: Any = None) -> None:
    """Send audit lines to stdout at INFO, independent of root/uvicorn logging.

    A dedicated handler (instead of a root ``basicConfig``) keeps other INFO logs,
    such as one httpx line per fetched docs page, out of stdout.
    """
    handler = next(
        (item for item in audit.handlers if getattr(item, "_docs_tool_audit", False)), None
    )
    if handler is None:
        handler = logging.StreamHandler(stream or sys.stdout)
        handler._docs_tool_audit = True  # type: ignore[attr-defined]
        handler.setFormatter(logging.Formatter("%(message)s"))
        audit.addHandler(handler)
    elif stream is not None:
        handler.setStream(stream)
    audit.setLevel(logging.INFO)
    audit.propagate = False


def create_app() -> FastAPI:
    return create_service_app('docs-tool-service')


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


docs_app = create_service_app("docs-tool-service")
app = create_app()


def _run(app_path: str, *, host: str, port: int) -> None:
    uvicorn.run(app_path, host=host, port=port, reload=False)




def main_docs() -> None:
    configure_audit_logging()
    settings = get_settings()
    _run("docs_tool_service.app:docs_app", host=settings.docs_host, port=settings.docs_port)






if __name__ == "__main__":
    main_docs()
