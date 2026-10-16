import json

import pytest

from zendesk_browser_relay.feishu_notifications import FeishuStatusNotifier


@pytest.mark.asyncio
async def test_status_notification_posts_success_payload(monkeypatch):
    captured = {}

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _limit):
            return b'{"code":0,"msg":"success"}'

    def fake_urlopen(request, timeout):
        captured["body"] = json.loads(request.data)
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr("zendesk_browser_relay.feishu_notifications.urlopen", fake_urlopen)
    notifier = FeishuStatusNotifier(
        "https://open.feishu.cn/open-apis/bot/v2/hook/" + "a" * 36
    )

    await notifier.notify_send(
        ticket_id=3726,
        status="sent",
        body="Hi,\n\nYour request was received.",
    )

    text = captured["body"]["content"]["text"]
    assert "#3726" in text
    assert "发送成功" in text
    assert "Your request was received." in text
    assert captured["body"]["msg_type"] == "text"


def test_status_notification_rejects_non_feishu_hosts():
    with pytest.raises(ValueError):
        FeishuStatusNotifier("https://example.com/hook")


@pytest.mark.asyncio
async def test_readiness_notification_contains_no_credentials_or_page_url(monkeypatch):
    captured = {}

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _limit):
            return b'{"code":0,"msg":"success"}'

    def fake_urlopen(request, timeout):
        captured["body"] = json.loads(request.data)
        return Response()

    monkeypatch.setattr(
        "zendesk_browser_relay.feishu_notifications.urlopen", fake_urlopen
    )
    notifier = FeishuStatusNotifier(
        "https://open.feishu.cn/open-apis/bot/v2/hook/" + "a" * 36
    )

    await notifier.notify_readiness(status="login_required")

    text = captured["body"]["content"]["text"]
    assert "登录态失效" in text
    assert "停止接单" in text
    assert "password" not in text.casefold()
    assert "http" not in text.casefold()
