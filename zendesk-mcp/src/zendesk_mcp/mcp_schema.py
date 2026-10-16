from __future__ import annotations

from typing import Any


STATUS_VALUES = ["new", "open", "pending", "hold", "solved"]
PRIORITY_VALUES = ["low", "normal", "high", "urgent"]
TYPE_VALUES = ["question", "incident", "problem", "task"]


def _base_paging_properties() -> dict[str, Any]:
    return {
        "page": {"type": "integer", "minimum": 1, "default": 1},
        "per_page": {"type": "integer", "minimum": 1, "maximum": 100, "default": 25},
    }


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "get_ticket",
        "description": (
            "Get a Zendesk ticket by ID. Read-only. Tool-level default is lightweight; "
            "for SaaS support tickets, include conversation log because Messaging is the source of truth."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticket_id": {"type": "integer", "minimum": 1},
                "include_comments": {"type": "boolean", "default": False},
                "include_conversation_log": {"type": "boolean", "default": False},
                "include_attachment_files": {
                    "type": "boolean",
                    "default": False,
                    "description": "Download ticket attachments and emit them as MCP file/image content blocks.",
                },
                "max_attachment_files": {"type": "integer", "minimum": 1, "maximum": 20, "default": 8},
                "conversation_page_size": {"type": "integer", "minimum": 1, "maximum": 100, "default": 100},
                "conversation_after": {"type": "string"},
                "force_refresh": {"type": "boolean", "default": False},
            },
            "required": ["ticket_id"],
        },
    },
    {
        "name": "list_tickets",
        "description": "List Zendesk tickets, optionally by status or assignee. Read-only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": STATUS_VALUES},
                "assignee_id": {"type": "integer", "minimum": 1},
                "sort_by": {"type": "string", "default": "updated_at"},
                "sort_order": {"type": "string", "enum": ["asc", "desc"], "default": "desc"},
                **_base_paging_properties(),
            },
        },
    },
    {
        "name": "search_tickets",
        "description": "Search Zendesk tickets using Zendesk search syntax. Read-only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1},
                "sort_by": {"type": "string", "default": "updated_at"},
                "sort_order": {"type": "string", "enum": ["asc", "desc"], "default": "desc"},
                **_base_paging_properties(),
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_ticket_comments",
        "description": (
            "Get ticket comments. Read-only. Tool-level default is comments only; "
            "for SaaS support tickets, include Zendesk messaging conversation log."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticket_id": {"type": "integer", "minimum": 1},
                "include_conversation_log": {"type": "boolean", "default": False},
                "conversation_page_size": {"type": "integer", "minimum": 1, "maximum": 100, "default": 100},
                "conversation_after": {"type": "string"},
                "force_refresh": {"type": "boolean", "default": False},
                "page": {"type": "integer", "minimum": 1, "default": 1},
                "per_page": {"type": "integer", "minimum": 1, "maximum": 100, "default": 100},
            },
            "required": ["ticket_id"],
        },
    },
    {
        "name": "get_ticket_conversation_log",
        "description": (
            "Explicit cached read for Zendesk messaging conversation log. "
            "This is the source-of-truth customer thread for SaaS Messaging tickets."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticket_id": {"type": "integer", "minimum": 1},
                "conversation_page_size": {"type": "integer", "minimum": 1, "maximum": 100, "default": 100},
                "conversation_after": {"type": "string"},
                "ticket_updated_at": {"type": "string"},
                "force_refresh": {"type": "boolean", "default": False},
            },
            "required": ["ticket_id"],
        },
    },
    {
        "name": "list_attachments",
        "description": (
            "Extract attachment metadata from ticket comments and Messaging conversation log by default. Read-only. "
            "Pass include_conversation_log=false only for a comments-only fast path."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticket_id": {"type": "integer", "minimum": 1},
                "include_conversation_log": {"type": "boolean", "default": True},
                "conversation_page_size": {"type": "integer", "minimum": 1, "maximum": 100, "default": 100},
                "conversation_after": {"type": "string"},
                "force_refresh": {"type": "boolean", "default": False},
            },
            "required": ["ticket_id"],
        },
    },
    {
        "name": "download_attachment",
        "description": (
            "Download a Zendesk attachment content_url. The MCP result includes standard image/audio "
            "content or an embedded binary resource, plus base64 metadata for backwards compatibility. "
            "Only same-subdomain Zendesk attachment URLs are allowed. Read-only."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "content_url": {"type": "string", "minLength": 1},
                "file_name": {"type": "string"},
                "max_bytes": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 26214400,
                    "default": 10485760,
                },
            },
            "required": ["content_url"],
        },
    },
    {
        "name": "get_user",
        "description": "Get a Zendesk user by ID. Read-only.",
        "inputSchema": {
            "type": "object",
            "properties": {"user_id": {"type": "integer", "minimum": 1}},
            "required": ["user_id"],
        },
    },
    {
        "name": "list_views",
        "description": "List Zendesk views. Read-only.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_view_tickets",
        "description": "List tickets in a Zendesk view. Read-only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "view_id": {"type": "integer", "minimum": 1},
                **_base_paging_properties(),
            },
            "required": ["view_id"],
        },
    },
    {
        "name": "list_groups",
        "description": "List Zendesk groups. Read-only.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "search_help_center",
        "description": "Search Zendesk Help Center articles. Read-only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1},
                "locale": {"type": "string", "default": "zh-cn"},
                **_base_paging_properties(),
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_article",
        "description": "Get a Zendesk Help Center article. Read-only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "article_id": {"type": "integer", "minimum": 1},
                "locale": {"type": "string", "default": "zh-cn"},
            },
            "required": ["article_id"],
        },
    },
    {
        "name": "add_internal_note",
        "description": (
            "Add a private internal note to a Zendesk ticket. Never creates public comments. "
            "Only call when the user explicitly asks to add an internal note."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticket_id": {"type": "integer", "minimum": 1},
                "body": {"type": "string", "minLength": 1},
                "explicit_user_internal_note_request": {"type": "string", "minLength": 1},
                "expected_updated_at": {"type": "string"},
                "dry_run": {"type": "boolean", "default": True},
                "confirm": {"type": "boolean", "default": False},
                "actor": {"type": "string"},
            },
            "required": ["ticket_id", "body", "explicit_user_internal_note_request"],
        },
    },
    {
        "name": "update_ticket_status",
        "description": (
            "Update Zendesk ticket status without adding any comment. "
            "Only call when the user explicitly asks to change ticket status."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticket_id": {"type": "integer", "minimum": 1},
                "status": {"type": "string", "enum": STATUS_VALUES},
                "explicit_user_status_request": {"type": "string", "minLength": 1},
                "expected_updated_at": {"type": "string"},
                "dry_run": {"type": "boolean", "default": True},
                "confirm": {"type": "boolean", "default": False},
                "confirm_solve": {"type": "boolean", "default": False},
                "actor": {"type": "string"},
            },
            "required": ["ticket_id", "status", "explicit_user_status_request"],
        },
    },
    {
        "name": "update_ticket_metadata",
        "description": (
            "Update non-comment Zendesk ticket metadata. Supports priority, type, assignee, "
            "group, incremental tag add/remove, and whitelisted custom fields. Does not support comments."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticket_id": {"type": "integer", "minimum": 1},
                "priority": {"type": "string", "enum": PRIORITY_VALUES},
                "type": {"type": "string", "enum": TYPE_VALUES},
                "assignee_id": {"type": "integer", "minimum": 1},
                "group_id": {"type": "integer", "minimum": 1},
                "add_tags": {"type": "array", "items": {"type": "string"}},
                "remove_tags": {"type": "array", "items": {"type": "string"}},
                "custom_fields": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "value": {},
                        },
                        "required": ["id", "value"],
                    },
                },
                "explicit_user_metadata_request": {"type": "string", "minLength": 1},
                "expected_updated_at": {"type": "string"},
                "dry_run": {"type": "boolean", "default": True},
                "confirm": {"type": "boolean", "default": False},
                "actor": {"type": "string"},
            },
            "required": ["ticket_id", "explicit_user_metadata_request"],
        },
    },
]
