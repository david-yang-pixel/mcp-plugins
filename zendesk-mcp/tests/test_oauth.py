from __future__ import annotations

import dataclasses
import json
import os
import stat
import time
from pathlib import Path

import pytest
import requests

from zendesk_mcp import client as client_module
from zendesk_mcp import oauth
from zendesk_mcp.client import ZendeskApiError, ZendeskClient
from zendesk_mcp.oauth import OAuthError, OAuthTokenProvider, ZendeskBearerAuth
from zendesk_mcp.settings import Settings

SUBDOMAIN = "example"


def _seed(**overrides) -> dict:
    data = {
        "subdomain": SUBDOMAIN,
        "client_id": "dify_zendesk_cli",
        "grant_type": "authorization_code",
        "scope": "read tickets:read",
        "user_id": 1234,
        "access_token": "seed-access",
        "expires_at": time.time() - 10,
        "refresh_token": "seed-refresh",
    }
    data.update(overrides)
    return data


def _write_seed(path: Path, data: dict) -> Path:
    path.write_text(json.dumps(data))
    return path


def _stored(path: Path) -> dict:
    return json.loads(path.read_text())


class FakeTokenEndpoint:
    def __init__(self, monkeypatch, responses):
        self.calls: list[dict] = []
        self.responses = list(responses)
        monkeypatch.setattr(oauth.requests, "post", self.post)

    def post(self, url, *, json=None, timeout=None, allow_redirects=True):
        assert url == f"https://{SUBDOMAIN}.zendesk.com/oauth/tokens"
        assert allow_redirects is False
        self.calls.append(json)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        status, body = item
        response = requests.Response()
        response.status_code = status
        response._content = (body if isinstance(body, str) else __import__("json").dumps(body)).encode()
        return response


def _provider(tmp_path: Path, seed: dict | None = None, **kwargs) -> OAuthTokenProvider:
    seed_path = _write_seed(tmp_path / "seed.json", seed) if seed is not None else None
    return OAuthTokenProvider(
        subdomain=SUBDOMAIN,
        path=str(tmp_path / "state" / "credentials.json"),
        seed_path=str(seed_path) if seed_path else "",
        **kwargs,
    )


def test_seed_is_imported_and_refreshed_with_rotation(tmp_path, monkeypatch):
    endpoint = FakeTokenEndpoint(
        monkeypatch,
        [(200, {"access_token": "new-access", "expires_in": 1800, "refresh_token": "rotated", "token_type": "bearer"})],
    )
    provider = _provider(tmp_path, _seed())

    assert provider.access_token() == "new-access"

    assert endpoint.calls == [
        {"client_id": "dify_zendesk_cli", "expires_in": 1800, "grant_type": "refresh_token", "refresh_token": "seed-refresh"}
    ]
    stored = _stored(provider.path)
    assert stored["refresh_token"] == "rotated"
    assert stored["access_token"] == "new-access"
    assert stored["seed_sha256"]
    assert stored["refreshed_at"] > 0
    assert stat.S_IMODE(os.stat(provider.path).st_mode) == 0o600
    # A cached, valid token needs neither the file nor the network.
    assert provider.access_token() == "new-access"
    assert len(endpoint.calls) == 1


def test_valid_seed_access_token_is_used_without_refresh(tmp_path, monkeypatch):
    endpoint = FakeTokenEndpoint(monkeypatch, [])
    provider = _provider(tmp_path, _seed(expires_at=time.time() + 600))
    assert provider.access_token() == "seed-access"
    assert endpoint.calls == []


def test_unchanged_seed_never_overwrites_rotated_tokens(tmp_path, monkeypatch):
    FakeTokenEndpoint(
        monkeypatch,
        [(200, {"access_token": "new-access", "expires_in": 1800, "refresh_token": "rotated"})],
    )
    seed = _seed()
    provider = _provider(tmp_path, seed)
    provider.access_token()

    # A redeploy mounts the same seed again; a new process must keep the rotation.
    restarted = _provider(tmp_path, seed)
    assert restarted.access_token() == "new-access"
    assert _stored(restarted.path)["refresh_token"] == "rotated"


def test_changed_seed_replaces_stored_credentials(tmp_path, monkeypatch):
    endpoint = FakeTokenEndpoint(
        monkeypatch,
        [
            (200, {"access_token": "first", "expires_in": 1800, "refresh_token": "rotated-1"}),
            (200, {"access_token": "second", "expires_in": 1800, "refresh_token": "rotated-2"}),
        ],
    )
    provider = _provider(tmp_path, _seed())
    provider.access_token()

    reauthorized = _provider(tmp_path, _seed(refresh_token="reauthorized-refresh"))
    assert reauthorized.access_token() == "second"
    assert endpoint.calls[-1]["refresh_token"] == "reauthorized-refresh"
    assert _stored(reauthorized.path)["refresh_token"] == "rotated-2"


