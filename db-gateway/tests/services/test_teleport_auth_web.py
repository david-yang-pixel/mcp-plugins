import asyncio
from unittest.mock import AsyncMock, patch

import pytest


def test_render_form_includes_token_and_user():
    from db_gateway.teleport_auth_web import render_form

    html = render_form(token="tok-123", user="yang")

    assert 'name="t" value="tok-123"' in html
    assert "用户：yang" in html


def test_render_form_uses_fallback_user_label():
    from db_gateway.teleport_auth_web import render_form

    html = render_form(token="tok-123", user="")

    assert "用户：(未配置)" in html


def test_render_form_includes_public_server_label():
    from db_gateway.teleport_auth_web import render_form

    html = render_form(
        token="tok-123",
        user="yang",
        public_base_url="https://agent.example.com",
    )

    assert "服务器：agent.example.com" in html


def test_render_result_success_mentions_recovery_window():
    from db_gateway.teleport_auth_web import render_result

    html = render_result(ok=True, message="证书已续期")

    assert "认证成功" in html
    assert "证书已续期" in html
    assert "30 秒内自动恢复数据库连接" in html


def test_render_result_failure_includes_detail():
    from db_gateway.teleport_auth_web import render_result

    html = render_result(ok=False, message="tsh login 失败", detail="bad otp")

    assert "认证失败" in html
    assert "tsh login 失败" in html
    assert "<pre>bad otp</pre>" in html


@pytest.mark.asyncio
async def test_run_tsh_login_delegates_to_shared_runner():
    from db_gateway.teleport_auth_web import _run_tsh_login

    runner = AsyncMock(return_value=(True, "ok"))
    with patch("db_gateway.teleport_login.run_tsh_login", runner):
        result = await _run_tsh_login(
            proxy="teleport.example:443",
            user="yang",
            password="secret",
            otp="123456",
            auth_home="/tmp/tsh",
            timeout_s=12,
        )

    assert result == (True, "ok")
    runner.assert_awaited_once_with(
        proxy="teleport.example:443",
        user="yang",
        password="secret",
        otp="123456",
        auth_home="/tmp/tsh",
        timeout_s=12,
    )


# --- handlers: a token renews the session it was minted for ------------------------------

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from db_gateway.app import create_db_gateway_app  # noqa: E402
from db_gateway.app_context import TELEPORT_AUTH_TOKEN_STORE_KEY  # noqa: E402
from db_gateway.config import AppConfig  # noqa: E402
from db_gateway.teleport_tokens import SCOPE_HOST, SCOPE_SESSION  # noqa: E402


@pytest.fixture
async def web_app(tmp_path, monkeypatch):
    monkeypatch.setenv("INTERNAL_SHARED_SECRET", "internal-secret")
    cfg = AppConfig(
        db_gateway={"api_key": "secret"},
        teleport={"proxy": "teleport.example:443", "user": "yang", "auth_home": str(tmp_path / "host"),
                  "sessions_dir": str(tmp_path / "sessions"), "public_base_url": "https://agent.example.com"},
    )
    app = create_db_gateway_app(cfg)
    async with TestClient(TestServer(app)) as client:
        yield app, client


async def _token(app, scope):
    return await app[TELEPORT_AUTH_TOKEN_STORE_KEY].create(scope)


async def test_internal_issue_mints_a_host_session_link(web_app):
    app, client = web_app
    resp = await client.post("/internal/teleport-auth/issue", headers={"X-Internal-Secret": "internal-secret"})
    url = (await resp.json())["url"]
    assert resp.status == 200 and url.startswith("https://agent.example.com/teleport-auth?t=")
    entry = await app[TELEPORT_AUTH_TOKEN_STORE_KEY].lookup(url.split("t=", 1)[1])
    assert entry.scope == SCOPE_HOST

    bad = await client.post("/internal/teleport-auth/issue", headers={"X-Internal-Secret": "wrong"})
    assert bad.status == 401


async def test_form_names_the_session_it_renews(web_app):
    app, client = web_app
    own = await (await client.get(f"/teleport-auth?t={await _token(app, SCOPE_SESSION)}")).text()
    host = await (await client.get(f"/teleport-auth?t={await _token(app, SCOPE_HOST)}")).text()
    assert "续期对象：db-gateway 自管会话" in own
    assert "续期对象：宿主机 tsh 会话" in host


