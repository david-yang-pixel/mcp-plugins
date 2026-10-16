from __future__ import annotations

import asyncio
import json
import time

import httpx
from fastapi.testclient import TestClient

from zendesk_mcp.client import ZendeskApiError


def _app(monkeypatch, tmp_path):
    monkeypatch.setenv("ZENDESK_MCP_API_KEY", "z" * 32)
    monkeypatch.setenv("ZENDESK_SUBDOMAIN", "example")
    monkeypatch.setenv("ZENDESK_EMAIL", "agent@example.com")
    monkeypatch.setenv("ZENDESK_API_TOKEN", "token")
    monkeypatch.setenv("ZENDESK_MCP_AUDIT_LOG", str(tmp_path / "audit.jsonl"))

    from zendesk_mcp.settings import get_settings

    get_settings.cache_clear()
    from zendesk_mcp.tools import _CACHE, _CACHE_LOAD_LOCKS

    _CACHE.clear()
    _CACHE_LOAD_LOCKS.clear()
    from zendesk_mcp.app import create_app

    return create_app()


def _client(monkeypatch, tmp_path):
    return TestClient(_app(monkeypatch, tmp_path))


def _mcp(client: TestClient, payload: dict):
    return client.post(
        "/mcp",
        headers={"Authorization": "Bearer " + "z" * 32},
        json=payload,
    )


def _tool_call(client: TestClient, name: str, arguments: dict, request_id="call-1"):
    return _mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )


def test_mcp_rejects_missing_bearer_token(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)

    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )

    assert response.status_code == 401


def test_tools_list_exposes_internal_note_but_excludes_customer_visible_reply_tools(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)

    response = _mcp(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})

    assert response.status_code == 200
    tool_names = {tool["name"] for tool in response.json()["result"]["tools"]}
    assert "update_ticket_status" in tool_names
    assert "update_ticket_metadata" in tool_names
    assert "add_internal_note" in tool_names
    assert "download_attachment" in tool_names
    assert "public_reply" not in tool_names
    assert "add_public_comment" not in tool_names
    assert "add_comment" not in tool_names


def test_slow_tool_call_does_not_block_ping(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)

    def slow_handle_tool_call(name, arguments, *, settings=None):
        time.sleep(0.35)
        return {"success": True, "data": {"tool": name}}

    monkeypatch.setattr("zendesk_mcp.app.handle_tool_call", slow_handle_tool_call)

    async def run_requests():
        transport = httpx.ASGITransport(app=app)
        headers = {"Authorization": "Bearer " + "z" * 32}
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            started = time.perf_counter()
            slow_task = asyncio.create_task(
                client.post(
                    "/mcp",
                    headers=headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": "slow",
                        "method": "tools/call",
                        "params": {"name": "list_views", "arguments": {}},
                    },
                )
            )
            await asyncio.sleep(0.05)
            ping_response = await client.post(
                "/mcp",
                headers=headers,
                json={"jsonrpc": "2.0", "id": "ping", "method": "ping"},
            )
            ping_elapsed = time.perf_counter() - started
            slow_response = await slow_task
        return ping_response, slow_response, ping_elapsed

    ping_response, slow_response, ping_elapsed = asyncio.run(run_requests())

    assert ping_response.status_code == 200
    assert slow_response.status_code == 200
    assert ping_elapsed < 0.25


