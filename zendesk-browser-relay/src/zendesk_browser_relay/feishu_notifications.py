from __future__ import annotations

import asyncio
import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


class FeishuNotificationError(RuntimeError):
    pass


class FeishuStatusNotifier:
    """Best-effort internal Feishu status notifications."""

    def __init__(self, webhook_url: str, timeout_seconds: float = 5.0):
        parsed = urlparse(webhook_url)
        if parsed.scheme != "https" or parsed.hostname != "open.feishu.cn":
            raise ValueError("Feishu webhook must use https://open.feishu.cn")
        self.webhook_url = webhook_url
        self.timeout_seconds = timeout_seconds

    async def notify_send(
        self,
        *,
        ticket_id: int,
        status: str,
        body: str = "",
        error: str = "",
    ) -> None:
        label = "发送成功" if status == "sent" else "发送失败"
        lines = [
            "🔔 Dify 工单自动处理结果",
            "",
            f"工单：#{ticket_id}",
            f"状态：{label}（{status}）",
            f"链接：https://langgenius.zendesk.com/agent/tickets/{ticket_id}",
        ]
        if error:
            lines.append(f"错误：{error[:300]}")
        if status == "sent" and body:
            lines.extend(["", "发送内容：", body[:4_000]])
        await self._post_text("\n".join(lines))

    async def notify_readiness(self, *, status: str) -> None:
        if status == "logged_in":
            lines = [
                "✅ Zendesk Browser Relay 登录态已恢复",
                "",
                "状态：已登录",
                "说明：Relay 已自动恢复；Agent 将按控制面的期望状态恢复接单。",
            ]
        elif status == "login_required":
            lines = [
                "🚨 Zendesk Browser Relay 登录态失效",
                "",
                "状态：需要重新登录",
                "保护动作：Agent readiness 已关闭，已停止接单和派发。",
                "说明：Relay 将使用已配置凭据退避重试；若持续失败，请检查登录凭据、MFA 或安全验证。",
            ]
        else:
            lines = [
                "🚨 Zendesk Browser Relay readiness 检查失败",
                "",
                "状态：无法确认登录态",
                "保护动作：Agent 已停止接单和派发。",
            ]
        await self._post_text("\n".join(lines))

    async def _post_text(self, text: str) -> None:
        body = json.dumps(
            {"msg_type": "text", "content": {"text": text}},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        request = Request(
            self.webhook_url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "Dify-Zendesk-Relay/1.0",
            },
            method="POST",
        )

        def send() -> None:
            try:
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    raw = response.read(4096)
                    if response.status < 200 or response.status >= 300:
                        raise FeishuNotificationError(
                            f"Feishu webhook returned HTTP {response.status}"
                        )
            except (HTTPError, URLError, TimeoutError, OSError) as exc:
                raise FeishuNotificationError("Feishu webhook request failed") from exc
            try:
                payload = json.loads(raw or b"{}")
            except (TypeError, ValueError) as exc:
                raise FeishuNotificationError("Feishu webhook returned invalid JSON") from exc
            if payload.get("code", 0) not in (0, None):
                raise FeishuNotificationError("Feishu webhook rejected the message")

        await asyncio.to_thread(send)
