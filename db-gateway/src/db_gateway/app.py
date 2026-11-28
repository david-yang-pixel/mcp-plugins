"""Standalone aiohttp app for DB Gateway."""
from __future__ import annotations

import asyncio
import hmac
from typing import Any

from aiohttp import web

from db_gateway.config import get_config
from db_gateway.app_context import (
    CONFIG_KEY,
    TELEPORT_AUTH_TOKEN_STORE_KEY,
)
from db_gateway.core import execute_gateway_query, execute_schema_lookup
from db_gateway.mcp import handle_mcp_message, mcp_sse_response
from db_gateway.log import get_logger
from db_gateway.teleport_auth_web import (
    SESSION_LINK_TTL_MINUTES,
    handle_teleport_auth_get,
    handle_teleport_auth_issue,
    handle_teleport_auth_post,
    issue_link,
)
from db_gateway.teleport_tokens import SCOPE_SESSION, TokenStore
from db_gateway.tsh_daemons import stop_all as stop_tsh_daemons
from db_gateway.tsh_daemons import tunnel_spec

logger = get_logger(module="app")


APP_CONFIG_KEY: web.AppKey[Any] = web.AppKey("db_gateway.http.config", object)
MAINTENANCE_TASK_KEY: web.AppKey[asyncio.Task] = web.AppKey("db_gateway.maintenance_task", asyncio.Task)


async def handle_healthz(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "service": "db-gateway"})


def _same(supplied: str, expected: str) -> bool:
    return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


def _authorized(request: web.Request, cfg: Any) -> bool:
    expected = cfg.db_gateway.api_key
    if not isinstance(expected, str) or not expected:
        return False
    api_key = request.headers.get("X-API-Key", "")
    auth = request.headers.get("Authorization", "")
    bearer = ""
    if auth.lower().startswith("bearer "):
        bearer = auth[7:].strip()
    # Evaluate both: no shortcut that tells which header was checked.
    return _same(api_key, expected) | _same(bearer, expected)


def _database_routes(cfg: Any) -> dict[str, dict[str, Any]]:
    """Per database: db-gateway's own tunnel, or the host tunnel's static port."""
    routes = {}
    for key in ("prod", "billing", "plugin", "knowledgefs"):
        spec = tunnel_spec(cfg, key)
        if spec is not None:
            routes[key] = {"via": "db-gateway", "teleport_db": spec.teleport_db, "port": spec.port}
        else:
            routes[key] = {"via": "static", "port": getattr(cfg.postgres, f"port_{key}")}
    return routes


async def handle_status(request: web.Request) -> web.Response:
    cfg = request.app[APP_CONFIG_KEY]
    if not _authorized(request, cfg):
        return web.json_response({"error": "unauthorized"}, status=401)
    from db_gateway.teleport_session import SESSION

    routes = _database_routes(cfg)
    return web.json_response({
        "ok": True,
        "postgres_host": cfg.postgres.host,
        "ports": {key: route["port"] for key, route in routes.items()},
        "databases": routes,
        "teleport_proxy": cfg.teleport.proxy,
        "teleport_session": {**SESSION.status_info(), "auto_login": SESSION.can_auto_login(cfg)},
    })


async def handle_query(request: web.Request) -> web.Response:
    cfg = request.app[APP_CONFIG_KEY]
    if not _authorized(request, cfg):
        return web.json_response({"error": "unauthorized"}, status=401)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    if not isinstance(body, dict):
        return web.json_response({"error": "invalid json"}, status=400)

    result = await execute_gateway_query(
        cfg=cfg,
        database=str(body.get("database") or ""),
        sql=str(body.get("sql") or ""),
        offset=body.get("offset"),
        limit=body.get("limit"),
        output_format=body.get("format") or "csv",
        explain=body.get("explain"),
        count_total=body.get("count_total"),
    )
    status = 200 if result.success else 400
    if result.code == "TELEPORT_EXPIRED":
        status = 503
    return web.json_response(result.to_json(), status=status)


