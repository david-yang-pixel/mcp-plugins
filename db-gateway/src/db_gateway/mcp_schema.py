"""Shared MCP tool schema for DB Gateway."""
from __future__ import annotations

from typing import Any


TOOL_SCHEMA: dict[str, Any] = {
    "name": "db_gateway_query",
    "description": (
        "Run a read-only SELECT query through the DB Gateway. "
        "Databases: prod, billing, plugin. The gateway enforces read-only SQL."
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
                "description": "Single read-only SELECT/WITH query.",
            },
            "ticket_id": {
                "type": "string",
                "description": "Optional Zendesk ticket ID for audit logs.",
            },
        },
        "required": ["database", "sql"],
    },
}
