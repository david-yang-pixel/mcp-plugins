from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import os
import stat
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

from zendesk_mcp import client as client_module
from zendesk_mcp import oauth
from zendesk_mcp.client import ZendeskApiError, ZendeskClient
from zendesk_mcp.oauth import (
    DEFAULT_SCOPE,
    NOT_AUTHORIZED,
    REDIRECT_URI,
    OAuthError,
    OAuthTokenProvider,
    ZendeskBearerAuth,
)
from zendesk_mcp.settings import Settings
from zendesk_mcp.tools import handle_tool_call

SUBDOMAIN = "example"
SECRETS = ("the-code", "access-1", "refresh-1", "rotated", "replacement", "stored-access", "stored-refresh")


class FakeZendesk:
    """Token endpoint and /users/me with scripted responses."""

    def __init__(self, monkeypatch, tokens=(), users=()):
        self.token_calls: list[dict] = []
        self.user_calls: list[str] = []
        self.tokens = list(tokens)
        self.users = list(users)
        monkeypatch.setattr(oauth.requests, "post", self.post)
        monkeypatch.setattr(oauth.requests, "get", self.get)

    @staticmethod
    def _response(status, body):
        response = requests.Response()
        response.status_code = status
        response._content = json.dumps(body).encode()
        return response

    def post(self, url, *, json=None, timeout=None, allow_redirects=True):
        assert url == f"https://{SUBDOMAIN}.zendesk.com/oauth/tokens"
        assert allow_redirects is False
        self.token_calls.append(json)
        item = self.tokens.pop(0)
        if isinstance(item, Exception):
            raise item
        return self._response(*item)

    def get(self, url, *, headers=None, timeout=None, allow_redirects=True):
        assert url == f"https://{SUBDOMAIN}.zendesk.com/api/v2/users/me.json"
        self.user_calls.append(headers["Authorization"])
        return self._response(*self.users.pop(0))


def _token(access="access-1", refresh="refresh-1", **extra):
    body = {"access_token": access, "expires_in": 1800, "token_type": "bearer", **extra}
    if refresh is not None:
        body["refresh_token"] = refresh
    return (200, body)


def _me(user_id=1234, name="Agent"):
    return (200, {"user": {"id": user_id, "name": name, "email": "agent@example.com"}})


def _provider(tmp_path: Path, **kwargs) -> OAuthTokenProvider:
    return OAuthTokenProvider(subdomain=SUBDOMAIN, path=str(tmp_path / "state" / "credentials.json"), **kwargs)


def _store(provider: OAuthTokenProvider, **overrides) -> None:
    data = {
        "subdomain": SUBDOMAIN,
        "client_id": "dify_zendesk_cli",
        "grant_type": "authorization_code",
        "scope": DEFAULT_SCOPE,
        "user_id": 1234,
        "access_token": "stored-access",
        "expires_at": time.time() + 600,
        "refresh_token": "stored-refresh",
        "refreshed_at": time.time(),
    }
    data.update(overrides)
    oauth._write_private(provider.path, data)


def _callback(start: dict, code="the-code") -> str:
    state = parse_qs(urlsplit(start["authorization_url"]).query)["state"][0]
    return f"{REDIRECT_URI}?code={code}&state={state}"


def _stored(path: Path) -> dict:
    return json.loads(path.read_text())


# ------------------------------------------------------------- authorization
def test_start_returns_a_pkce_link_and_keeps_the_verifier_private(tmp_path):
    provider = _provider(tmp_path)
    started = provider.start_authorization()

    url = urlsplit(started["authorization_url"])
    query = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert (url.scheme, url.hostname, url.path) == ("https", f"{SUBDOMAIN}.zendesk.com", "/oauth/authorizations/new")
    assert query["response_type"] == "code"
    assert query["client_id"] == "dify_zendesk_cli"
    assert query["redirect_uri"] == REDIRECT_URI
    assert query["scope"] == DEFAULT_SCOPE
    assert query["code_challenge_method"] == "S256"
    pending = _stored(provider.pending_path)
    expected = base64.urlsafe_b64encode(hashlib.sha256(pending["verifier"].encode()).digest()).rstrip(b"=").decode()
    assert query["code_challenge"] == expected
    assert query["state"] == pending["state"]
    assert pending["verifier"] not in started["authorization_url"]
    assert stat.S_IMODE(os.stat(provider.pending_path).st_mode) == 0o600