@pytest.mark.parametrize(
    "seed, message",
    [
        (_seed(subdomain="other"), "another Zendesk account"),
        (_seed(refresh_token=""), "no refresh token"),
        (_seed(grant_type="password"), "Unsupported"),
        (_seed(client_id=""), "client_id"),
    ],
)
def test_invalid_seed_is_rejected_without_storing(tmp_path, monkeypatch, seed, message):
    endpoint = FakeTokenEndpoint(monkeypatch, [])
    provider = _provider(tmp_path, seed)
    with pytest.raises(OAuthError, match=message) as raised:
        provider.access_token()
    assert raised.value.reauthorize
    assert not provider.path.exists()
    assert endpoint.calls == []


def test_seed_of_unexpected_user_is_rejected(tmp_path, monkeypatch):
    FakeTokenEndpoint(monkeypatch, [])
    provider = _provider(tmp_path, _seed(user_id=999), expected_user_id="1234")
    with pytest.raises(OAuthError, match="unexpected user"):
        provider.access_token()
    assert not provider.path.exists()


def test_rejected_refresh_requires_reauthorization_and_hides_tokens(tmp_path, monkeypatch):
    FakeTokenEndpoint(monkeypatch, [(400, {"error": "invalid_grant", "error_description": "seed-refresh"})])
    provider = _provider(tmp_path, _seed())
    with pytest.raises(OAuthError) as raised:
        provider.access_token()
    assert raised.value.reauthorize
    assert "seed-refresh" not in str(raised.value)
    assert "reauthorize" in str(raised.value)
    # The stored refresh token is unchanged, so a transient issue can be retried.
    assert _stored(provider.path)["refresh_token"] == "seed-refresh"
    assert provider.status()["last_error"] == str(raised.value)


def test_missing_rotated_refresh_token_keeps_previous_file(tmp_path, monkeypatch):
    FakeTokenEndpoint(monkeypatch, [(200, {"access_token": "new-access", "expires_in": 1800})])
    provider = _provider(tmp_path, _seed())
    with pytest.raises(OAuthError, match="rotated refresh token"):
        provider.access_token()
    assert _stored(provider.path)["refresh_token"] == "seed-refresh"


def test_network_failure_is_not_a_reauthorization(tmp_path, monkeypatch):
    FakeTokenEndpoint(monkeypatch, [requests.ConnectionError("boom")])
    provider = _provider(tmp_path, _seed())
    with pytest.raises(OAuthError) as raised:
        provider.access_token()
    assert not raised.value.reauthorize


def test_client_credentials_grant_requests_new_token(tmp_path, monkeypatch):
    endpoint = FakeTokenEndpoint(monkeypatch, [(200, {"access_token": "cc-access", "expires_in": 1800})])
    seed = _seed(grant_type="client_credentials", client_secret="secret", refresh_token=None)
    provider = _provider(tmp_path, seed)
    assert provider.access_token() == "cc-access"
    assert endpoint.calls[0]["grant_type"] == "client_credentials"
    assert endpoint.calls[0]["client_secret"] == "secret"
    assert endpoint.calls[0]["scope"] == "read tickets:read"


def test_rejected_token_forces_a_refresh(tmp_path, monkeypatch):
    endpoint = FakeTokenEndpoint(
        monkeypatch,
        [(200, {"access_token": "replacement", "expires_in": 1800, "refresh_token": "rotated"})],
    )
    provider = _provider(tmp_path, _seed(expires_at=time.time() + 600))
    assert provider.access_token() == "seed-access"
    assert provider.access_token(rejected="seed-access") == "replacement"
    assert len(endpoint.calls) == 1


def test_keepalive_rotates_only_stale_credentials(tmp_path, monkeypatch):
    endpoint = FakeTokenEndpoint(
        monkeypatch,
        [(200, {"access_token": "kept-alive", "expires_in": 1800, "refresh_token": "rotated"})],
    )
    provider = _provider(tmp_path, _seed(expires_at=time.time() + 600))
    # A freshly imported seed has never been refreshed by this server.
    assert provider.keepalive(24 * 3600) is True
    assert provider.keepalive(24 * 3600) is False
    assert len(endpoint.calls) == 1

    empty = OAuthTokenProvider(subdomain=SUBDOMAIN, path=str(tmp_path / "none" / "c.json"))
    assert empty.keepalive(24 * 3600) is False


def test_status_reports_no_secret_values(tmp_path, monkeypatch):
    FakeTokenEndpoint(
        monkeypatch,
        [(200, {"access_token": "new-access", "expires_in": 1800, "refresh_token": "rotated"})],
    )
    provider = _provider(tmp_path, _seed())
    assert provider.status() == {"credentials": False, "seed": True}
    provider.access_token()
    status = provider.status()
    assert status["credentials"] is True
    assert status["user_id"] == 1234
    assert status["access_token_valid"] is True
    assert status["seed_imported"] is True
    text = json.dumps(status)
    for secret in ("new-access", "rotated", "seed-refresh", "seed-access"):
        assert secret not in text


