"""Standalone aiohttp app for DB Gateway."""
from __future__ import annotations

import asyncio
from typing import Any

from aiohttp import web

from db_gateway.config import get_config
from db_gateway.app_context import (
    CONFIG_KEY,
    TELEPORT_AUTH_TOKEN_STORE_KEY,
)
from db_gateway.core import execute_gateway_query
from db_gateway.mcp import handle_mcp_message, mcp_sse_response
from db_gateway.teleport_auth_web import (
    handle_teleport_auth_get,
    handle_teleport_auth_issue,
    handle_teleport_auth_post,
)
from db_gateway.teleport_tokens import TokenStore


APP_CONFIG_KEY: web.AppKey[Any] = web.AppKey("db_gateway.http.config", object)


async def handle_healthz(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "service": "db-gateway"})


def _authorized(request: web.Request, cfg: Any) -> bool:
    if not cfg.db_gateway.api_key:
        return False
    api_key = request.headers.get("X-API-Key", "")
    auth = request.headers.get("Authorization", "")
    bearer = ""
    if auth.lower().startswith("bearer "):
        bearer = auth[7:].strip()
    return api_key == cfg.db_gateway.api_key or bearer == cfg.db_gateway.api_key


async def handle_status(request: web.Request) -> web.Response:
    cfg = request.app[APP_CONFIG_KEY]
    if not _authorized(request, cfg):
        return web.json_response({"error": "unauthorized"}, status=401)
    return web.json_response({
        "ok": True,
        "postgres_host": cfg.postgres.host,
        "ports": {
            "prod": cfg.postgres.port_prod,
            "billing": cfg.postgres.port_billing,
            "plugin": cfg.postgres.port_plugin,
        },
        "teleport_proxy": cfg.teleport.proxy,
    })


async def handle_query(request: web.Request) -> web.Response:
    cfg = request.app[APP_CONFIG_KEY]
    if not _authorized(request, cfg):
        return web.json_response({"error": "unauthorized"}, status=401)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    result = await execute_gateway_query(
        cfg=cfg,
        database=str(body.get("database") or ""),
        sql=str(body.get("sql") or ""),
        ticket_id=str(body.get("ticket_id") or ""),
    )
    status = 200 if result.success else 400
    if result.code == "TELEPORT_EXPIRED":
        status = 503
    return web.json_response(result.to_json(), status=status)


async def handle_mcp_post(request: web.Request) -> web.Response:
    cfg = request.app[APP_CONFIG_KEY]
    if not _authorized(request, cfg):
        return web.json_response({"error": "unauthorized"}, status=401)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32700, "message": "invalid json"},
        }, status=400)

    if not isinstance(body, dict):
        return web.json_response({
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32600, "message": "invalid request"},
        }, status=400)

    response = await handle_mcp_message(body, cfg=cfg)
    if response is None:
        return web.Response(status=202)
    return web.json_response(response)


async def handle_mcp_get(request: web.Request) -> web.StreamResponse:
    cfg = request.app[APP_CONFIG_KEY]
    if not _authorized(request, cfg):
        return web.json_response({"error": "unauthorized"}, status=401)

    response = web.StreamResponse(
        status=200,
        reason="OK",
        headers={
            "Content-Type": "text/event-stream; charset=utf-8",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
    await response.prepare(request)

    endpoint = "/db-gateway/mcp"
    await response.write(f"event: endpoint\ndata: {endpoint}\n\n".encode("utf-8"))

    try:
        while True:
            await asyncio.sleep(15)
            await response.write(b": ping\n\n")
    except (asyncio.CancelledError, ConnectionResetError):
        pass

    return response


def create_db_gateway_app(cfg: Any | None = None) -> web.Application:
    cfg = cfg or get_config()
    app = web.Application()
    app[APP_CONFIG_KEY] = cfg
    app[CONFIG_KEY] = cfg
    app[TELEPORT_AUTH_TOKEN_STORE_KEY] = TokenStore()
    app.router.add_get("/healthz", handle_healthz)
    app.router.add_get("/status", handle_status)
    app.router.add_post("/query", handle_query)
    app.router.add_get("/mcp", handle_mcp_get)
    app.router.add_get("/sse", handle_mcp_get)
    app.router.add_post("/mcp", handle_mcp_post)
    app.router.add_get("/teleport-auth", handle_teleport_auth_get)
    app.router.add_post("/teleport-auth", handle_teleport_auth_post)
    app.router.add_post("/internal/teleport-auth/issue", handle_teleport_auth_issue)
    return app


def run_db_gateway_app() -> None:
    cfg = get_config()
    app = create_db_gateway_app(cfg)
    web.run_app(
        app,
        host=cfg.db_gateway.listen_host,
        port=cfg.db_gateway.listen_port,
    )