def test_complete_exchanges_the_code_and_stores_tokens(tmp_path, monkeypatch):
    fake = FakeZendesk(monkeypatch, tokens=[_token(scope="read tickets:read")], users=[_me()])
    provider = _provider(tmp_path)
    started = provider.start_authorization()
    verifier = _stored(provider.pending_path)["verifier"]

    result = provider.complete_authorization(_callback(started))

    assert result == {"user_id": 1234, "name": "Agent", "granted_scope": "read tickets:read"}
    exchange = fake.token_calls[0]
    assert exchange["grant_type"] == "authorization_code"
    assert exchange["code"] == "the-code"
    assert exchange["code_verifier"] == verifier
    assert exchange["redirect_uri"] == REDIRECT_URI
    assert exchange["client_id"] == "dify_zendesk_cli"
    assert fake.user_calls == ["Bearer access-1"]
    stored = _stored(provider.path)
    assert stored["refresh_token"] == "refresh-1"
    assert stored["user_id"] == 1234
    assert stored["refreshed_at"] > 0
    assert stat.S_IMODE(os.stat(provider.path).st_mode) == 0o600
    assert not provider.pending_path.exists()
    # The fresh token is used directly, without another request.
    assert provider.access_token() == "access-1"
    assert len(fake.token_calls) == 1


def test_complete_accepts_only_the_query_string(tmp_path, monkeypatch):
    FakeZendesk(monkeypatch, tokens=[_token()], users=[_me()])
    provider = _provider(tmp_path)
    started = provider.start_authorization()
    query = urlsplit(_callback(started)).query
    assert provider.complete_authorization(query)["user_id"] == 1234


def test_wrong_state_is_refused_and_the_link_stays_usable(tmp_path, monkeypatch):
    fake = FakeZendesk(monkeypatch, tokens=[_token()], users=[_me()])
    provider = _provider(tmp_path)
    started = provider.start_authorization()
    with pytest.raises(OAuthError, match="latest authorization link"):
        provider.complete_authorization(f"{REDIRECT_URI}?code=the-code&state=forged")
    assert fake.token_calls == []
    assert provider.complete_authorization(_callback(started))["user_id"] == 1234


@pytest.mark.parametrize(
    "callback, message",
    [
        (f"{REDIRECT_URI}?error=access_denied&state=x", "denied"),
        (f"{REDIRECT_URI}?state=x", "code and state"),
        ("", "full address"),
    ],
)
def test_malformed_callbacks_are_refused(tmp_path, monkeypatch, callback, message):
    fake = FakeZendesk(monkeypatch)
    provider = _provider(tmp_path)
    provider.start_authorization()
    with pytest.raises(OAuthError, match=message):
        provider.complete_authorization(callback)
    assert fake.token_calls == []


def test_complete_without_start_or_after_expiry(tmp_path, monkeypatch):
    FakeZendesk(monkeypatch)
    provider = _provider(tmp_path)
    with pytest.raises(OAuthError, match="zendesk_auth_start first"):
        provider.complete_authorization(f"{REDIRECT_URI}?code=c&state=s")

    started = provider.start_authorization()
    pending = _stored(provider.pending_path)
    oauth._write_private(provider.pending_path, {**pending, "created_at": time.time() - oauth.PENDING_SECONDS - 1})
    with pytest.raises(OAuthError, match="expired"):
        provider.complete_authorization(_callback(started))
    assert not provider.pending_path.exists()


def test_rejected_code_spends_the_link_and_hides_it(tmp_path, monkeypatch):
    FakeZendesk(monkeypatch, tokens=[(400, {"error": "invalid_grant", "error_description": "the-code"})])
    provider = _provider(tmp_path)
    started = provider.start_authorization()
    with pytest.raises(OAuthError) as raised:
        provider.complete_authorization(_callback(started))
    assert "zendesk_auth_start" in str(raised.value)
    assert "the-code" not in str(raised.value)
    assert not provider.pending_path.exists()
    assert not provider.path.exists()


def test_anonymous_identity_is_not_saved(tmp_path, monkeypatch):
    FakeZendesk(monkeypatch, tokens=[_token()], users=[(200, {"user": {"id": None, "name": "Anonymous user"}})])
    provider = _provider(tmp_path)
    started = provider.start_authorization()
    with pytest.raises(OAuthError, match="identity check failed"):
        provider.complete_authorization(_callback(started))
    assert not provider.path.exists()


def test_first_user_is_pinned_for_reauthorization(tmp_path, monkeypatch):
    FakeZendesk(monkeypatch, tokens=[_token(), _token("access-2", "refresh-2")], users=[_me(1234), _me(999, "Other")])
    provider = _provider(tmp_path)
    provider.complete_authorization(_callback(provider.start_authorization()))

    with pytest.raises(OAuthError, match="pinned"):
        provider.complete_authorization(_callback(provider.start_authorization()))
    stored = _stored(provider.path)
    assert stored["user_id"] == 1234
    assert stored["refresh_token"] == "refresh-1"


