from fastapi.testclient import TestClient

from zendesk_browser_relay.customer_reply import CUSTOMER_REPLY_FOOTER
from zendesk_browser_relay.schemas import FinishTicketRunRequest


class FakeRelay:
    def __init__(self, *, result_status="sent", raise_error=None):
        self.requests = []
        self.result_status = result_status
        self.raise_error = raise_error

    async def send_message(self, request):
        from zendesk_browser_relay.schemas import SendResponse

        self.requests.append(request)
        if self.raise_error is not None:
            raise self.raise_error
        return SendResponse(
            ok=self.result_status == "sent",
            ticket_id=request.ticket_id,
            status=self.result_status,
            current_url=f"https://langgenius.zendesk.com/agent/tickets/{request.ticket_id}",
        )


def test_finish_request_normalizes_detailed_agent_failure() -> None:
    request = FinishTicketRunRequest(
        ticket_id=3726,
        run_token="r" * 32,
        outcome="ALERT: send failed after the final freshness check",
    )

    assert request.outcome == "failed"


def test_finish_request_preserves_handoff_outcome() -> None:
    request = FinishTicketRunRequest(
        ticket_id=3726,
        run_token="r" * 32,
        outcome="handoff_billing",
    )

    assert request.outcome == "handoff_billing"


def _client(monkeypatch, tmp_path):
    monkeypatch.setenv("RELAY_API_KEY", "x" * 32)
    from zendesk_browser_relay.app import create_app, get_coordinator, get_relay
    from zendesk_browser_relay.coordination import TicketRunCoordinator
    from zendesk_browser_relay.settings import get_settings

    get_settings.cache_clear()
    relay = FakeRelay()
    app = create_app()
    app.dependency_overrides[get_relay] = lambda: relay
    app.dependency_overrides[get_coordinator] = lambda: TicketRunCoordinator(
        tmp_path / "ticket-runs.sqlite3"
    )
    return TestClient(app), relay


def _client_with_relay(monkeypatch, tmp_path, relay):
    monkeypatch.setenv("RELAY_API_KEY", "x" * 32)
    from zendesk_browser_relay.app import create_app, get_coordinator, get_relay
    from zendesk_browser_relay.coordination import TicketRunCoordinator
    from zendesk_browser_relay.settings import get_settings

    get_settings.cache_clear()
    app = create_app()
    app.dependency_overrides[get_relay] = lambda: relay
    app.dependency_overrides[get_coordinator] = lambda: TicketRunCoordinator(
        tmp_path / "ticket-runs.sqlite3"
    )
    return TestClient(app)


def test_mcp_rejects_missing_bearer_token(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)

    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )

    assert response.status_code == 401