def test_world_readable_credential_file_is_refused(tmp_path):
    provider = _provider(tmp_path)
    provider.path.parent.mkdir(parents=True)
    provider.path.write_text(json.dumps(_seed()))
    os.chmod(provider.path, 0o644)
    with pytest.raises(OAuthError, match="private"):
        provider.access_token()


def test_bearer_auth_is_sent_only_to_the_zendesk_origin(tmp_path, monkeypatch):
    FakeTokenEndpoint(monkeypatch, [])
    auth = ZendeskBearerAuth(_provider(tmp_path, _seed(expires_at=time.time() + 600)))
    for url, expected in [
        (f"https://{SUBDOMAIN}.zendesk.com/api/v2/tickets/1", "Bearer seed-access"),
        (f"http://{SUBDOMAIN}.zendesk.com/api/v2/tickets/1", None),
        ("https://cdn.example.net/attachment", None),
        (f"https://{SUBDOMAIN}.zendesk.com.evil.example/x", None),
    ]:
        prepared = requests.Request("GET", url, headers={"Authorization": "Basic stale"}).prepare()
        auth(prepared)
        assert prepared.headers.get("Authorization") == expected, url


# --------------------------------------------------------------- client wiring
def _oauth_settings(tmp_path: Path, seed: dict) -> Settings:
    seed_path = _write_seed(tmp_path / "seed.json", seed)
    return Settings(
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
        zendesk_oauth_seed_file=str(seed_path),
    )


def test_client_sends_bearer_and_refreshes_once_after_401(tmp_path, monkeypatch):
    endpoint = FakeTokenEndpoint(
        monkeypatch,
        [(200, {"access_token": "replacement", "expires_in": 1800, "refresh_token": "rotated"})],
    )
    settings = _oauth_settings(tmp_path, _seed(expires_at=time.time() + 600))
    seen: list[str] = []

    def fake_send(session, request, **kwargs):
        seen.append(request.headers.get("Authorization"))
        response = requests.Response()
        response.request = request
        response.status_code = 401 if len(seen) == 1 else 200
        response._content = b'{"views": []}'
        return response

    monkeypatch.setattr(requests.Session, "send", fake_send)
    client = ZendeskClient(settings)
    assert client._get("views") == {"views": []}
    assert seen == ["Bearer seed-access", "Bearer replacement"]
    assert len(endpoint.calls) == 1


def test_client_reports_reauthorization_without_calling_zendesk(tmp_path, monkeypatch):
    FakeTokenEndpoint(monkeypatch, [(400, {"error": "invalid_grant"})])
    settings = _oauth_settings(tmp_path, _seed())
    monkeypatch.setattr(
        requests.Session, "send", lambda *a, **k: (_ for _ in ()).throw(AssertionError("API called"))
    )
    with pytest.raises(ZendeskApiError) as raised:
        ZendeskClient(settings)._get("views")
    assert raised.value.status_code == 401
    assert "reauthorize" in raised.value.message


def test_oauth_mode_does_not_need_api_token(tmp_path):
    settings = _oauth_settings(tmp_path, _seed())
    ZendeskClient(settings)._ensure_configured()
    with pytest.raises(ZendeskApiError, match="ZENDESK_OAUTH_FILE"):
        ZendeskClient(dataclasses.replace(settings, zendesk_oauth_file=""))._ensure_configured()
    with pytest.raises(ZendeskApiError, match="ZENDESK_AUTH_MODE"):
        ZendeskClient(dataclasses.replace(settings, zendesk_auth_mode="basic"))._ensure_configured()


def test_settings_default_to_api_token(monkeypatch):
    from zendesk_mcp import settings as settings_module

    monkeypatch.chdir(Path(__file__).parent)
    for name in ("ZENDESK_AUTH_MODE", "ZENDESK_OAUTH_FILE", "ZENDESK_OAUTH_SEED_FILE"):
        monkeypatch.delenv(name, raising=False)
    settings_module.get_settings.cache_clear()
    try:
        loaded = settings_module.get_settings()
        assert loaded.zendesk_auth_mode == "api_token"
        assert not loaded.uses_oauth
        monkeypatch.setenv("ZENDESK_AUTH_MODE", "OAuth")
        settings_module.get_settings.cache_clear()
        assert settings_module.get_settings().uses_oauth
    finally:
        settings_module.get_settings.cache_clear()


def test_health_reports_oauth_state(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from zendesk_mcp.app import create_app
    from zendesk_mcp.settings import get_settings

    settings = _oauth_settings(tmp_path, _seed())
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    body = TestClient(app).get("/health").json()
    assert body["ok"] is True
    assert body["zendesk_auth_mode"] == "oauth"
    assert body["zendesk_configured"] is False
    assert body["zendesk_oauth"] == {"credentials": False, "seed": True}


@pytest.fixture(autouse=True)
def _isolated_providers(monkeypatch):
    monkeypatch.setattr(oauth, "_PROVIDERS", {})
    monkeypatch.setattr(client_module, "_THREAD_LOCAL", __import__("threading").local())