def test_same_user_can_reauthorize(tmp_path, monkeypatch):
    FakeZendesk(monkeypatch, tokens=[_token(), _token("access-2", "refresh-2")], users=[_me(), _me()])
    provider = _provider(tmp_path)
    provider.complete_authorization(_callback(provider.start_authorization()))
    provider.complete_authorization(_callback(provider.start_authorization()))
    assert _stored(provider.path)["refresh_token"] == "refresh-2"


def test_expected_user_id_overrides_the_pin(tmp_path, monkeypatch):
    FakeZendesk(monkeypatch, tokens=[_token(), _token()], users=[_me(999), _me(1234)])
    first = _provider(tmp_path, expected_user_id="1234")
    with pytest.raises(OAuthError, match="pinned"):
        first.complete_authorization(_callback(first.start_authorization()))
    assert not first.path.exists()
    assert first.complete_authorization(_callback(first.start_authorization()))["user_id"] == 1234

    _store(first, user_id=555)
    switched = _provider(tmp_path, expected_user_id="777")
    FakeZendesk(monkeypatch, tokens=[_token()], users=[_me(777)])
    assert switched.complete_authorization(_callback(switched.start_authorization()))["user_id"] == 777


def test_missing_refresh_token_from_exchange_is_not_saved(tmp_path, monkeypatch):
    FakeZendesk(monkeypatch, tokens=[_token(refresh=None)], users=[_me()])
    provider = _provider(tmp_path)
    with pytest.raises(OAuthError, match="refresh token"):
        provider.complete_authorization(_callback(provider.start_authorization()))
    assert not provider.path.exists()


# -------------------------------------------------------------------- tokens
def test_not_authorized_until_login(tmp_path):
    provider = _provider(tmp_path)
    with pytest.raises(OAuthError) as raised:
        provider.access_token()
    assert str(raised.value) == NOT_AUTHORIZED
    assert raised.value.reauthorize


def test_expired_token_is_refreshed_with_rotation(tmp_path, monkeypatch):
    fake = FakeZendesk(monkeypatch, tokens=[_token("replacement", "rotated")])
    provider = _provider(tmp_path)
    _store(provider, expires_at=time.time() - 5)
    assert provider.access_token() == "replacement"
    assert fake.token_calls == [
        {"grant_type": "refresh_token", "client_id": "dify_zendesk_cli", "refresh_token": "stored-refresh", "expires_in": 1800}
    ]
    assert _stored(provider.path)["refresh_token"] == "rotated"


def test_rejected_refresh_requires_login_and_keeps_file(tmp_path, monkeypatch):
    FakeZendesk(monkeypatch, tokens=[(400, {"error": "invalid_grant", "error_description": "stored-refresh"})])
    provider = _provider(tmp_path)
    _store(provider, expires_at=time.time() - 5)
    with pytest.raises(OAuthError) as raised:
        provider.access_token()
    assert raised.value.reauthorize
    assert "zendesk_auth_start" in str(raised.value)
    assert "stored-refresh" not in str(raised.value)
    assert _stored(provider.path)["refresh_token"] == "stored-refresh"
    assert provider.status()["last_error"] == str(raised.value)


def test_missing_rotated_refresh_token_keeps_previous_file(tmp_path, monkeypatch):
    FakeZendesk(monkeypatch, tokens=[_token(refresh=None)])
    provider = _provider(tmp_path)
    _store(provider, expires_at=time.time() - 5)
    with pytest.raises(OAuthError, match="rotated refresh token"):
        provider.access_token()
    assert _stored(provider.path)["refresh_token"] == "stored-refresh"


def test_network_failure_is_not_a_reauthorization(tmp_path, monkeypatch):
    FakeZendesk(monkeypatch, tokens=[requests.ConnectionError("boom")])
    provider = _provider(tmp_path)
    _store(provider, expires_at=time.time() - 5)
    with pytest.raises(OAuthError) as raised:
        provider.access_token()
    assert not raised.value.reauthorize


def test_rejected_token_forces_a_refresh(tmp_path, monkeypatch):
    fake = FakeZendesk(monkeypatch, tokens=[_token("replacement", "rotated")])
    provider = _provider(tmp_path)
    _store(provider)
    assert provider.access_token() == "stored-access"
    assert provider.access_token(rejected="stored-access") == "replacement"
    assert len(fake.token_calls) == 1