def test_mcp_initialize_and_tools_list(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    headers = {"Authorization": "Bearer " + "x" * 32}

    init_response = client.post(
        "/mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    )
    tools_response = client.post(
        "/mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    )

    assert init_response.status_code == 200
    assert init_response.json()["result"]["capabilities"]["tools"] == {}
    tools = tools_response.json()["result"]["tools"]
    assert {tool["name"] for tool in tools} == {
        "claim_zendesk_ticket_run",
        "bind_codex_ticket_job",
        "accept_codex_ticket_result",
        "check_zendesk_ticket_run",
        "finish_zendesk_ticket_run",
        "send_zendesk_native_message",
    }
    send_tool = next(
        tool for tool in tools if tool["name"] == "send_zendesk_native_message"
    )
    assert send_tool["inputSchema"]["required"] == [
        "ticket_id",
        "body",
        "run_token",
        "snapshot_customer_message_count",
        "snapshot_customer_last_message_id",
    ]
    assert "handoff_type" in send_tool["inputSchema"]["properties"]
    claim_tool = next(
        tool for tool in tools if tool["name"] == "claim_zendesk_ticket_run"
    )
    assert "customer_requested_human" in claim_tool["inputSchema"]["properties"]


def test_mcp_binds_and_accepts_only_the_latest_codex_cursor(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("RELAY_API_KEY", "x" * 32)
    from zendesk_browser_relay.app import create_app, get_coordinator, get_relay
    from zendesk_browser_relay.coordination import TicketRunCoordinator
    from zendesk_browser_relay.settings import get_settings

    get_settings.cache_clear()
    coordinator = TicketRunCoordinator(
        tmp_path / "ticket-runs.sqlite3",
        coalesce_active_customer_messages=True,
    )
    app = create_app()
    app.dependency_overrides[get_relay] = lambda: FakeRelay()
    app.dependency_overrides[get_coordinator] = lambda: coordinator
    client = TestClient(app)
    first = _call_tool(
        client,
        "claim_zendesk_ticket_run",
        {
            "ticket_id": 3880,
            "event_id": "message-1",
            "customer_last_message_id": "message-1",
            "customer_message_count": 1,
        },
    )["result"]["structuredContent"]
    job_id = "11111111-1111-4111-8111-111111111111"
    bound = _call_tool(
        client,
        "bind_codex_ticket_job",
        {"ticket_id": 3880, "run_token": first["run_token"], "job_id": job_id},
    )["result"]["structuredContent"]
    _call_tool(
        client,
        "claim_zendesk_ticket_run",
        {
            "ticket_id": 3880,
            "event_id": "message-2",
            "customer_last_message_id": "message-2",
            "customer_message_count": 2,
        },
    )
    stale = _call_tool(
        client,
        "accept_codex_ticket_result",
        {
            "ticket_id": 3880,
            "run_token": first["run_token"],
            "job_id": job_id,
            "analyzed_customer_last_message_id": "message-1",
            "analyzed_customer_message_count": 1,
        },
    )["result"]["structuredContent"]

    assert bound["bound"] is True
    assert stale["accepted"] is False
    assert stale["reason"] == "customer_snapshot_changed"
    assert stale["dirty"] is True


def test_mcp_explicit_human_claim_blocks_later_customer_message(
    monkeypatch, tmp_path
):
    client, _ = _client(monkeypatch, tmp_path)
    first = _call_tool(
        client,
        "claim_zendesk_ticket_run",
        {
            "ticket_id": 3880,
            "event_id": "message-human",
            "worker_id": "workflow-human",
            "customer_last_message_id": "message-human",
            "customer_message_count": 1,
            "customer_requested_human": True,
        },
    )["result"]["structuredContent"]
    _call_tool(
        client,
        "finish_zendesk_ticket_run",
        {
            "ticket_id": 3880,
            "run_token": first["run_token"],
            "outcome": "human",
        },
    )
    later = _call_tool(
        client,
        "claim_zendesk_ticket_run",
        {
            "ticket_id": 3880,
            "event_id": "message-later",
            "worker_id": "workflow-later",
            "customer_last_message_id": "message-later",
            "customer_message_count": 2,
        },
    )["result"]["structuredContent"]

    assert first["reason"] == "human_takeover_required"
    assert first["human_owned"] is True
    assert later["should_process"] is False
    assert later["reason"] == "human_owned"


def test_mcp_handoff_send_locks_future_customer_message(monkeypatch, tmp_path):
    client, relay = _client(monkeypatch, tmp_path)
    claim = _call_tool(
        client,
        "claim_zendesk_ticket_run",
        {
            "ticket_id": 3880,
            "event_id": "message-billing",
            "customer_last_message_id": "message-billing",
            "customer_message_count": 1,
        },
    )["result"]["structuredContent"]
    sent = _call_tool(
        client,
        "send_zendesk_native_message",
        {
            "ticket_id": 3880,
            "body": "Billing-team handoff explanation.",
            "run_token": claim["run_token"],
            "snapshot_customer_last_message_id": "message-billing",
            "snapshot_customer_message_count": 1,
            "handoff_type": "billing",
        },
    )["result"]["structuredContent"]
    later = _call_tool(
        client,
        "claim_zendesk_ticket_run",
        {
            "ticket_id": 3880,
            "event_id": "message-after-billing",
            "customer_last_message_id": "message-after-billing",
            "customer_message_count": 2,
        },
    )["result"]["structuredContent"]

    assert sent["status"] == "sent"
    assert len(relay.requests) == 1
    assert later["should_process"] is False
    assert later["reason"] == "human_owned"


def test_mcp_tool_call_sends_native_message(monkeypatch, tmp_path):
    client, relay = _client(monkeypatch, tmp_path)

    response = client.post(
        "/mcp",
        headers={"Authorization": "Bearer " + "x" * 32},
        json={
            "jsonrpc": "2.0",
            "id": "call-1",
            "method": "tools/call",
            "params": {
                "name": "send_zendesk_native_message",
                "arguments": {"ticket_id": 2861, "body": "hello"},
            },
        },
    )

    body = response.json()
    assert response.status_code == 200
    assert body["id"] == "call-1"
    assert body["result"]["isError"] is False
    assert body["result"]["structuredContent"]["status"] == "sent"
    assert relay.requests[0].ticket_id == 2861
    assert relay.requests[0].body == f"hello\n\n{CUSTOMER_REPLY_FOOTER}"


def test_mcp_normalizes_paragraphs_and_appends_footer_once(monkeypatch, tmp_path):
    client, relay = _client(monkeypatch, tmp_path)
    body = (
        "Hi,\\n\\nFirst paragraph.\\n\\n\\nSecond paragraph.\n\n"
        f"{CUSTOMER_REPLY_FOOTER}"
    )

    response = client.post(
        "/mcp",
        headers={"Authorization": "Bearer " + "x" * 32},
        json={
            "jsonrpc": "2.0",
            "id": "format-call",
            "method": "tools/call",
            "params": {
                "name": "send_zendesk_native_message",
                "arguments": {"ticket_id": 2861, "body": body},
            },
        },
    )

    assert response.status_code == 200
    sent_body = relay.requests[0].body
    assert sent_body == (
        "Hi,\n\nFirst paragraph.\n\nSecond paragraph.\n\n"
        f"{CUSTOMER_REPLY_FOOTER}"
    )
    assert sent_body.count("This reply was sent by Dify AI Agent.") == 1


def test_mcp_expands_single_line_email_sections_into_paragraphs(monkeypatch, tmp_path):
    client, relay = _client(monkeypatch, tmp_path)
    body = (
        "Hi,\n"
        "Thank you for providing your correct email address.\n"
        "Here are the verified details for your workspace.\n"
        "- Paid pool: 5,000 quota, 5,000 used, 0 remaining\n"
        "- Trial pool: 200 quota, 200 used, 0 remaining\n"
        "Please refresh the page and let us know if the count still does not match.\n"
        "Best regards,\n"
        "Dify Support"
    )

    response = client.post(
        "/mcp",
        headers={"Authorization": "Bearer " + "x" * 32},
        json={
            "jsonrpc": "2.0",
            "id": "single-line-format-call",
            "method": "tools/call",
            "params": {
                "name": "send_zendesk_native_message",
                "arguments": {"ticket_id": 2861, "body": body},
            },
        },
    )

    assert response.status_code == 200
    assert relay.requests[0].body == (
        "Hi,\n\n"
        "Thank you for providing your correct email address.\n\n"
        "Here are the verified details for your workspace.\n\n"
        "- Paid pool: 5,000 quota, 5,000 used, 0 remaining\n"
        "- Trial pool: 200 quota, 200 used, 0 remaining\n\n"
        "Please refresh the page and let us know if the count still does not match.\n\n"
        "Best regards,\n"
        "Dify Support\n\n"
        f"{CUSTOMER_REPLY_FOOTER}"
    )


def test_sse_endpoint_advertises_mcp_endpoint(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)

    response = client.get("/sse", headers={"Authorization": "Bearer " + "x" * 32})

    assert response.status_code == 200
    assert "text/event-stream" in response.headers["content-type"]
    assert "event: endpoint" in response.text
    assert "data: /mcp" in response.text


def test_sse_endpoint_can_advertise_public_mcp_path(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)

    response = client.get(
        "/sse",
        headers={
            "Authorization": "Bearer " + "x" * 32,
            "X-MCP-Endpoint": "/zendesk-reply-mcp",
        },
    )

    assert response.status_code == 200
    assert "data: /zendesk-reply-mcp" in response.text


def _call_tool(client, name, arguments, *, request_id="call"):
    return client.post(
        "/mcp",
        headers={"Authorization": "Bearer " + "x" * 32},
        json={
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    ).json()


def test_mcp_supersedes_on_new_event_and_prevents_stale_snapshot_send(
    monkeypatch, tmp_path
):
    client, relay = _client(monkeypatch, tmp_path)

    first = _call_tool(
        client,
        "claim_zendesk_ticket_run",
        {
            "ticket_id": 3880,
            "event_id": "message-1",
            "customer_last_message_id": "message-1",
            "customer_message_count": 1,
        },
        request_id="claim-1",
    )["result"]["structuredContent"]
    second = _call_tool(
        client,
        "claim_zendesk_ticket_run",
        {
            "ticket_id": 3880,
            "event_id": "message-2",
            "customer_last_message_id": "message-2",
            "customer_message_count": 2,
        },
        request_id="claim-2",
    )["result"]["structuredContent"]

    stale_send = _call_tool(
        client,
        "send_zendesk_native_message",
        {
            "ticket_id": 3880,
            "body": "stale answer",
            "run_token": first["run_token"],
            "snapshot_customer_last_message_id": "message-1",
            "snapshot_customer_message_count": 1,
        },
        request_id="send-stale",
    )

    assert second["generation"] == first["generation"] + 1
    assert second["run_token"] != first["run_token"]
    assert second["should_process"] is True
    assert second["reason"] == "superseded_by_new_customer_message"
    assert stale_send["result"]["isError"] is True
    assert (
        stale_send["result"]["structuredContent"]["status"]
        == "superseded"
    )
    assert relay.requests == []


def test_mcp_current_generation_sends_once(monkeypatch, tmp_path):
    client, relay = _client(monkeypatch, tmp_path)
    claim = _call_tool(
        client,
        "claim_zendesk_ticket_run",
        {
            "ticket_id": 3880,
            "event_id": "message-1",
            "customer_last_message_id": "message-1",
            "customer_message_count": 1,
        },
    )["result"]["structuredContent"]
    arguments = {
        "ticket_id": 3880,
        "body": "current answer",
        "run_token": claim["run_token"],
        "snapshot_customer_last_message_id": "message-1",
        "snapshot_customer_message_count": 1,
    }

    first_send = _call_tool(
        client, "send_zendesk_native_message", arguments, request_id="send-1"
    )
    duplicate_send = _call_tool(
        client, "send_zendesk_native_message", arguments, request_id="send-2"
    )

    assert first_send["result"]["isError"] is False
    assert duplicate_send["result"]["isError"] is True
    assert duplicate_send["result"]["structuredContent"]["status"] == "already_sent"
    assert len(relay.requests) == 1


def test_internal_handoff_marker_is_stripped_and_locks_human_ownership(
    monkeypatch, tmp_path
):
    client, relay = _client(monkeypatch, tmp_path)
    claim = _call_tool(
        client,
        "claim_zendesk_ticket_run",
        {
            "ticket_id": 3880,
            "event_id": "message-billing",
            "worker_id": "workflow-billing",
            "customer_last_message_id": "message-billing",
            "customer_message_count": 1,
            "customer_requested_human": False,
        },
    )["result"]["structuredContent"]

    response = _call_tool(
        client,
        "send_zendesk_native_message",
        {
            "ticket_id": 3880,
            "body": "[[DIFY_HANDOFF:billing]]\nHi,\n\nOur billing team will review this.\n\nBest regards,\nDify Support",
            "run_token": claim["run_token"],
            "snapshot_customer_last_message_id": "message-billing",
            "snapshot_customer_message_count": 1,
        },
    )["result"]

    assert response["isError"] is False
    assert relay.requests[-1].body.startswith("Hi,")
    assert "DIFY_HANDOFF" not in relay.requests[-1].body
    later = _call_tool(
        client,
        "claim_zendesk_ticket_run",
        {
            "ticket_id": 3880,
            "event_id": "message-after-billing",
            "worker_id": "workflow-after-billing",
            "customer_last_message_id": "message-after-billing",
            "customer_message_count": 2,
            "customer_requested_human": False,
        },
    )["result"]["structuredContent"]
    assert later["should_process"] is False
    assert later["reason"] == "human_owned"
    assert later["human_owner_type"] == "billing"


def test_mcp_send_returns_next_run_when_customer_replies_during_send(
    monkeypatch, tmp_path
):
    client, relay = _client(monkeypatch, tmp_path)
    claim = _call_tool(
        client,
        "claim_zendesk_ticket_run",
        {
            "ticket_id": 3880,
            "event_id": "message-1",
            "worker_id": "workflow-run-1",
            "customer_last_message_id": "message-1",
            "customer_message_count": 1,
        },
    )["result"]["structuredContent"]

    original_send = relay.send_message
    superseding = {}

    async def send_while_customer_replies(request):
        claimed = _call_tool(
            client,
            "claim_zendesk_ticket_run",
            {
                "ticket_id": 3880,
                "event_id": "message-2",
                "worker_id": "workflow-run-2",
                "customer_last_message_id": "message-2",
                "customer_message_count": 2,
            },
            request_id="claim-during-send",
        )["result"]["structuredContent"]
        superseding.update(claimed)
        return await original_send(request)

    relay.send_message = send_while_customer_replies
    sent = _call_tool(
        client,
        "send_zendesk_native_message",
        {
            "ticket_id": 3880,
            "body": "answer for the first message",
            "run_token": claim["run_token"],
            "snapshot_customer_last_message_id": "message-1",
            "snapshot_customer_message_count": 1,
        },
    )["result"]

    assert sent["isError"] is False
    assert sent["structuredContent"]["status"] == "sent"
    assert sent["structuredContent"]["follow_up_required"] is False
    assert sent["structuredContent"]["next_generation"] == 0
    assert sent["structuredContent"]["next_run_token"] == ""
    assert sent["structuredContent"]["customer_last_message_id"] == "message-2"
    assert superseding["generation"] == 2
    assert superseding["run_token"] != claim["run_token"]
    assert superseding["should_process"] is True


def test_mcp_run_token_requires_complete_customer_snapshot(monkeypatch, tmp_path):
    client, relay = _client(monkeypatch, tmp_path)

    response = _call_tool(
        client,
        "send_zendesk_native_message",
        {
            "ticket_id": 3880,
            "body": "answer",
            "run_token": "a" * 32,
        },
    )

    assert response["error"]["code"] == -32602
    assert relay.requests == []


def test_mcp_unknown_delivery_result_remains_fenced(monkeypatch, tmp_path):
    relay = FakeRelay(result_status="send_not_confirmed")
    client = _client_with_relay(monkeypatch, tmp_path, relay)
    claim = _call_tool(
        client,
        "claim_zendesk_ticket_run",
        {
            "ticket_id": 3880,
            "event_id": "message-1",
            "customer_last_message_id": "message-1",
            "customer_message_count": 1,
        },
    )["result"]["structuredContent"]
    arguments = {
        "ticket_id": 3880,
        "body": "answer",
        "run_token": claim["run_token"],
        "snapshot_customer_last_message_id": "message-1",
        "snapshot_customer_message_count": 1,
    }

    first_send = _call_tool(client, "send_zendesk_native_message", arguments)
    second_send = _call_tool(client, "send_zendesk_native_message", arguments)

    assert first_send["result"]["structuredContent"]["status"] == "send_not_confirmed"
    assert second_send["result"]["structuredContent"]["status"] == (
        "delivery_in_progress_or_ambiguous"
    )
    assert len(relay.requests) == 1


def test_mcp_relay_exception_remains_fenced(monkeypatch, tmp_path):
    relay = FakeRelay(raise_error=RuntimeError("response lost"))
    client = _client_with_relay(monkeypatch, tmp_path, relay)
    claim = _call_tool(
        client,
        "claim_zendesk_ticket_run",
        {
            "ticket_id": 3880,
            "event_id": "message-1",
            "customer_last_message_id": "message-1",
            "customer_message_count": 1,
        },
    )["result"]["structuredContent"]
    arguments = {
        "ticket_id": 3880,
        "body": "answer",
        "run_token": claim["run_token"],
        "snapshot_customer_last_message_id": "message-1",
        "snapshot_customer_message_count": 1,
    }

    first_send = _call_tool(client, "send_zendesk_native_message", arguments)
    second_send = _call_tool(client, "send_zendesk_native_message", arguments)

    assert first_send["error"]["code"] == -32000
    assert second_send["result"]["structuredContent"]["status"] == (
        "delivery_in_progress_or_ambiguous"
    )
    assert len(relay.requests) == 1


def test_mcp_relay_exception_does_not_cancel_the_superseding_worker(
    monkeypatch, tmp_path
):
    relay = FakeRelay(raise_error=RuntimeError("response lost"))
    client = _client_with_relay(monkeypatch, tmp_path, relay)
    claim = _call_tool(
        client,
        "claim_zendesk_ticket_run",
        {
            "ticket_id": 3926,
            "event_id": "ticket-created",
            "worker_id": "workflow-run-1",
            "customer_last_message_id": "",
            "customer_message_count": 0,
        },
    )["result"]["structuredContent"]

    original_send = relay.send_message
    superseding = {}

    async def fail_after_customer_message(request):
        claimed = _call_tool(
            client,
            "claim_zendesk_ticket_run",
            {
                "ticket_id": 3926,
                "event_id": "message-refund",
                "worker_id": "workflow-run-2",
                "customer_last_message_id": "message-refund",
                "customer_message_count": 1,
            },
            request_id="claim-during-failed-send",
        )["result"]["structuredContent"]
        superseding.update(claimed)
        return await original_send(request)

    relay.send_message = fail_after_customer_message
    result = _call_tool(
        client,
        "send_zendesk_native_message",
        {
            "ticket_id": 3926,
            "body": "What can we help you with?",
            "run_token": claim["run_token"],
            "snapshot_customer_last_message_id": "",
            "snapshot_customer_message_count": 0,
        },
    )

    assert result["error"]["code"] == -32000
    assert superseding["generation"] == 2
    assert superseding["run_token"] != claim["run_token"]
    assert superseding["should_process"] is True
    assert superseding["customer_last_message_id"] == "message-refund"
