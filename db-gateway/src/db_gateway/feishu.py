"""Minimal Feishu custom-bot client for DB Gateway auth alerts."""

from __future__ import annotations

import base64
import hashlib
import hmac
import time

import httpx


class FeishuClient:
    def __init__(self, *, webhook_url: str, webhook_secret: str = "") -> None:
        self.webhook_url = webhook_url
        self.webhook_secret = webhook_secret

    def _signature(self, timestamp: int) -> str:
        string_to_sign = f"{timestamp}\n{self.webhook_secret}"
        digest = hmac.new(
            string_to_sign.encode("utf-8"),
            b"",
            hashlib.sha256,
        ).digest()
        return base64.b64encode(digest).decode("utf-8")

    async def _post(self, payload: dict) -> None:
        if not self.webhook_url:
            return
        if self.webhook_secret:
            ts = int(time.time())
            payload = {
                **payload,
                "timestamp": str(ts),
                "sign": self._signature(ts),
            }
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(self.webhook_url, json=payload)
        resp.raise_for_status()

    async def send_alert(self, *, title: str, message: str, urgent: bool = False) -> None:
        prefix = "🚨 " if urgent else ""
        await self._post({
            "msg_type": "text",
            "content": {"text": f"{prefix}{title}\n\n{message}"},
        })

    async def send_auth_renew(self, *, url: str, user: str) -> None:
        await self._post({
            "msg_type": "text",
            "content": {
                "text": (
                    "🔐 Teleport 证书需要续期\n\n"
                    f"user: {user or '(未配置)'}\n"
                    f"认证链接: {url}"
                )
            },
        })

