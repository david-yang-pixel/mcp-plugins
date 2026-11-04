"""Acceptance items 1-7: discovery, registration, PKCE, Dify approval, codes, refresh and expiry."""
import json
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from conftest import API_KEY, REDIRECT, calls, control


def test_unauthenticated_mcp_returns_401_with_resource_metadata(live):
    response = live.http.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert response.status_code == 401
    assert f'resource_metadata="{live.base}/.well-known/oauth-protected-resource/difyctl-mcp/mcp"' in response.headers["www-authenticate"]
    assert live.rpc("wrong-key", "tools/list").status_code == 401


def test_discovery_documents_name_this_issuer_and_resource(live):
    protected = live.http.get("/.well-known/oauth-protected-resource/difyctl-mcp/mcp").json()
    assert protected["resource"] == live.settings.resource
    assert protected["authorization_servers"] == [live.settings.issuer]
    metadata = live.http.get("/.well-known/oauth-authorization-server/difyctl-mcp").json()
    assert metadata["issuer"] == live.settings.issuer
    assert metadata["authorization_endpoint"] == live.settings.issuer + "/authorize"
    assert metadata["registration_endpoint"] == live.settings.issuer + "/register"
    assert metadata["code_challenge_methods_supported"] == ["S256"]
    assert metadata["scopes_supported"] == ["difyctl:read", "difyctl:write", "difyctl:destructive"]


def test_deploy_probe_lists_tools_but_cannot_call_them(live):
    tools = live.rpc(API_KEY, "tools/list").json()["result"]["tools"]
    names = {t["name"] for t in tools}
    assert names == {"difyctl_status", "difyctl_help", "difyctl_guide", "difyctl_get_file", "difyctl_refresh_catalog",
                     "difyctl_read", "difyctl_write", "difyctl_destructive", "difyctl_put_file", "difyctl_use_workspace",
                     "difyctl_logout"}
    annotations = {t["name"]: t["annotations"] for t in tools}
    assert annotations["difyctl_read"]["readOnlyHint"] is True
    assert annotations["difyctl_write"]["readOnlyHint"] is False and annotations["difyctl_write"]["destructiveHint"] is False
    assert annotations["difyctl_destructive"]["destructiveHint"] is True
    result = live.call(API_KEY, "difyctl_read", {"operation": "get.console_app"})
    assert result["error"]["code"] == "oauth_required"
    assert calls(live.settings) == [c for c in calls(live.settings) if c["args"][:2] == ["version", "--json"]]


def test_full_authorization_code_flow(live):
    client, tokens = live.tokens()
    assert tokens["token_type"] == "Bearer" and 3500 <= tokens["expires_in"] <= 3600
    assert tokens["scope"] == "difyctl:read difyctl:write difyctl:destructive"
    assert len(tokens["access_token"]) >= 43
    status = live.call(tokens["access_token"], "difyctl_status")
    assert status["result"]["account"] == {"account_id": "acc-1", "email": "alice@example.com"}
    assert 9 * 86400 < status["result"]["session_expires_in_seconds"] <= 10 * 86400
    login = [c for c in calls(live.settings) if c["args"][:1] == ["login"] and "--server" in c["args"]][0]
    assert login["args"] == ["login", "--server", "https://cloud.dify.example", "--no-browser", "--no-wait", "--no-keyring", "--json"]
    # Dify tokens (dfoa_) never reach the client, and are stored hashed nowhere in plaintext form.
    database = (live.settings.data_dir / "auth.db").read_bytes()
    assert tokens["access_token"].encode() not in database and tokens["refresh_token"].encode() not in database
    assert client["client_secret"].encode() not in database


def test_redirect_and_pkce_are_enforced(live):
    assert live.register("https://evil.example/callback").status_code == 400
    loopback = live.register("http://localhost:43123/callback")
    assert loopback.status_code == 201
    client = live.register().json()
    response, _ = live.authorize(client, redirect="https://evil.example/callback")
    assert response.status_code == 400
    response, _ = live.authorize(client, resource="https://other.example/mcp")
    assert response.status_code == 302 and "invalid_target" in response.headers["location"]
    done, verifier, _, _ = live.approve(client)
    code = parse_qs(urlsplit(done.headers["location"]).query)["code"][0]
    assert parse_qs(urlsplit(done.headers["location"]).query)["state"] == ["st-1"]
    bad = live.exchange(client, code, "x" * 50)
    assert bad.status_code == 400 and bad.json()["error"] == "invalid_grant"
    # The code was consumed by the failed attempt and cannot be replayed with the right verifier.
    assert live.exchange(client, code, verifier).json()["error"] == "invalid_grant"
    wrong_secret = live.http.post("/token", data={"grant_type": "authorization_code", "code": "x", "redirect_uri": REDIRECT,
                                                  "client_id": client["client_id"], "client_secret": "wrong", "code_verifier": verifier})
    assert wrong_secret.status_code == 401


def test_authorization_code_is_single_use(live):
    client = live.register().json()
    done, verifier, _, _ = live.approve(client)
    code = parse_qs(urlsplit(done.headers["location"]).query)["code"][0]
    assert live.exchange(client, code, verifier).status_code == 200
    second = live.exchange(client, code, verifier)
    assert second.status_code == 400 and second.json()["error"] == "invalid_grant"


