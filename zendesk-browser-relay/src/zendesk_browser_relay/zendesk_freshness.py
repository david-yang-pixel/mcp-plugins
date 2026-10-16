from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen


class ZendeskMcpFreshnessChecker:
    """Read the canonical Messaging cursor immediately before Relay send."""

    _PAGE_SIZE = 100
    _MAX_PAGES = 10
    _MAX_EVENTS = 1_000

    def __init__(
        self,
        *,
        endpoint_url: str,
        authorization: str,
        timeout_seconds: float = 12,
    ) -> None:
        endpoint_url = endpoint_url.rstrip("/")
        if not endpoint_url.endswith("/mcp"):
            endpoint_url += "/mcp"
        parsed = urlparse(endpoint_url)
        is_loopback_http = parsed.scheme == "http" and parsed.hostname in {
            "127.0.0.1",
            "localhost",
            "::1",
        }
        if parsed.scheme != "https" and not is_loopback_http:
            raise ValueError("Zendesk MCP URL must use HTTPS or loopback HTTP")
        if len(authorization) < 16:
            raise ValueError("Zendesk MCP authorization is too short")
        self.endpoint_url = endpoint_url
        self.authorization = authorization
        self.timeout_seconds = timeout_seconds

    async def current_customer_cursor(self, ticket_id: int) -> tuple[str, int]:
        events: list[dict[str, Any]] = []
        seen_event_ids: set[str] = set()
        seen_cursors: set[str] = set()
        after_cursor = ""

        for page_number in range(self._MAX_PAGES):
            arguments: dict[str, Any] = {
                "ticket_id": ticket_id,
                "conversation_page_size": self._PAGE_SIZE,
                "force_refresh": True,
            }
            if after_cursor:
                arguments["conversation_after"] = after_cursor
            payload = await asyncio.to_thread(
                self._call_tool,
                "get_ticket_conversation_log",
                arguments,
            )
            page_events, meta = self._page_parts(payload)
            for event in page_events:
                event_id = str(event.get("id") or "")
                dedupe_key = event_id or json.dumps(event, sort_keys=True, default=str)
                if dedupe_key in seen_event_ids:
                    continue
                seen_event_ids.add(dedupe_key)
                events.append(event)
            if len(events) > self._MAX_EVENTS:
                raise RuntimeError("conversation event safety cap exceeded")
            if not bool(meta.get("has_more")):
                break
            next_cursor = str(meta.get("after_cursor") or "").strip()
            if not next_cursor:
                raise RuntimeError("conversation has_more without after_cursor")
            if next_cursor in seen_cursors:
                raise RuntimeError("conversation after_cursor repeated")
            seen_cursors.add(next_cursor)
            after_cursor = next_cursor
        else:
            raise RuntimeError("conversation page safety cap exceeded")

        customer_events = [
            event
            for event in events
            if isinstance(event.get("author"), dict)
            and event["author"].get("type") == "end-user"
            and str(event.get("id") or "")
        ]
        customer_events.sort(
            key=lambda event: (
                str(event.get("created_at") or event.get("timestamp") or ""),
                str(event.get("id") or ""),
            )
        )
        if not customer_events:
            return "", 0
        return str(customer_events[-1]["id"]), len(customer_events)

    def _call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
            separators=(",", ":"),
        ).encode("utf-8")
        request = Request(
            self.endpoint_url,
            data=body,
            headers={
                "Authorization": self.authorization,
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urlopen(request, timeout=self.timeout_seconds) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise RuntimeError("Zendesk MCP response is too large")
        envelope = self._parse_envelope(raw)
        if "error" in envelope:
            raise RuntimeError("Zendesk MCP returned a JSON-RPC error")
        result = envelope.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("Zendesk MCP returned an invalid result")
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            payload = structured
        else:
            content = result.get("content")
            if not isinstance(content, list) or not content:
                raise RuntimeError("Zendesk MCP returned no content")
            text = content[0].get("text") if isinstance(content[0], dict) else None
            if not isinstance(text, str):
                raise RuntimeError("Zendesk MCP returned invalid content")
            payload = json.loads(text)
        if not isinstance(payload, dict) or payload.get("success") is not True:
            raise RuntimeError("Zendesk MCP tool call failed")
        return payload

    @staticmethod
    def _parse_envelope(raw: bytes) -> dict[str, Any]:
        text = raw.decode("utf-8")
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
            for line in text.splitlines():
                if not line.startswith("data:"):
                    continue
                candidate = line.removeprefix("data:").strip()
                if not candidate or candidate == "[DONE]":
                    continue
                value = json.loads(candidate)
                if isinstance(value, dict):
                    parsed = value
            if parsed is None:
                raise RuntimeError("Zendesk MCP returned invalid JSON or SSE")
        if not isinstance(parsed, dict):
            raise RuntimeError("Zendesk MCP returned a non-object envelope")
        return parsed

    @staticmethod
    def _page_parts(
        payload: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        data = payload.get("data") or {}
        if not isinstance(data, dict):
            raise RuntimeError("Zendesk MCP returned invalid data")
        conversation = data.get("conversation_log") or data
        if not isinstance(conversation, dict):
            raise RuntimeError("Zendesk MCP returned invalid conversation_log")
        nested = conversation.get("conversation_log") or {}
        if not isinstance(nested, dict):
            nested = {}
        raw_events = conversation.get("events")
        if raw_events is None:
            raw_events = nested.get("events") or []
        if not isinstance(raw_events, list):
            raise RuntimeError("Zendesk MCP returned invalid events")
        events = [event for event in raw_events if isinstance(event, dict)]
        meta = conversation.get("meta") or nested.get("meta") or {}
        if not isinstance(meta, dict):
            raise RuntimeError("Zendesk MCP returned invalid conversation meta")
        return events, meta
