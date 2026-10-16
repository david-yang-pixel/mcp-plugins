import json

import pytest

from zendesk_browser_relay.zendesk_freshness import ZendeskMcpFreshnessChecker


@pytest.mark.asyncio
async def test_reads_all_conversation_pages_and_returns_latest_customer_cursor(
    monkeypatch,
):
    requests = []
    responses = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "structuredContent": {
                    "success": True,
                    "data": {
                        "conversation_log": {
                            "events": [
                                {
                                    "id": "message-1",
                                    "created_at": "2026-08-21T01:00:00Z",
                                    "author": {"type": "end-user"},
                                },
                                {
                                    "id": "agent-1",
                                    "created_at": "2026-08-21T01:01:00Z",
                                    "author": {"type": "agent"},
                                },
                            ],
                            "meta": {"has_more": True, "after_cursor": "cursor-1"},
                        }
                    },
                }
            },
        },
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "structuredContent": {
                    "success": True,
                    "data": {
                        "conversation_log": {
                            "events": [
                                {
                                    "id": "message-2",
                                    "created_at": "2026-08-21T01:02:00Z",
                                    "author": {"type": "end-user"},
                                }
                            ],
                            "meta": {"has_more": False},
                        }
                    },
                }
            },
        },
    ]

    class FakeResponse:
        status = 200

        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _limit):
            return json.dumps(self.payload).encode()

    def fake_urlopen(request, timeout):
        requests.append(json.loads(request.data))
        assert timeout == 12
        assert request.get_header("Authorization") == "Bearer zendesk-secret"
        return FakeResponse(responses.pop(0))

    monkeypatch.setattr("zendesk_browser_relay.zendesk_freshness.urlopen", fake_urlopen)
    checker = ZendeskMcpFreshnessChecker(
        endpoint_url="https://zendesk.example.test/mcp",
        authorization="Bearer zendesk-secret",
    )

    cursor = await checker.current_customer_cursor(3880)

    assert cursor == ("message-2", 2)
    assert requests[0]["params"]["name"] == "get_ticket_conversation_log"
    assert "conversation_after" not in requests[0]["params"]["arguments"]
    assert requests[1]["params"]["arguments"]["conversation_after"] == "cursor-1"


@pytest.mark.asyncio
async def test_rejects_incomplete_conversation_pagination(monkeypatch):
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "structuredContent": {
                "success": True,
                "data": {
                    "conversation_log": {
                        "events": [],
                        "meta": {"has_more": True, "after_cursor": ""},
                    }
                },
            }
        },
    }

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _limit):
            return json.dumps(payload).encode()

    monkeypatch.setattr(
        "zendesk_browser_relay.zendesk_freshness.urlopen",
        lambda *_args, **_kwargs: FakeResponse(),
    )
    checker = ZendeskMcpFreshnessChecker(
        endpoint_url="https://zendesk.example.test/mcp",
        authorization="Bearer zendesk-secret",
    )

    with pytest.raises(RuntimeError, match="after_cursor"):
        await checker.current_customer_cursor(3880)
