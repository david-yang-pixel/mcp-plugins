"""Service token for clients without OAuth (openconnector), bound to one Dify account, plus login renewal."""
import re
import time
from pathlib import Path

import pytest

from conftest import calls, control
from difyctl_mcp.settings import Settings

SERVICE = "svc-" + "s" * 40


@pytest.fixture
def service(live):
    live.settings.service_token = SERVICE
    live.settings.service_email = "alice@example.com"
    live.settings.service_scope = "destructive"
    return live


def store(live):
    return live.server.config.app.state.login.store


def renew(live):
    """Drive /login/renew to a completed Dify approval; return the final page."""
    response = live.http.get("/login/renew")
    assert response.status_code == 303 and "/difyctl-mcp/login?request=" in response.headers["location"]
    path, headers = live.login_path(response), {"Cookie": live.cookie(response)}
    page = live.http.get(path, headers=headers)
    assert "续期 difyctl MCP 保存的 Dify 登录" in page.text and "alice@example.com" in page.text
    csrf = re.search(r'name="csrf" value="([0-9a-f]+)"', page.text).group(1)
    assert live.start(path, csrf, headers).status_code == 303
    assert "ABCD-1234" in live.http.get(path, headers=headers).text
    control(live.settings, approve=True, email="alice@example.com", account_id="acc-1")
    return live.http.get(path, headers=headers)


def test_service_token_before_any_login_points_to_renewal(service):
    assert service.rpc(SERVICE, "tools/list").status_code == 200
    result = service.call(SERVICE, "difyctl_read", {"operation": "get.console_app"})
    assert result["error"]["code"] == "dify_login_required"
    assert result["renew_url"] == service.settings.public_url + "/login/renew"
    assert "alice@example.com" in result["next_step"]
    assert not [c for c in calls(service.settings) if c["args"][:2] == ["get", "console_app"]]


def test_service_token_acts_as_the_bound_account_with_its_scope(service):
    service.tokens()  # the account's first Dify login, through any OAuth client
    result = service.call(SERVICE, "difyctl_read", {"operation": "get.console_app"})
    assert result["outcome"] == "success"
    assert service.call(SERVICE, "difyctl_destructive", {"operation": "delete.plugin", "input": {"plugin_id": "p"}})["outcome"] == "success"
    status = service.call(SERVICE, "difyctl_status")["result"]
    assert status["account"]["email"] == "alice@example.com" and status["client_id"] == "service-token"
    assert status["scopes"] == ["difyctl:read", "difyctl:write", "difyctl:destructive"]
    service.settings.service_scope = "read"
    assert service.call(SERVICE, "difyctl_write", {"operation": "create.console_app.workflow"})["error"]["code"] == "insufficient_scope"
    # The token itself never reaches the database and cannot be revoked like an OAuth token.
    assert SERVICE.encode() not in (service.settings.data_dir / "auth.db").read_bytes()


def test_expired_dify_login_then_renewal_restores_the_same_token(service):
    _, tokens = service.tokens()
    old_dir = store(service).user("acc-1")["config_dir"]
    expired = service.call(SERVICE, "difyctl_read", {"operation": "get.console_app", "input": {"fail": "auth_expired"}})
    assert expired["error"]["code"] == "auth_expired"
    # From now on the token explains how to renew instead of running difyctl again.
    before = len(calls(service.settings))
    again = service.call(SERVICE, "difyctl_read", {"operation": "get.console_app"})
    assert again["error"]["code"] == "dify_login_required" and len(calls(service.settings)) == before
    assert service.rpc(tokens["access_token"], "tools/list").status_code == 401
    control(service.settings, approve=False)
    done = renew(service)
    assert done.status_code == 200 and "续期完成" in done.text and "location" not in done.headers
    new_dir = store(service).user("acc-1")["config_dir"]
    assert new_dir != old_dir and not Path(old_dir).exists()
    assert store(service)._one("SELECT COUNT(*) FROM auth_codes")[0] == 0  # nothing was issued to any client
    assert service.call(SERVICE, "difyctl_read", {"operation": "get.console_app"})["outcome"] == "success"


def test_renewal_for_another_allowed_account_does_not_touch_the_service_account(service):
    service.settings.allowed_emails.append("bob@example.com")
    service.tokens()
    alice_dir = store(service).user("acc-1")["config_dir"]
    control(service.settings, approve=False)
    response = service.http.get("/login/renew")
    path, headers = service.login_path(response), {"Cookie": service.cookie(response)}
    csrf = re.search(r'name="csrf" value="([0-9a-f]+)"', service.http.get(path, headers=headers).text).group(1)
    service.start(path, csrf, headers)
    service.http.get(path, headers=headers)
    control(service.settings, approve=True, email="bob@example.com", account_id="acc-2")
    done = service.http.get(path, headers=headers)
    assert "bob@example.com" in done.text and "服务令牌的登录没有变化" in done.text
    assert store(service).user("acc-1")["config_dir"] == alice_dir


def test_renewal_still_enforces_the_email_allowlist_and_browser_binding(service):
    response = service.http.get("/login/renew")
    path = service.login_path(response)
    assert service.http.get(path).status_code == 403
    headers = {"Cookie": service.cookie(response)}
    csrf = re.search(r'name="csrf" value="([0-9a-f]+)"', service.http.get(path, headers=headers).text).group(1)
    service.start(path, csrf, headers)
    service.http.get(path, headers=headers)
    control(service.settings, approve=True, email="mallory@example.com", account_id="acc-9")
    assert service.http.get(path, headers=headers).status_code == 403
    assert store(service).user("acc-9") is None


def test_tool_results_warn_before_the_login_expires(service):
    control(service.settings, session_expires_at=int(time.time()) + 2 * 86400)
    service.tokens()
    result = service.call(SERVICE, "difyctl_read", {"operation": "get.console_app"})
    assert any("/login/renew" in n and "expires in" in n for n in result["notices"])


def test_service_token_settings_are_validated():
    base = {"MCP_PUBLIC_URL": "https://mcp.example/difyctl-mcp", "MCP_ALLOWED_EMAILS": "a@x.com",
            "MCP_ALLOWED_REDIRECT_URIS": "https://c.example/cb", "DIFYCTL_MCP_API_KEY": "k" * 40}
    for extra in ({"DIFYCTL_MCP_SERVICE_TOKEN": "t" * 40},
                  {"DIFYCTL_MCP_SERVICE_ACCOUNT_EMAIL": "a@x.com"},
                  {"DIFYCTL_MCP_SERVICE_TOKEN": "short", "DIFYCTL_MCP_SERVICE_ACCOUNT_EMAIL": "a@x.com"},
                  {"DIFYCTL_MCP_SERVICE_TOKEN": "k" * 40, "DIFYCTL_MCP_SERVICE_ACCOUNT_EMAIL": "a@x.com"},
                  {"DIFYCTL_MCP_SERVICE_TOKEN": "t" * 40, "DIFYCTL_MCP_SERVICE_ACCOUNT_EMAIL": "a@x.com",
                   "DIFYCTL_MCP_SERVICE_SCOPE": "admin"}):
        with pytest.raises(ValueError):
            Settings.from_env({**base, **extra})
    settings = Settings.from_env({**base, "DIFYCTL_MCP_SERVICE_TOKEN": "t" * 40,
                                  "DIFYCTL_MCP_SERVICE_ACCOUNT_EMAIL": "A@X.com", "DIFYCTL_MAX_EFFECT": "write"})
    assert settings.service_email == "a@x.com" and settings.service_scopes == ["difyctl:read", "difyctl:write"]