def test_keepalive_rotates_only_stale_credentials(tmp_path, monkeypatch):
    fake = FakeZendesk(monkeypatch, tokens=[_token("kept-alive", "rotated")])
    provider = _provider(tmp_path)
    assert provider.keepalive(24 * 3600) is False  # not authorized yet
    _store(provider, refreshed_at=time.time() - 25 * 3600)
    assert provider.keepalive(24 * 3600) is True
    assert provider.keepalive(24 * 3600) is False
    assert len(fake.token_calls) == 1


def test_credentials_for_another_account_are_refused(tmp_path):
    provider = _provider(tmp_path)
    _store(provider, subdomain="other")
    with pytest.raises(OAuthError, match="another Zendesk account"):
        provider.access_token()


def test_world_readable_credential_file_is_refused(tmp_path):
    provider = _provider(tmp_path)
    _store(provider)
    os.chmod(provider.path, 0o644)
    with pytest.raises(OAuthError, match="private"):
        provider.access_token()


def test_status_reports_no_secret_values(tmp_path, monkeypatch):
    FakeZendesk(monkeypatch, tokens=[_token()], users=[_me()])
    provider = _provider(tmp_path)
    assert provider.status() == {"authorized": False}
    started = provider.start_authorization()
    assert provider.status() == {"authorized": False, "authorization_pending": True}
    provider.complete_authorization(_callback(started))
    status = provider.status()
    assert status["authorized"] is True
    assert status["user_id"] == 1234
    assert status["access_token_valid"] is True
    text = json.dumps(status)
    for secret in SECRETS:
        assert secret not in text


def test_bearer_auth_is_sent_only_to_the_zendesk_origin(tmp_path):
    provider = _provider(tmp_path)
    _store(provider)
    auth = ZendeskBearerAuth(provider)
    for url, expected in [
        (f"https://{SUBDOMAIN}.zendesk.com/api/v2/tickets/1", "Bearer stored-access"),
        (f"http://{SUBDOMAIN}.zendesk.com/api/v2/tickets/1", None),
        ("https://cdn.example.net/attachment", None),
        (f"https://{SUBDOMAIN}.zendesk.com.evil.example/x", None),
    ]:
        prepared = requests.Request("GET", url, headers={"Authorization": "Basic stale"}).prepare()
        auth(prepared)
        assert prepared.headers.get("Authorization") == expected, url


# ------------------------------------------------------- client, tools, app
def _oauth_settings(tmp_path: Path, **overrides) -> Settings:
    settings = Settings(
        api_key="z" * 32,
        host="127.0.0.1",
        port=8100,
        workers=1,
        audit_log=str(tmp_path / "audit.jsonl"),
        allowed_custom_field_ids="",
        request_timeout_seconds=30.0,
        connect_timeout_seconds=3.0,
        read_timeout_seconds=20.0,
        max_retries=0,
        cache_ttl_seconds=0.0,
        cache_max_entries=0,
        download_max_bytes=1024,
        zendesk_subdomain=SUBDOMAIN,
        zendesk_email="",
        zendesk_api_token="",
        zendesk_auth_mode="oauth",
        zendesk_oauth_file=str(tmp_path / "state" / "credentials.json"),
    )
    return dataclasses.replace(settings, **overrides)


def test_client_sends_bearer_and_refreshes_once_after_401(tmp_path, monkeypatch):
    fake = FakeZendesk(monkeypatch, tokens=[_token("replacement", "rotated")])
    settings = _oauth_settings(tmp_path)
    _store(client_module._oauth_provider(settings))
    seen: list[str] = []

    def fake_send(session, request, **kwargs):
        seen.append(request.headers.get("Authorization"))
        response = requests.Response()
        response.request = request
        response.status_code = 401 if len(seen) == 1 else 200
        response._content = b'{"views": []}'
        return response

    monkeypatch.setattr(requests.Session, "send", fake_send)
    assert ZendeskClient(settings)._get("views") == {"views": []}
    assert seen == ["Bearer stored-access", "Bearer replacement"]
    assert len(fake.token_calls) == 1


def test_unauthorized_tool_call_asks_for_login_without_calling_zendesk(tmp_path, monkeypatch):
    monkeypatch.setattr(
        requests.Session, "send", lambda *a, **k: (_ for _ in ()).throw(AssertionError("API called"))
    )
    settings = _oauth_settings(tmp_path)
    with pytest.raises(ZendeskApiError) as raised:
        ZendeskClient(settings)._get("views")
    assert raised.value.status_code == 401
    assert raised.value.message == NOT_AUTHORIZED
    result = handle_tool_call("list_views", {}, settings=settings)
    assert result["success"] is False
    assert result["error"] == NOT_AUTHORIZED


