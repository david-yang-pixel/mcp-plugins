"""One JSON audit line per MCP ``tools/call``, in the same shape as difyctl-mcp.

Only metadata is recorded: argument key names, plus values for a small allow-list
of non-sensitive scalar keys. Query text, filters, search text, tenant IDs, trace
IDs and response contents are never logged.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
import time
from typing import Any

from .sz_commands import ALLOWED_COMMANDS, HELP_WORDS

audit_logger = logging.getLogger("signoz_mcp.audit")

# Values of these argument keys are low-cardinality labels or windows, never free text
# supplied to SigNoz queries. Everything else is logged by key name only.
LOGGED_VALUE_KEYS = frozenset({
    "deployment", "service", "environment", "signal", "aggregation", "requestType", "timeRange",
})
_KEY_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")
_MAX_KEYS = 32
_MAX_VALUE_CHARS = 128
_HANDLER_MARKER = "_signoz_mcp_audit_stdout"


def configure_audit_logging() -> None:
    """Send audit lines to stdout at INFO, independent of uvicorn's logging config."""
    if any(getattr(handler, _HANDLER_MARKER, False) for handler in audit_logger.handlers):
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    setattr(handler, _HANDLER_MARKER, True)
    audit_logger.addHandler(handler)
    audit_logger.setLevel(logging.INFO)
    audit_logger.propagate = False


def _safe_value(value: Any) -> Any:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        return value[:_MAX_VALUE_CHARS]
    return None


def summarize_arguments(tool: str, arguments: Any) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        return {"keys": [], "values": {}}
    keys = sorted(k for k in arguments if isinstance(k, str) and _KEY_PATTERN.fullmatch(k))[:_MAX_KEYS]
    values = {k: _safe_value(arguments[k]) for k in keys if k in LOGGED_VALUE_KEYS}
    if tool == "sz_run":
        # Only the sz subcommand is logged; the remaining args carry query text.
        args = arguments.get("args")
        if isinstance(args, list) and args and isinstance(args[0], str):
            command = args[0].strip()
            if command in ALLOWED_COMMANDS or command in HELP_WORDS:
                values["command"] = command
    return {"keys": keys, "values": values}


def emit_tool_audit(
    *,
    tool: str,
    known_tools: frozenset[str] | set[str],
    arguments: Any,
    started: float,
    payload: dict[str, Any] | None,
    response_text: str | None,
    exception: BaseException | None = None,
) -> None:
    """Write one audit line. Never raises: auditing must not break a tool call."""
    try:
        tool_name = tool if tool in known_tools else "(unknown)"
        success = exception is None and isinstance(payload, dict) and bool(payload.get("success"))
        error: str | None = None
        if not success:
            code = payload.get("code") if isinstance(payload, dict) else None
            if isinstance(code, str) and code:
                error = code[:_MAX_VALUE_CHARS]
            elif isinstance(exception, asyncio.CancelledError):
                error = "CANCELLED"
            elif exception is not None:
                error = "INTERNAL_ERROR"
        audit_logger.info(json.dumps({
            "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "tool": tool_name,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "outcome": "success" if success else "failed",
            "error": error,
            "response_chars": len(response_text) if isinstance(response_text, str) else 0,
            "args": summarize_arguments(tool_name, arguments),
        }, ensure_ascii=False))
    except Exception:  # noqa: BLE001 - auditing is best effort by design
        pass