def test_email_outside_allowlist_is_refused_and_dify_session_ended(live):
    client = live.register().json()
    done, _, _, _ = live.approve(client, email="mallory@example.com", account_id="acc-9")
    assert done.status_code == 403 and "mallory@example.com" in done.text
    logouts = (live.settings.home_dir / "logouts").read_text().split()
    assert len(logouts) == 1 and not Path(logouts[0]).exists()
    assert list(live.settings.sessions_dir.iterdir()) == []
    assert live.settings.data_dir.joinpath("auth.db").exists()


def test_other_browser_cannot_continue(live):
    client = live.register().json()
    response, _ = live.authorize(client)
    path = live.login_path(response)
    assert live.http.get(path).status_code == 403
    assert live.http.get(path, headers={"Cookie": "difyctl_login=forged"}).status_code == 403
    page = live.http.get(path, headers={"Cookie": "difyctl_login=" + live.cookie(response)})
    assert page.status_code == 200
    # The cookie is bound to this login path and protected from scripts.
    assert "HttpOnly" in response.headers["set-cookie"] and "Path=/difyctl-mcp/login/" in response.headers["set-cookie"]
    assert "SameSite=lax" in response.headers["set-cookie"]
    # CSRF: a cross-site form without the token cannot start the Dify login.
    forged = live.http.post(path + "/start", data={"csrf": "0" * 64}, headers={"Cookie": "difyctl_login=" + live.cookie(response)})
    assert forged.status_code == 403


def test_untrusted_verification_page_is_not_shown(live):
    control(live.settings, verification_uri="https://phish.example/device")
    client = live.register().json()
    response, _ = live.authorize(client)
    path, headers = live.login_path(response), {"Cookie": "difyctl_login=" + live.cookie(response)}
    import re
    csrf = re.search(r'name="csrf" value="([0-9a-f]+)"', live.http.get(path, headers=headers).text).group(1)
    live.http.post(path + "/start", data={"csrf": csrf}, headers=headers)
    page = live.http.get(path, headers=headers)
    assert page.status_code == 400 and "phish.example" not in page.text


def test_refresh_rotates_and_reuse_revokes_the_client(live):
    client, tokens = live.tokens()
    refresh = {"grant_type": "refresh_token", "client_id": client["client_id"], "client_secret": client["client_secret"]}
    first = live.http.post("/token", data={**refresh, "refresh_token": tokens["refresh_token"]})
    assert first.status_code == 200
    rotated = first.json()
    assert rotated["refresh_token"] != tokens["refresh_token"]
    # The old access token stops working once its refresh token rotates.
    assert live.rpc(tokens["access_token"], "tools/list").status_code == 401
    assert live.rpc(rotated["access_token"], "tools/list").status_code == 200
    reuse = live.http.post("/token", data={**refresh, "refresh_token": tokens["refresh_token"]})
    assert reuse.status_code == 400 and reuse.json()["error"] == "invalid_grant"
    assert live.rpc(rotated["access_token"], "tools/list").status_code == 401
    assert live.http.post("/token", data={**refresh, "refresh_token": rotated["refresh_token"]}).status_code == 400


def test_expired_dify_login_blocks_refresh_until_reauthorized(live):
    client, tokens = live.tokens()
    store = live.server.config.app.app.state.login.store
    store._run("UPDATE users SET session_expires_at = ?", (int(time.time()) - 1,))
    assert live.rpc(tokens["access_token"], "tools/list").status_code == 401
    refresh = live.http.post("/token", data={"grant_type": "refresh_token", "client_id": client["client_id"],
                                             "client_secret": client["client_secret"], "refresh_token": tokens["refresh_token"]})
    assert refresh.status_code == 400 and refresh.json()["error"] == "invalid_grant"
    control(live.settings, approve=False)
    _, again = live.tokens(client)
    assert live.rpc(again["access_token"], "tools/list").status_code == 200


def test_new_login_replaces_the_previous_config_directory(live):
    _, first = live.tokens()
    store = live.server.config.app.app.state.login.store
    old = store.user("acc-1")["config_dir"]
    control(live.settings, approve=False)
    _, second = live.tokens()
    new = store.user("acc-1")["config_dir"]
    assert new != old and not Path(old).exists() and Path(new).is_dir()
    # Both clients share the newest Dify login.
    assert live.call(first["access_token"], "difyctl_status")["outcome"] == "success"


def test_access_token_lifetime_is_capped_by_the_dify_login(live):
    control(live.settings, session_expires_at=int(time.time()) + 600)
    _, tokens = live.tokens()
    assert tokens["expires_in"] <= 600


def test_revocation_endpoint(live):
    client, tokens = live.tokens()
    response = live.http.post("/revoke", data={"token": tokens["access_token"], "client_id": client["client_id"],
                                               "client_secret": client["client_secret"]})
    assert response.status_code == 200
    assert live.rpc(tokens["access_token"], "tools/list").status_code == 401


def test_rate_limit_on_registration(live):
    statuses = [live.register().status_code for _ in range(21)]
    assert statuses[-1] == 429 and statuses[0] == 201
    assert live.http.post("/register", json={}, headers={"X-Real-IP": "203.0.113.9"}).status_code == 400


def test_health_reports_configuration(live):
    health = live.http.get("/health").json()
    assert health == {"ok": True, "service": "difyctl-mcp", "configured": True, "difyctl": "1.18.0"}
    assert "client_secret" not in json.dumps(health)
