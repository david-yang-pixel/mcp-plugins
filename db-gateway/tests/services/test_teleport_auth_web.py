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
