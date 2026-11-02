"""Shared MCP tool schema for DB Gateway."""
from __future__ import annotations

from typing import Any


TOOL_SCHEMA: dict[str, Any] = {
    "name": "db_gateway_query",
    "description": (
        "Run a read-only SELECT/WITH query through the DB Gateway. "
        "Databases: prod, billing, plugin. The gateway enforces read-only SQL "
        "(single statement; may start with '(' e.g. '(SELECT 1) UNION ALL (SELECT 2)'). "
        "Results are paginated: at most `limit` rows (<= server max_rows, default 1000) "
        "starting at `offset`. The response includes row_count, total_rows (null if the "
        "bounded count timed out), has_more, next_offset (pass it as `offset` for the next "
        "page), warnings and timing_ms. Add ORDER BY (ideally on a unique key) when paging, "
        "otherwise page order is not stable. format=csv (default) returns `csv` text where "
        "NULL and empty string both appear as an empty field; format=json returns `columns`, "
        "`column_types` and `rows` as an array of arrays of strings, with SQL NULL as null."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "database": {
                "type": "string",
                "enum": ["prod", "billing", "plugin"],
                "description": "prod=langgenius, billing=billing, plugin=dify_plugin_daemon",
            },
            "sql": {
                "type": "string",
                "description": (
                    "Single read-only SELECT/WITH query (optionally wrapped in parentheses). "
                    "Do not add LIMIT/OFFSET for paging; use the offset/limit arguments."
                ),
            },
            "ticket_id": {
                "type": "string",
                "description": (
                    "Zendesk ticket ID for audit logs. Strongly recommended: a missing value "
                    "is recorded in the audit log and returns a warning; it is rejected when "
                    "the gateway enables require_ticket_id."
                ),
            },
            "offset": {
                "type": "integer",
                "minimum": 0,
                "default": 0,
                "description": "Rows to skip before this page (use next_offset from the previous page).",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "description": "Page size; defaults to and is capped at the server max_rows (default 1000).",
            },
            "format": {
                "type": "string",
                "enum": ["csv", "json"],
                "default": "csv",
                "description": (
                    "csv: `csv` text (header + rows). json: `columns`, `column_types`, and "
                    "`rows` as array of arrays (string values, SQL NULL as null)."
                ),
            },
        },
        "required": ["database", "sql"],
    },
}
