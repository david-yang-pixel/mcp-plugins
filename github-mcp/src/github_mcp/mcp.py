from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any, Awaitable, Callable, Optional

from fastapi import Header, HTTPException

PROTOCOL_VERSION = "2025-03-26"
ToolHandler = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]

audit = logging.getLogger("github_mcp.audit")

# Argument values that are safe to log. Everything else (search queries, code paths,
# field lists, free text) is logged by key name only.
AUDIT_VALUE_KEYS = frozenset(
    {
        "repo",
        "owner",
        "scope",
        "pr_number",
        "issue_number",
        "tag",
        "base",
        "head",
        "page",
        "per_page",
        "limit",
        "visibility",
        "raw",
        "include_patch",
        "include_comments",
    }
)
_AUDIT_MAX_KEYS = 32
_AUDIT_MAX_KEY_CHARS = 64
_AUDIT_MAX_VALUE_CHARS = 200


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


# Every tool only reads external systems. Clients such as OpenConnector derive
# read/write from these hints; without them unfamiliar tool names are treated as writes.
READ_ONLY_ANNOTATIONS: dict[str, Any] = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": True,
}


def with_read_only_annotations(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {**tool, "annotations": {**READ_ONLY_ANNOTATIONS, **(tool.get("annotations") or {})}}
        for tool in tools
    ]


def tools_list_result(request_id: Any, tools: list[dict[str, Any]]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": with_read_only_annotations(tools)}}


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


def optional_bool(arguments: dict[str, Any], name: str, default: bool) -> bool:
    value = arguments.get(name, default)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "1", "yes"}:
        return True
    if isinstance(value, str) and value.strip().lower() in {"false", "0", "no"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def _audit_args(arguments: Any) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        return {"keys": [], "values": {}}
    keys = sorted(str(key)[:_AUDIT_MAX_KEY_CHARS] for key in arguments)[:_AUDIT_MAX_KEYS]
    values: dict[str, Any] = {}
    for key in keys:
        if key not in AUDIT_VALUE_KEYS:
            continue
        value = arguments.get(key)
        if isinstance(value, str):
            values[key] = value[:_AUDIT_MAX_VALUE_CHARS]
        elif isinstance(value, (bool, int, float)) or value is None:
            values[key] = value
    return {"keys": keys, "values": values}


def _audit_outcome(response: Any) -> tuple[str, Optional[str], int]:
    """Return (outcome, error code, response_chars) for a tools/call JSON-RPC response."""
    if not isinstance(response, dict):
        return "failed", "INTERNAL_ERROR", 0
    if "error" in response:
        err = response.get("error") or {}
        code = err.get("code")
        error = "UNKNOWN_TOOL" if code == -32601 else f"JSONRPC_{code}"
        return "failed", error, len(json.dumps(response, ensure_ascii=False))
    result = response.get("result") or {}
    content = result.get("content") or []
    text = content[0].get("text", "") if content and isinstance(content[0], dict) else ""
    payload = result.get("structuredContent") or {}
    if payload.get("success"):
        return "success", None, len(text)
    code = payload.get("code")
    return "failed", str(code) if code else "UNKNOWN_ERROR", len(text)


def audit_tool_call(*, tool: Any, arguments: Any, started: float, response: Any) -> None:
    """Emit one JSON line per tools/call. Never raises: logging must not break a call."""
    try:
        outcome, error, response_chars = _audit_outcome(response)
        audit.info(
            json.dumps(
                {
                    "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "tool": str(tool)[:100] if tool is not None else None,
                    "duration_ms": int((time.monotonic() - started) * 1000),
                    "outcome": outcome,
                    "error": error,
                    "response_chars": response_chars,
                    "args": _audit_args(arguments),
                },
                ensure_ascii=False,
            )
        )
    except Exception:  # noqa: BLE001
        pass


class _StdoutHandler(logging.StreamHandler):  # type: ignore[type-arg]
    """Writes to whatever sys.stdout is at emit time (survives stream swaps)."""

    def emit(self, record: logging.LogRecord) -> None:
        self.stream = sys.stdout
        super().emit(record)


def configure_logging() -> None:
    """Send github_mcp.audit INFO lines to stdout as bare JSON (idempotent).

    Only the audit logger gets a handler. The root logger stays at WARNING so
    third-party INFO logs (httpx logs every request URL, including search
    queries and paths) are never emitted.
    """
    if any(isinstance(h, _StdoutHandler) for h in audit.handlers):
        return
    handler = _StdoutHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    audit.addHandler(handler)
    audit.setLevel(logging.INFO)
    audit.propagate = False
