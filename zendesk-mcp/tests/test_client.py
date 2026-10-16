from __future__ import annotations

import requests
from requests.exceptions import Timeout

from zendesk_mcp.client import ZendeskClient
from zendesk_mcp.settings import Settings


def _settings(max_retries: int) -> Settings:
    return Settings(
        api_key="z" * 32,
        host="127.0.0.1",
        port=8100,
        workers=1,
        audit_log="./audit.jsonl",
        allowed_custom_field_ids="",
        request_timeout_seconds=30.0,
        connect_timeout_seconds=3.0,
        read_timeout_seconds=20.0,
        max_retries=max_retries,
        cache_ttl_seconds=300.0,
        cache_max_entries=256,
        download_max_bytes=10 * 1024 * 1024,
        zendesk_subdomain="example",
        zendesk_email="agent@example.com",
        zendesk_api_token="token",
    )


def test_zendesk_client_retries_transient_timeout():
    class FlakySession:
        def __init__(self) -> None:
            self.calls = 0

        def request(self, method, url, *, params=None, json=None, timeout=None):
            self.calls += 1
            assert timeout == (3.0, 20.0)
            if self.calls == 1:
                raise Timeout()
            response = requests.Response()
            response.status_code = 200
            response._content = b'{"views":[]}'
            return response

    client = ZendeskClient(_settings(max_retries=1))
    session = FlakySession()
    client.session = session

    assert client._get("views") == {"views": []}
    assert session.calls == 2


def test_zendesk_client_downloads_sc_attachment_via_ticket_attachment_redirect(monkeypatch):
    class FakeSession:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def get(self, url, *, stream=False, timeout=None, allow_redirects=True):
            self.calls.append(
                {
                    "url": url,
                    "stream": stream,
                    "timeout": timeout,
                    "allow_redirects": allow_redirects,
                }
            )
            response = requests.Response()
            response.status_code = 302
            response.headers["Location"] = "https://signed.example/download"
            return response

    def unsigned_get(url, *, stream=False, timeout=None):
        unsigned_calls.append({"url": url, "stream": stream, "timeout": timeout})
        response = requests.Response()
        response.status_code = 200
        response.headers["Content-Type"] = "image/png"
        response._content = b"PNGDATA"
        response._content_consumed = True
        return response

    unsigned_calls: list[dict] = []
    client = ZendeskClient(_settings(max_retries=0))
    session = FakeSession()
    client.session = session
    monkeypatch.setattr("zendesk_mcp.client.requests.get", unsigned_get)

    downloaded = client.download_attachment(
        "https://example.zendesk.com/sc/attachments/v2/01ABC/screenshot.png",
        max_bytes=1024,
    )

    assert downloaded == {"content": b"PNGDATA", "content_type": "image/png", "size": 7}
    assert session.calls == [
        {
            "url": "https://example.zendesk.com/api/v2/ticket_attachments/01ABC/content",
            "stream": False,
            "timeout": (3.0, 20.0),
            "allow_redirects": False,
        }
    ]
    assert unsigned_calls == [
        {"url": "https://signed.example/download", "stream": True, "timeout": (3.0, 20.0)}
    ]


def test_zendesk_client_adds_internal_note_as_private_comment():
    class FakeSession:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def request(self, method, url, *, params=None, json=None, timeout=None):
            self.calls.append(
                {
                    "method": method,
                    "url": url,
                    "params": params,
                    "json": json,
                    "timeout": timeout,
                }
            )
            response = requests.Response()
            response.status_code = 200
            response._content = b'{"ticket":{"id":123,"updated_at":"u2"}}'
            return response

    client = ZendeskClient(_settings(max_retries=0))
    session = FakeSession()
    client.session = session

    result = client.add_internal_note(123, "Investigated from logs.")

    assert result == {"ticket": {"id": 123, "updated_at": "u2"}}
    assert session.calls == [
        {
            "method": "PUT",
            "url": "https://example.zendesk.com/api/v2/tickets/123",
            "params": None,
            "json": {
                "ticket": {
                    "comment": {
                        "body": "Investigated from logs.",
                        "public": False,
                    }
                }
            },
            "timeout": (3.0, 20.0),
        }
    ]