async def handle_schema(request: web.Request) -> web.Response:
    """HTTP form of the db_gateway_schema tool (used by the stdio MCP client)."""
    cfg = request.app[APP_CONFIG_KEY]
    if not _authorized(request, cfg):
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "invalid json"}, status=400)

    result = await execute_schema_lookup(
        cfg=cfg,
        database=str(body.get("database") or ""),
        tables=body.get("tables"),
    )
    status = 200 if result.get("success") else 400
    if result.get("code") == "TELEPORT_EXPIRED":
        status = 503
    return web.json_response(result, status=status)


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
    app.router.add_post("/schema", handle_schema)
    app.router.add_get("/mcp", handle_mcp_get)
    app.router.add_get("/sse", handle_mcp_get)
    app.router.add_post("/mcp", handle_mcp_post)
    app.router.add_get("/teleport-auth", handle_teleport_auth_get)
    app.router.add_post("/teleport-auth", handle_teleport_auth_post)
    app.router.add_post("/internal/teleport-auth/issue", handle_teleport_auth_issue)
    app.on_startup.append(_start_maintenance)
    app.on_cleanup.append(_stop_maintenance)   # before the pools it maintains
    app.on_cleanup.append(_close_pg_pools)
    app.on_cleanup.append(_stop_tsh_daemons)
    app.on_cleanup.append(_stop_teleport_session)
    return app


def _teleport_alert(app: web.Application):
    """Feishu alerts for the session loop; a manual-login alert carries a fresh link."""
    cfg = app[APP_CONFIG_KEY]
    webhook = getattr(getattr(cfg, "feishu", None), "webhook_url", "")
    if not isinstance(webhook, str) or not webhook:
        return None
    from db_gateway.feishu import FeishuClient

    feishu = FeishuClient(webhook_url=webhook, webhook_secret=cfg.feishu.webhook_secret or "")

    async def alert(title: str, message: str, link: bool) -> None:
        if link:
            url = await issue_link(app, SCOPE_SESSION, ttl_minutes=SESSION_LINK_TTL_MINUTES)
            message += (f"\n\n认证链接（一次性，{SESSION_LINK_TTL_MINUTES} 分钟内有效）：{url}" if url
                        else "\n\n（未配置 TELEPORT_PUBLIC_BASE_URL，无法生成认证链接）")
        await feishu.send_alert(title=title, message=message, urgent=True)

    return alert


async def _start_maintenance(app: web.Application) -> None:
    """Teleport session renewal and pool keepalive in the background (maintenance.py).

    Never blocks startup: the first Teleport login runs inside the task.
    """
    from db_gateway.maintenance import Maintenance

    maintenance = Maintenance(app[APP_CONFIG_KEY], alert=_teleport_alert(app))
    app[MAINTENANCE_TASK_KEY] = asyncio.create_task(maintenance.run())


async def _stop_maintenance(app: web.Application) -> None:
    task = app.get(MAINTENANCE_TASK_KEY)
    if task is not None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def _close_pg_pools(_app: web.Application) -> None:
    from db_gateway.pg_pool import MANAGER

    await MANAGER.close_all()


async def _stop_tsh_daemons(_app: web.Application) -> None:
    """Kill the long-running tsh processes (AWS proxy, db-gateway's tunnels)."""
    await stop_tsh_daemons()


async def _stop_teleport_session(_app: web.Application) -> None:
    """Cancel the session manager's background tasks (old-generation cleanup, listeners)."""
    from db_gateway.teleport_session import SESSION

    await SESSION.shutdown()


def run_db_gateway_app() -> None:
    cfg = get_config()
    app = create_db_gateway_app(cfg)
    web.run_app(
        app,
        host=cfg.db_gateway.listen_host,
        port=cfg.db_gateway.listen_port,
    )