async def test_session_token_renews_db_gateways_own_session(web_app):
    app, client = web_app
    tok = await _token(app, SCOPE_SESSION)
    renew, legacy = AsyncMock(), AsyncMock()
    with patch("db_gateway.teleport_session.SESSION.renew", renew), \
            patch("db_gateway.teleport_login.run_tsh_login", legacy):
        resp = await client.post("/teleport-auth", data={"t": tok, "password": " pw ", "otp": "123456"})
    html = await resp.text()
    assert "认证成功" in html and "自动续期已恢复" in html
    assert renew.await_args.kwargs == {"reason": "manual_web", "password": "pw", "otp": "123456"}
    assert legacy.await_count == 0
    assert await app[TELEPORT_AUTH_TOKEN_STORE_KEY].lookup(tok) is None     # consumed


async def test_host_token_keeps_renewing_the_host_session(web_app, tmp_path):
    app, client = web_app
    tok = await _token(app, SCOPE_HOST)
    renew, legacy = AsyncMock(), AsyncMock(return_value=(True, "ok"))
    with patch("db_gateway.teleport_session.SESSION.renew", renew), \
            patch("db_gateway.teleport_login.run_tsh_login", legacy):
        resp = await client.post("/teleport-auth", data={"t": tok, "password": "pw", "otp": "123456"})
    assert "认证成功" in await resp.text()
    assert legacy.await_args.kwargs["auth_home"] == str(tmp_path / "host")
    assert renew.await_count == 0


async def test_wrong_code_keeps_the_token_for_a_retry(web_app):
    from db_gateway.teleport_session import LoginRejected

    app, client = web_app
    tok = await _token(app, SCOPE_SESSION)
    with patch("db_gateway.teleport_session.SESSION.renew",
               AsyncMock(side_effect=LoginRejected("密码或 OTP 错误：invalid credentials", "auth"))):
        resp = await client.post("/teleport-auth", data={"t": tok, "password": "pw", "otp": "000000"})
    html = await resp.text()
    assert "认证失败" in html and "密码或 OTP 错误" in html and "pw" not in html.split("<body>")[1]
    assert await app[TELEPORT_AUTH_TOKEN_STORE_KEY].lookup(tok) is not None


async def test_locked_account_gets_a_do_not_retry_hint(web_app):
    from db_gateway.teleport_session import LoginRejected

    app, client = web_app
    tok = await _token(app, SCOPE_SESSION)
    with patch("db_gateway.teleport_session.SESSION.renew",
               AsyncMock(side_effect=LoginRejected("Teleport 账号被锁定", "locked"))):
        resp = await client.post("/teleport-auth", data={"t": tok, "password": "pw", "otp": "123456"})
    assert "账号已被锁定" in await resp.text()


def test_failure_detail_is_html_escaped():
    from db_gateway.teleport_auth_web import render_result

    assert "<script>" not in render_result(ok=False, message="x", detail="<script>alert(1)</script>")


async def test_a_second_submit_while_one_login_runs_is_not_sent_to_teleport(web_app):
    app, client = web_app
    tok = await _token(app, SCOPE_SESSION)
    started, release = asyncio.Event(), asyncio.Event()

    async def slow_renew(*args, **kwargs):
        started.set()
        await release.wait()

    renew = AsyncMock(side_effect=slow_renew)
    with patch("db_gateway.teleport_session.SESSION.renew", renew):
        first = asyncio.ensure_future(client.post("/teleport-auth", data={"t": tok, "password": "pw", "otp": "1"}))
        await asyncio.wait_for(started.wait(), 5)
        try:
            second = await asyncio.wait_for(
                client.post("/teleport-auth", data={"t": tok, "password": "pw", "otp": "1"}), 5)
            assert "正在进行" in await second.text()
        finally:
            release.set()
        assert "认证成功" in await (await first).text()
    assert renew.await_count == 1


async def test_a_link_stops_working_after_three_failed_logins(web_app):
    from db_gateway.teleport_session import LoginRejected

    app, client = web_app
    tok = await _token(app, SCOPE_SESSION)
    renew = AsyncMock(side_effect=LoginRejected("密码或 OTP 错误：invalid credentials", "auth"))
    with patch("db_gateway.teleport_session.SESSION.renew", renew):
        for _ in range(3):
            await client.post("/teleport-auth", data={"t": tok, "password": "pw", "otp": "000000"})
        last = await client.post("/teleport-auth", data={"t": tok, "password": "pw", "otp": "000000"})
    assert renew.await_count == 3
    assert "Token 无效或已过期" in await last.text()
