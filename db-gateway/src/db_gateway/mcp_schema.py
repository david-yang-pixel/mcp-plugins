"""Shared MCP tool schema for DB Gateway."""
from __future__ import annotations

from typing import Any


TOOL_SCHEMA: dict[str, Any] = {
    "name": "db_gateway_query",
    # Read-only SQL only. Clients such as OpenConnector derive read/write from these hints.
    "annotations": {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
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
        "`column_types` and `rows` as an array of arrays of strings, with SQL NULL as null. "
        "explain=true returns the query plan (EXPLAIN without ANALYZE: the query is planned, "
        "not executed) as rows of the `QUERY PLAN` column; use it to check index usage before "
        "running a slow query. A query that times out returns the indexes of the tables it "
        "reads. Use db_gateway_schema to look up real column names and types."
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
            "explain": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Return the query plan instead of rows. The gateway prepends EXPLAIN "
                    "itself (no ANALYZE, the query is never executed); do not write EXPLAIN "
                    "in `sql`."
                ),
            },
        },
        "required": ["database", "sql"],
    },
}


SCHEMA_TOOL_SCHEMA: dict[str, Any] = {
    "name": "db_gateway_schema",
    "annotations": {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
    "description": (
        "Look up the real columns (name, type, nullable, default) and indexes of one or more "
        "tables through the DB Gateway, from information_schema.columns and pg_indexes. "
        "Databases: prod, billing, plugin. Use it before writing SQL to avoid 'column does "
        "not exist' errors and type mismatches (e.g. uuid = text), and to pick filters that "
        "hit an index. Read-only."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "database": {
                "type": "string",
                "enum": ["prod", "billing", "plugin"],
                "description": "prod=langgenius, billing=billing, plugin=dify_plugin_daemon",
            },
            "tables": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 20,
                "description": (
                    "Table names, e.g. [\"workflow_runs\"]; `schema.table` is accepted. "
                    "Unqualified names match every non-system schema."
                ),
            },
        },
        "required": ["database", "tables"],
    },
}

TOOLS: list[dict[str, Any]] = [TOOL_SCHEMA, SCHEMA_TOOL_SCHEMA]
