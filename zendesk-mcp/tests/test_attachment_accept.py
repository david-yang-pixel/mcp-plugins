from dataclasses import replace
from io import BytesIO

import pytest
import requests
from requests.adapters import BaseAdapter
from requests.exceptions import Timeout

from zendesk_mcp.client import ZendeskClient
from zendesk_mcp.settings import get_settings


@pytest.mark.parametrize("content_type,payload", [
    ("text/plain", b"API log sample"),
    ("image/jpeg", b"\xff\xd8\xff\xd9"),
])
@pytest.mark.parametrize("transient_timeout", [False, True])
def test_attachment_accept_survives_redirect_without_mutating_api_headers(
    content_type, payload, transient_timeout
):
    seen = []

    class Adapter(BaseAdapter):
        def send(self, request, **kwargs):
            seen.append(request)
            assert kwargs["timeout"] == (3.0, 20.0)
            if transient_timeout and len(seen) == 1:
                raise Timeout()
            response = requests.Response()
            response.request = request
            response.url = request.url
            if request.url.endswith("/attachment"):
                response.status_code = 302
                response.headers["Location"] = "https://cdn.example/file"
                body = b""
            elif request.url == "https://cdn.example/file":
                assert "Authorization" not in request.headers
                response.status_code = (
                    200 if request.headers.get("Accept") == "*/*" else 406
                )
                response.headers["Content-Type"] = content_type
                body = payload if response.status_code == 200 else b""
            else:
                assert request.headers["Accept"] == "application/json"
                response.status_code = 200
                body = b'{"ticket":{"id":4846}}'
            response.headers["Content-Length"] = str(len(body))
            response.raw = BytesIO(body)
            return response

        def close(self):
            pass

    settings = replace(
        get_settings(), zendesk_subdomain="example", zendesk_email="test@example.com",
        zendesk_api_token="test-token", max_retries=1,
        connect_timeout_seconds=3.0, read_timeout_seconds=20.0,
    )
    client = ZendeskClient(settings)
    with requests.Session() as session:
        session.trust_env = False
        session.headers.update({"Accept": "application/json", "Content-Type": "application/json"})
        session.auth = ("test@example.com/token", "test-token")
        session.mount("https://", Adapter())
        client.session = session
        result = client.download_attachment(
            "https://example.zendesk.com/attachment", max_bytes=1024,
        )
        assert result == {"content": payload, "content_type": content_type, "size": len(payload)}
        assert all(req.headers["Accept"] == "*/*" for req in seen)
        assert len(seen) == (3 if transient_timeout else 2)
        assert session.headers["Accept"] == "application/json"
        assert session.headers["Content-Type"] == "application/json"
        assert client.get_ticket(4846) == {"ticket": {"id": 4846}}
        assert seen[-1].headers["Accept"] == "application/json"