def test_oauth_mode_does_not_need_api_token(tmp_path):
    settings = _oauth_settings(tmp_path)
    ZendeskClient(settings)._ensure_configured()
    with pytest.raises(ZendeskApiError, match="ZENDESK_OAUTH_FILE"):
        ZendeskClient(dataclasses.replace(settings, zendesk_oauth_file=""))._ensure_configured()
    with pytest.raises(ZendeskApiError, match="ZENDESK_AUTH_MODE"):
        ZendeskClient(dataclasses.replace(settings, zendesk_auth_mode="basic"))._ensure_configured()


def test_login_tools_end_to_end(tmp_path, monkeypatch):
    FakeZendesk(monkeypatch, tokens=[_token()], users=[_me()])
    settings = _oauth_settings(tmp_path)

    assert handle_tool_call("zendesk_auth_status", {}, settings=settings) == {
        "success": True, "auth_mode": "oauth", "authorized": False,
    }
    started = handle_tool_call("zendesk_auth_start", {}, settings=settings)
    assert started["success"] is True
    assert started["authorization_url"].startswith(f"https://{SUBDOMAIN}.zendesk.com/oauth/authorizations/new?")
    assert "zendesk_auth_complete" in started["instructions"]

    bad = handle_tool_call("zendesk_auth_complete", {"callback_url": "nonsense"}, settings=settings)
    assert bad["success"] is False and bad["code"] == "ZENDESK_OAUTH_ERROR"

    done = handle_tool_call("zendesk_auth_complete", {"callback_url": _callback(started)}, settings=settings)
    assert done["success"] is True
    assert done["user_id"] == 1234
    assert done["audit_id"]
    status = handle_tool_call("zendesk_auth_status", {}, settings=settings)
    assert status["authorized"] is True and status["user_id"] == 1234

    audit = (tmp_path / "audit.jsonl").read_text()
    assert '"zendesk_oauth_authorized"' in audit
    for value in (json.dumps(done), json.dumps(status), audit):
        for secret in SECRETS:
            assert secret not in value


def test_login_tools_are_disabled_in_api_token_mode(tmp_path):
    settings = _oauth_settings(tmp_path, zendesk_auth_mode="api_token")
    assert handle_tool_call("zendesk_auth_start", {}, settings=settings)["code"] == "OAUTH_DISABLED"
    assert handle_tool_call("zendesk_auth_status", {}, settings=settings) == {"success": True, "auth_mode": "api_token"}


def test_login_tools_are_listed():
    from zendesk_mcp.mcp_schema import TOOL_SCHEMAS

    names = {tool["name"] for tool in TOOL_SCHEMAS}
    assert {"zendesk_auth_status", "zendesk_auth_start", "zendesk_auth_complete"} <= names


def test_settings_default_to_api_token(monkeypatch):
    from zendesk_mcp import settings as settings_module

    monkeypatch.chdir(Path(__file__).parent)
    for name in ("ZENDESK_AUTH_MODE", "ZENDESK_OAUTH_FILE", "ZENDESK_OAUTH_CLIENT_ID", "ZENDESK_OAUTH_SCOPE"):
        monkeypatch.delenv(name, raising=False)
    settings_module.get_settings.cache_clear()
    try:
        loaded = settings_module.get_settings()
        assert loaded.zendesk_auth_mode == "api_token"
        assert loaded.zendesk_oauth_client_id == "dify_zendesk_cli"
        assert not loaded.uses_oauth
        monkeypatch.setenv("ZENDESK_AUTH_MODE", "OAuth")
        settings_module.get_settings.cache_clear()
        assert settings_module.get_settings().uses_oauth
    finally:
        settings_module.get_settings.cache_clear()


def test_health_reports_oauth_state(tmp_path):
    from fastapi.testclient import TestClient

    from zendesk_mcp.app import create_app
    from zendesk_mcp.settings import get_settings

    settings = _oauth_settings(tmp_path)
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    body = TestClient(app).get("/health").json()
    assert body == {
        "ok": True,
        "zendesk_auth_mode": "oauth",
        "zendesk_configured": False,
        "zendesk_oauth": {"authorized": False},
    }


@pytest.fixture(autouse=True)
def _isolated_providers(monkeypatch):
    monkeypatch.setattr(oauth, "_PROVIDERS", {})
    monkeypatch.setattr(client_module, "_THREAD_LOCAL", threading.local())
