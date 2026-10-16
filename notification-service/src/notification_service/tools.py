from __future__ import annotations

import base64
import hashlib
import hmac
import time
from typing import Any

import httpx

from .mcp import failure, optional_str, required_str, success
from .settings import Settings

TOOLS = [
    {
        "name": "feishu_push",
        "description": "Push a guarded Feishu notification card.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "card_type": {"type": "string", "enum": ["ticket_done", "ticket_escalate", "alert"]},
                "ticket_id": {"type": "string"},
                "content": {"type": "string"},
                "title": {"type": "string"},
                "zendesk_url": {"type": "string"},
                "urgent": {"type": "boolean"},
            },
            "required": ["card_type", "content"],
        },
    },
]


async def handle(name: str, arguments: dict[str, Any], settings: Settings) -> dict[str, Any]:
    try:
        if name != "feishu_push":
            return failure("UNKNOWN_TOOL", f"unknown tool: {name}")
        if not settings.feishu_webhook_url:
            return failure("FEISHU_WEBHOOK_URL_MISSING", "FEISHU_WEBHOOK_URL is not configured")

        card_type = required_str(arguments, "card_type")
        content = required_str(arguments, "content")
        title = optional_str(arguments, "title", "Alert") or "Alert"
        ticket_id = optional_str(arguments, "ticket_id")
        zendesk_url = optional_str(arguments, "zendesk_url")
        urgent = bool(arguments.get("urgent", False))
        if card_type not in {"ticket_done", "ticket_escalate", "alert"}:
            return failure("UNKNOWN_CARD_TYPE", f"unknown card_type: {card_type}")

        payload = _card_payload(
            card_type=card_type,
            ticket_id=ticket_id,
            title=title,
            content=content,
            zendesk_url=zendesk_url,
            urgent=urgent,
            settings=settings,
        )
        await _send(payload, settings)
        return success(output=f"pushed ({card_type})")
    except ValueError as e:
        return failure("BAD_ARGUMENT", str(e))
    except httpx.HTTPStatusError as e:
        return failure("FEISHU_HTTP_ERROR", f"HTTP {e.response.status_code}: {e.response.text[:500]}")
    except RuntimeError as e:
        return failure("FEISHU_API_ERROR", str(e))
    except Exception as e:  # noqa: BLE001
        return failure("INTERNAL_ERROR", str(e))


def _card_payload(
    *,
    card_type: str,
    ticket_id: str,
    title: str,
    content: str,
    zendesk_url: str,
    urgent: bool,
    settings: Settings,
) -> dict[str, Any]:
    display_title = title
    if card_type == "ticket_done":
        display_title = f"Ticket #{ticket_id} 处理完成" if ticket_id else "工单处理完成"
    elif card_type == "ticket_escalate":
        display_title = f"Ticket #{ticket_id} 需要人工介入" if ticket_id else "工单需要人工介入"
    elif urgent:
        display_title = f"[URGENT] {title}"

    elements: list[dict[str, Any]] = []
    if urgent:
        elements.append({"tag": "markdown", "content": "<at id=all></at>"})
    elements.append({"tag": "markdown", "content": content})
    if zendesk_url:
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "查看 Zendesk"},
                        "type": "primary",
                        "url": zendesk_url,
                    }
                ],
            }
        )

    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "red" if urgent or card_type == "ticket_escalate" else "blue",
            "title": {"tag": "plain_text", "content": display_title[:80]},
        },
        "elements": elements,
    }
    payload: dict[str, Any] = {"msg_type": "interactive", "card": card}
    if settings.feishu_webhook_secret:
        timestamp = str(int(time.time()))
        payload["timestamp"] = timestamp
        payload["sign"] = _sign(timestamp, settings.feishu_webhook_secret)
    return payload


def _sign(timestamp: str, secret: str) -> str:
    string_to_sign = f"{timestamp}\n{secret}".encode("utf-8")
    digest = hmac.new(string_to_sign, b"", digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


async def _send(payload: dict[str, Any], settings: Settings) -> None:
    retry_delays = (1.0, 3.0, 8.0)
    last_error = ""
    for attempt in range(len(retry_delays) + 1):
        try:
            async with httpx.AsyncClient(timeout=settings.notification_timeout_seconds, trust_env=False) as client:
                resp = await client.post(settings.feishu_webhook_url, json=payload)
                resp.raise_for_status()
                data = resp.json()
            if data.get("code", 0) == 0:
                return
            last_error = f"Feishu code={data.get('code')}: {data.get('msg') or data}"
            if data.get("code") != 11232:
                break
        except httpx.HTTPStatusError as e:
            last_error = f"HTTP {e.response.status_code}: {e.response.text[:300]}"
            if e.response.status_code not in {429, 500, 502, 503, 504}:
                raise
        except httpx.HTTPError as e:
            last_error = str(e)
        if attempt < len(retry_delays):
            await _sleep(retry_delays[attempt])
    raise RuntimeError(last_error or "Feishu push failed")


async def _sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)
