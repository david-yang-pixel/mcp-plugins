from __future__ import annotations

import json
from typing import Any, Awaitable, Callable, Optional

from fastapi import Header, HTTPException

PROTOCOL_VERSION = "2025-03-26"
ToolHandler = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


def check_auth(
    *,
    authorization: Optional[str],
    api_key: str,
    service_name: str,
) -> None:
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail=f"{service_name} API key is not configured",
        )
    if authorization != f"Bearer {api_key}":
        raise HTTPException(status_code=401, detail="Unauthorized")


def auth_header(authorization: Optional[str] = Header(default=None)) -> Optional[str]:
    return authorization


def tool_result(request_id: Any, payload: dict[str, Any]) -> dict[str, Any]:
    ok = bool(payload.get("success"))
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "content": [
                {"type": "text", "text": json.dumps(payload, ensure_ascii=False)},
            ],
            "structuredContent": payload,
            "isError": not ok,
        },
    }


def jsonrpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def initialize_result(request_id: Any, *, service_name: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": service_name, "version": "0.1.0"},
        },
    }


def tools_list_result(request_id: Any, tools: list[dict[str, Any]]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": tools}}


def success(data: Any = None, *, output: str = "") -> dict[str, Any]:
    payload: dict[str, Any] = {"success": True}
    if data is not None:
        payload["data"] = data
    if output:
        payload["output"] = output
    return payload


def failure(code: str, error: str) -> dict[str, Any]:
    return {"success": False, "code": code, "error": error}


def required_str(arguments: dict[str, Any], name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def optional_str(arguments: dict[str, Any], name: str, default: str = "") -> str:
    value = arguments.get(name, default)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value.strip()


def optional_int(
    arguments: dict[str, Any],
    name: str,
    default: int,
    *,
    minimum: int = 1,
    maximum: int = 100,
) -> int:
    try:
        value = int(arguments.get(name, default))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    return max(minimum, min(value, maximum))


def required_int(
    arguments: dict[str, Any],
    name: str,
    *,
    minimum: int = 1,
    maximum: int = 2_147_483_647,
) -> int:
    if name not in arguments:
        raise ValueError(f"{name} is required")
    return optional_int(arguments, name, minimum, minimum=minimum, maximum=maximum)
