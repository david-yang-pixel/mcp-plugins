"""aiohttp app context helpers."""

from __future__ import annotations

from typing import Any

from aiohttp import web


CONFIG_KEY: web.AppKey[Any] = web.AppKey("db_gateway.config", object)
TELEPORT_AUTH_TOKEN_STORE_KEY: web.AppKey[Any] = web.AppKey(
    "db_gateway.teleport_auth_token_store",
    object,
)


def app_config(app: web.Application) -> Any:
    return app[CONFIG_KEY]


def teleport_auth_token_store(app: web.Application) -> Any:
    return app[TELEPORT_AUTH_TOKEN_STORE_KEY]