def test_list_views_uses_short_lived_cache(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    calls: list[int] = []

    class FakeZendesk:
        def list_views(self):
            calls.append(1)
            return {"views": [{"id": 1, "title": "Open tickets"}]}

    monkeypatch.setattr("zendesk_mcp.tools.ZendeskClient", lambda settings=None: FakeZendesk())

    first = _tool_call(client, "list_views", {}, request_id="views-1")
    second = _tool_call(client, "list_views", {}, request_id="views-2")

    assert first.json()["result"]["isError"] is False
    assert second.json()["result"]["isError"] is False
    assert calls == [1]


def test_get_ticket_with_conversation_log_uses_cache(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    calls: list[int] = []

    class FakeZendesk:
        def get_ticket(self, ticket_id: int):
            return {"ticket": {"id": ticket_id, "updated_at": "u1"}}

        def get_ticket_conversation_log(self, ticket_id: int, *, page_size: int = 100, after=None):
            calls.append(ticket_id)
            return {"events": [{"id": "evt-1"}], "meta": {"has_more": False}}

    monkeypatch.setattr("zendesk_mcp.tools.ZendeskClient", lambda settings=None: FakeZendesk())

    first = _tool_call(
        client,
        "get_ticket",
        {"ticket_id": 123, "include_conversation_log": True},
        request_id="ticket-1",
    )
    second = _tool_call(
        client,
        "get_ticket",
        {"ticket_id": 123, "include_conversation_log": True},
        request_id="ticket-2",
    )

    assert first.json()["result"]["isError"] is False
    assert second.json()["result"]["isError"] is False
    assert calls == [123]


def test_get_ticket_conversation_log_cache_invalidates_when_ticket_updated_at_changes(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    updated_at = {"value": "u1"}
    calls: list[int] = []

    class FakeZendesk:
        def get_ticket(self, ticket_id: int):
            return {"ticket": {"id": ticket_id, "updated_at": updated_at["value"]}}

        def get_ticket_conversation_log(self, ticket_id: int, *, page_size: int = 100, after=None):
            calls.append(ticket_id)
            return {
                "events": [{"id": f"evt-{len(calls)}"}],
                "meta": {"has_more": False},
            }

    monkeypatch.setattr("zendesk_mcp.tools.ZendeskClient", lambda settings=None: FakeZendesk())

    first = _tool_call(client, "get_ticket_conversation_log", {"ticket_id": 123}, request_id="log-1")
    second = _tool_call(client, "get_ticket_conversation_log", {"ticket_id": 123}, request_id="log-2")
    updated_at["value"] = "u2"
    third = _tool_call(client, "get_ticket_conversation_log", {"ticket_id": 123}, request_id="log-3")

    assert first.json()["result"]["isError"] is False
    assert second.json()["result"]["isError"] is False
    assert third.json()["result"]["isError"] is False
    assert calls == [123, 123]
    assert first.json()["result"]["structuredContent"]["data"]["events"] == [{"id": "evt-1"}]
    assert second.json()["result"]["structuredContent"]["data"]["events"] == [{"id": "evt-1"}]
    assert third.json()["result"]["structuredContent"]["data"]["events"] == [{"id": "evt-2"}]


def test_get_ticket_conversation_log_force_refresh_bypasses_cache(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    calls: list[int] = []

    class FakeZendesk:
        def get_ticket(self, ticket_id: int):
            return {"ticket": {"id": ticket_id, "updated_at": "u1"}}

        def get_ticket_conversation_log(self, ticket_id: int, *, page_size: int = 100, after=None):
            calls.append(ticket_id)
            return {
                "events": [{"id": f"evt-{len(calls)}"}],
                "meta": {"has_more": False},
            }

    monkeypatch.setattr("zendesk_mcp.tools.ZendeskClient", lambda settings=None: FakeZendesk())

    first = _tool_call(client, "get_ticket_conversation_log", {"ticket_id": 123}, request_id="log-1")
    second = _tool_call(client, "get_ticket_conversation_log", {"ticket_id": 123}, request_id="log-2")
    third = _tool_call(
        client,
        "get_ticket_conversation_log",
        {"ticket_id": 123, "force_refresh": True},
        request_id="log-3",
    )

    assert first.json()["result"]["isError"] is False
    assert second.json()["result"]["isError"] is False
    assert third.json()["result"]["isError"] is False
    assert calls == [123, 123]
    assert first.json()["result"]["structuredContent"]["data"]["events"] == [{"id": "evt-1"}]
    assert second.json()["result"]["structuredContent"]["data"]["events"] == [{"id": "evt-1"}]
    assert third.json()["result"]["structuredContent"]["data"]["events"] == [{"id": "evt-2"}]


def test_get_ticket_can_include_comments_and_conversation_log(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)

    class FakeZendesk:
        def get_ticket(self, ticket_id: int):
            return {"ticket": {"id": ticket_id, "updated_at": "u1"}}

        def get_ticket_comments(self, ticket_id: int, page: int = 1, per_page: int = 100):
            return {"comments": [{"id": 1, "body": "comment"}]}

        def get_ticket_conversation_log(self, ticket_id: int, *, page_size: int = 100, after=None):
            return {"events": [{"id": "evt-1"}], "meta": {"has_more": False}}

    monkeypatch.setattr("zendesk_mcp.tools.ZendeskClient", lambda settings=None: FakeZendesk())

    response = _tool_call(
        client,
        "get_ticket",
        {"ticket_id": 123, "include_comments": True, "include_conversation_log": True},
    )

    body = response.json()
    assert body["result"]["isError"] is False
    data = body["result"]["structuredContent"]["data"]
    assert data["ticket"]["id"] == 123
    assert data["comments"] == [{"id": 1, "body": "comment"}]
    assert data["conversation_log"]["events"] == [{"id": "evt-1"}]


def test_read_only_tool_retries_transient_connection_error(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    calls = 0

    class FakeZendesk:
        def get_ticket(self, ticket_id: int):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ZendeskApiError(0, "Zendesk connection failed", "tickets/123")
            return {"ticket": {"id": ticket_id, "updated_at": "u1"}}

    monkeypatch.setattr("zendesk_mcp.tools.ZendeskClient", lambda settings=None: FakeZendesk())
    monkeypatch.setattr("zendesk_mcp.tools.time.sleep", lambda seconds: None)

    response = _tool_call(client, "get_ticket", {"ticket_id": 123})

    body = response.json()
    assert body["result"]["isError"] is False
    assert body["result"]["structuredContent"]["data"]["ticket"]["id"] == 123
    assert calls == 2


def test_get_ticket_conversation_log_is_explicit_heavy_tool(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    calls: list[tuple[int, int, str | None]] = []

    class FakeZendesk:
        def get_ticket(self, ticket_id: int):
            return {"ticket": {"id": ticket_id, "updated_at": "u1"}}

        def get_ticket_conversation_log(self, ticket_id: int, *, page_size: int = 100, after=None):
            calls.append((ticket_id, page_size, after))
            return {"events": [{"id": "evt-1"}], "meta": {"has_more": False}}

    monkeypatch.setattr("zendesk_mcp.tools.ZendeskClient", lambda settings=None: FakeZendesk())

    response = _tool_call(
        client,
        "get_ticket_conversation_log",
        {"ticket_id": 123, "conversation_page_size": 50, "conversation_after": "cursor-1"},
    )

    body = response.json()
    assert body["result"]["isError"] is False
    assert body["result"]["structuredContent"]["data"]["events"] == [{"id": "evt-1"}]
    assert calls == [(123, 50, "cursor-1")]


def test_list_attachments_includes_conversation_log_by_default(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)

    class FakeZendesk:
        def get_ticket(self, ticket_id: int):
            return {"ticket": {"id": ticket_id, "updated_at": "u1"}}

        def get_ticket_comments(self, ticket_id: int, page: int = 1, per_page: int = 100):
            return {
                "comments": [
                    {
                        "id": 1,
                        "attachments": [
                            {"id": 10, "file_name": "comment.txt", "content_url": "https://example.com/a"}
                        ],
                    }
                ]
            }

        def get_ticket_conversation_log(self, ticket_id: int, *, page_size: int = 100, after=None):
            return {
                "events": [
                    {
                        "id": "evt-1",
                        "attachments": [
                            {"id": "att-1", "name": "conversation.png", "url": "https://example.com/c"}
                        ],
                    }
                ]
            }

    monkeypatch.setattr("zendesk_mcp.tools.ZendeskClient", lambda settings=None: FakeZendesk())

    response = _tool_call(client, "list_attachments", {"ticket_id": 123})

    body = response.json()
    assert body["result"]["isError"] is False
    data = body["result"]["structuredContent"]["data"]
    assert data["conversation_log_included"] is True
    assert {attachment["source"] for attachment in data["attachments"]} == {
        "comment",
        "conversation_log",
    }


def test_list_attachments_includes_conversation_log_image_content(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)

    class FakeZendesk:
        def get_ticket(self, ticket_id: int):
            return {"ticket": {"id": ticket_id, "updated_at": "u1"}}

        def get_ticket_comments(self, ticket_id: int, page: int = 1, per_page: int = 100):
            return {"comments": []}

        def get_ticket_conversation_log(self, ticket_id: int, *, page_size: int = 100, after=None):
            return {
                "events": [
                    {
                        "id": "evt-img",
                        "created_at": "2026-07-01T06:17:53Z",
                        "author": {"zen:support:user_id": 456, "display_name": "customer"},
                        "content": {
                            "type": "image",
                            "media_url": "https://example.zendesk.com/sc/attachments/v2/01ABC/screenshot.png",
                            "media_type": "image/png",
                            "media_size": 150687,
                            "alt_text": "screenshot.png",
                            "attachment_id": "01ABC",
                        },
                    }
                ]
            }

    monkeypatch.setattr("zendesk_mcp.tools.ZendeskClient", lambda settings=None: FakeZendesk())

    response = _tool_call(client, "list_attachments", {"ticket_id": 123})

    body = response.json()
    assert body["result"]["isError"] is False
    data = body["result"]["structuredContent"]["data"]
    assert data["attachments"] == [
        {
            "source": "conversation_log",
            "event_id": "evt-img",
            "author_id": 456,
            "created_at": "2026-07-01T06:17:53Z",
            "id": "01ABC",
            "file_name": "screenshot.png",
            "content_url": "https://example.zendesk.com/sc/attachments/v2/01ABC/screenshot.png",
            "content_type": "image/png",
            "size": 150687,
        }
    ]


def test_list_attachments_can_skip_conversation_log_explicitly(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)

    class FakeZendesk:
        def get_ticket_comments(self, ticket_id: int, page: int = 1, per_page: int = 100):
            return {"comments": []}

        def get_ticket_conversation_log(self, ticket_id: int, *, page_size: int = 100, after=None):
            raise AssertionError("conversation log should be skipped when explicitly disabled")

    monkeypatch.setattr("zendesk_mcp.tools.ZendeskClient", lambda settings=None: FakeZendesk())

    response = _tool_call(
        client,
        "list_attachments",
        {"ticket_id": 123, "include_conversation_log": False},
    )

    body = response.json()
    assert body["result"]["isError"] is False
    data = body["result"]["structuredContent"]["data"]
    assert data["conversation_log_included"] is False
    assert data["attachments"] == []


def test_download_attachment_returns_base64_payload(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    calls: list[tuple[str, int]] = []

    class FakeZendesk:
        def download_attachment(self, content_url: str, *, max_bytes: int):
            calls.append((content_url, max_bytes))
            return {
                "content": b"PNGDATA",
                "content_type": "image/png",
                "size": 7,
            }

    monkeypatch.setattr("zendesk_mcp.tools.ZendeskClient", lambda settings=None: FakeZendesk())

    response = _tool_call(
        client,
        "download_attachment",
        {
            "content_url": "https://example.zendesk.com/attachments/token/abc/?name=screenshot.png",
            "file_name": "screenshot.png",
            "max_bytes": 1024,
        },
    )

    body = response.json()
    assert body["result"]["isError"] is False
    data = body["result"]["structuredContent"]["data"]
    assert data == {
        "content_url": "https://example.zendesk.com/attachments/token/abc/?name=screenshot.png",
        "file_name": "screenshot.png",
        "content_type": "image/png",
        "size": 7,
        "encoding": "base64",
        "data_base64": "UE5HREFUQQ==",
    }
    assert calls == [
        ("https://example.zendesk.com/attachments/token/abc/?name=screenshot.png", 1024)
    ]


def test_download_attachment_rejects_non_zendesk_url(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)

    response = _tool_call(
        client,
        "download_attachment",
        {"content_url": "https://evil.example.com/attachments/token/abc"},
    )

    body = response.json()
    assert body["result"]["isError"] is True
    assert body["result"]["structuredContent"]["code"] == "BAD_ATTACHMENT_URL"


def test_status_update_requires_explicit_user_request(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)

    response = _tool_call(
        client,
        "update_ticket_status",
        {"ticket_id": 123, "status": "pending", "dry_run": False, "confirm": True},
    )

    body = response.json()
    assert body["result"]["isError"] is True
    assert body["result"]["structuredContent"]["code"] == "EXPLICIT_STATUS_REQUEST_REQUIRED"


def test_status_update_dry_run_does_not_call_zendesk(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    updates: list[tuple[int, dict]] = []

    class FakeZendesk:
        def get_ticket(self, ticket_id: int):
            return {
                "ticket": {
                    "id": ticket_id,
                    "status": "open",
                    "updated_at": "2026-06-26T08:00:00Z",
                    "tags": ["saas"],
                }
            }

        def update_ticket(self, ticket_id: int, fields: dict):
            updates.append((ticket_id, fields))
            return {"ticket": {"id": ticket_id, **fields}}

    monkeypatch.setattr("zendesk_mcp.tools.ZendeskClient", lambda settings=None: FakeZendesk())

    response = _tool_call(
        client,
        "update_ticket_status",
        {
            "ticket_id": 123,
            "status": "pending",
            "explicit_user_status_request": "把 123 改成 pending",
            "dry_run": True,
        },
    )

    body = response.json()
    assert body["result"]["isError"] is False
    data = body["result"]["structuredContent"]
    assert data["dry_run"] is True
    assert data["diff"] == {"status": {"before": "open", "after": "pending"}}
    assert updates == []


def test_solved_status_requires_extra_confirmation(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)

    class FakeZendesk:
        def get_ticket(self, ticket_id: int):
            return {"ticket": {"id": ticket_id, "status": "pending", "updated_at": "u1"}}

        def update_ticket(self, ticket_id: int, fields: dict):
            raise AssertionError("should not update without confirm_solve")

    monkeypatch.setattr("zendesk_mcp.tools.ZendeskClient", lambda settings=None: FakeZendesk())

    response = _tool_call(
        client,
        "update_ticket_status",
        {
            "ticket_id": 123,
            "status": "solved",
            "explicit_user_status_request": "把 123 改成 solved",
            "dry_run": False,
            "confirm": True,
        },
    )

    body = response.json()
    assert body["result"]["isError"] is True
    assert body["result"]["structuredContent"]["code"] == "CONFIRM_SOLVE_REQUIRED"


def test_confirmed_status_update_writes_audit_log(monkeypatch, tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    client = _client(monkeypatch, tmp_path)
    updates: list[tuple[int, dict]] = []

    class FakeZendesk:
        def get_ticket(self, ticket_id: int):
            return {"ticket": {"id": ticket_id, "status": "open", "updated_at": "u1"}}

        def update_ticket(self, ticket_id: int, fields: dict):
            updates.append((ticket_id, fields))
            return {"ticket": {"id": ticket_id, "status": fields["status"], "updated_at": "u2"}}

    monkeypatch.setattr("zendesk_mcp.tools.ZendeskClient", lambda settings=None: FakeZendesk())

    response = _tool_call(
        client,
        "update_ticket_status",
        {
            "ticket_id": 123,
            "status": "pending",
            "explicit_user_status_request": "把 123 改成 pending",
            "dry_run": False,
            "confirm": True,
            "actor": "codex-local",
        },
    )

    body = response.json()
    assert body["result"]["isError"] is False
    assert updates == [(123, {"status": "pending"})]
    audit_line = json.loads(audit_path.read_text().strip())
    assert audit_line["action"] == "update_ticket_status"
    assert audit_line["ticket_id"] == 123
    assert audit_line["actor"] == "codex-local"
    assert audit_line["diff"]["status"] == {"before": "open", "after": "pending"}


def test_internal_note_requires_explicit_user_request(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)

    response = _tool_call(
        client,
        "add_internal_note",
        {"ticket_id": 123, "body": "Investigated from logs.", "dry_run": False, "confirm": True},
    )

    body = response.json()
    assert body["result"]["isError"] is True
    assert body["result"]["structuredContent"]["code"] == "EXPLICIT_INTERNAL_NOTE_REQUEST_REQUIRED"


def test_internal_note_rejects_public_comment_flag(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)

    response = _tool_call(
        client,
        "add_internal_note",
        {
            "ticket_id": 123,
            "body": "Investigated from logs.",
            "public": True,
            "explicit_user_internal_note_request": "给 123 加内部备注",
            "dry_run": False,
            "confirm": True,
        },
    )

    body = response.json()
    assert body["result"]["isError"] is True
    assert body["result"]["structuredContent"]["code"] == "PUBLIC_COMMENT_FORBIDDEN"


def test_internal_note_dry_run_does_not_call_zendesk(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    notes: list[tuple[int, str]] = []

    class FakeZendesk:
        def get_ticket(self, ticket_id: int):
            return {"ticket": {"id": ticket_id, "status": "open", "updated_at": "u1"}}

        def add_internal_note(self, ticket_id: int, body: str):
            notes.append((ticket_id, body))
            return {"ticket": {"id": ticket_id, "updated_at": "u2"}}

    monkeypatch.setattr("zendesk_mcp.tools.ZendeskClient", lambda settings=None: FakeZendesk())

    response = _tool_call(
        client,
        "add_internal_note",
        {
            "ticket_id": 123,
            "body": "Investigated from logs.",
            "explicit_user_internal_note_request": "给 123 加内部备注",
            "dry_run": True,
        },
    )

    body = response.json()
    assert body["result"]["isError"] is False
    data = body["result"]["structuredContent"]
    assert data["dry_run"] is True
    assert data["body_preview"] == "Investigated from logs."
    assert notes == []


def test_confirmed_internal_note_writes_private_comment_and_audit_log(monkeypatch, tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    client = _client(monkeypatch, tmp_path)
    notes: list[tuple[int, str]] = []

    class FakeZendesk:
        def get_ticket(self, ticket_id: int):
            return {"ticket": {"id": ticket_id, "status": "open", "updated_at": "u1"}}

        def add_internal_note(self, ticket_id: int, body: str):
            notes.append((ticket_id, body))
            return {"ticket": {"id": ticket_id, "status": "open", "updated_at": "u2"}}

    monkeypatch.setattr("zendesk_mcp.tools.ZendeskClient", lambda settings=None: FakeZendesk())

    response = _tool_call(
        client,
        "add_internal_note",
        {
            "ticket_id": 123,
            "body": "Investigated from logs.",
            "explicit_user_internal_note_request": "给 123 加内部备注",
            "dry_run": False,
            "confirm": True,
            "actor": "codex-local",
        },
    )

    body = response.json()
    assert body["result"]["isError"] is False
    assert notes == [(123, "Investigated from logs.")]
    audit_line = json.loads(audit_path.read_text().strip())
    assert audit_line["action"] == "add_internal_note"
    assert audit_line["ticket_id"] == 123
    assert audit_line["actor"] == "codex-local"
    assert audit_line["body_preview"] == "Investigated from logs."


def test_metadata_update_rejects_comment_like_payload(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)

    response = _tool_call(
        client,
        "update_ticket_metadata",
        {
            "ticket_id": 123,
            "priority": "high",
            "comment": {"body": "do not expose comments"},
            "explicit_user_metadata_request": "把 123 优先级改成 high",
            "dry_run": False,
            "confirm": True,
        },
    )

    body = response.json()
    assert body["result"]["isError"] is True
    assert body["result"]["structuredContent"]["code"] == "COMMENT_FIELDS_FORBIDDEN"


def test_metadata_update_preserves_other_tags_and_uses_optimistic_lock(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    updates: list[tuple[int, dict]] = []

    class FakeZendesk:
        def get_ticket(self, ticket_id: int):
            return {
                "ticket": {
                    "id": ticket_id,
                    "priority": "normal",
                    "type": "question",
                    "assignee_id": None,
                    "group_id": 1,
                    "tags": ["saas", "keep_existing"],
                    "updated_at": "u1",
                }
            }

        def update_ticket(self, ticket_id: int, fields: dict):
            updates.append((ticket_id, fields))
            return {"ticket": {"id": ticket_id, **fields, "updated_at": "u2"}}

    monkeypatch.setattr("zendesk_mcp.tools.ZendeskClient", lambda settings=None: FakeZendesk())

    response = _tool_call(
        client,
        "update_ticket_metadata",
        {
            "ticket_id": 123,
            "priority": "high",
            "add_tags": ["needs_logs"],
            "remove_tags": ["saas"],
            "explicit_user_metadata_request": "给 123 加 needs_logs，优先级 high",
            "dry_run": False,
            "confirm": True,
        },
    )

    body = response.json()
    assert body["result"]["isError"] is False
    assert updates == [
        (
            123,
            {
                "priority": "high",
                "tags": ["keep_existing", "needs_logs"],
                "safe_update": True,
                "updated_stamp": "u1",
            },
        )
    ]
