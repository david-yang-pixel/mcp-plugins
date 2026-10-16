from __future__ import annotations

import re

CUSTOMER_REPLY_FOOTER = (
    "This reply was sent by Dify AI Agent. If this does not resolve your issue, "
    "reply \u201chuman\u201d. A human support engineer will respond as soon as possible "
    "during support hours (New York time): Sunday\u2013Thursday nights, "
    "9:00 PM\u20136:00 AM EDT (daylight saving time) or "
    "8:00 PM\u20135:00 AM EST (standard time)."
)

CUSTOMER_REPLY_FOOTER_ZH = (
    '此回复由 Dify AI Agent 发送。如果问题仍未解决，请回复“human”。'
    '人工支持会在服务时间内尽快回复（纽约时间：周日至周四晚间，'
    '夏令时 21:00 至次日 6:00，标准时间 20:00 至次日 5:00）。'
)
CUSTOMER_REPLY_FOOTER_JA = (
    'この返信は Dify AI Agent が送信しました。問題が解決しない場合は「human」と返信してください。'
    'サポート担当者が営業時間内に順次対応します（ニューヨーク時間：日曜～木曜、'
    '夏時間 21:00～翌6:00、標準時間 20:00～翌5:00）。'
)

_FOOTER_RE = re.compile(
    r'^[ \t]*(?:This reply was sent by Dify AI Agent\.|'
    r'此回复由 Dify AI Agent 发送。|この返信は Dify AI Agent が送信しました。)',
    re.MULTILINE,
)
_LIST_ITEM_RE = re.compile(r"^(?:[-*•]|\d+[.)])\s+")
_SIGN_OFF_RE = re.compile(
    r"^(?:best|kind|warm) regards,?$|^regards,?$|^sincerely,?$",
    re.IGNORECASE,
)


def _normalize_email_paragraphs(body: str) -> str:
    """Turn model line breaks into stable email paragraphs.

    Zendesk renders a lone newline as a compact line break. Treat ordinary
    model lines as separate paragraphs, while keeping consecutive list items
    and the sign-off/name pair together.
    """

    lines = [line.strip() for line in body.split("\n")]
    paragraphs: list[str] = []
    index = 0

    while index < len(lines):
        line = lines[index]
        if not line:
            index += 1
            continue

        if _LIST_ITEM_RE.match(line):
            items = [line]
            index += 1
            while index < len(lines):
                candidate = lines[index]
                if not candidate:
                    index += 1
                    continue
                if not _LIST_ITEM_RE.match(candidate):
                    break
                items.append(candidate)
                index += 1
            paragraphs.append("\n".join(items))
            continue

        if _SIGN_OFF_RE.match(line):
            sign_off = [line]
            index += 1
            while index < len(lines) and not lines[index]:
                index += 1
            if index < len(lines):
                sign_off.append(lines[index])
                index += 1
            paragraphs.append("\n".join(sign_off))
            continue

        paragraphs.append(line)
        index += 1

    return "\n\n".join(paragraphs)


def format_customer_reply(body: str) -> str:
    """Normalize a customer email and append the required footer exactly once."""

    normalized = body.replace("\\r\\n", "\n").replace("\\n", "\n")
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n").strip()

    # Pi Agent may already supply a localized footer. Strip the entire footer
    # suffix (including duplicates) before detecting the body's language.
    footer_match = _FOOTER_RE.search(normalized)
    if footer_match:
        normalized = normalized[:footer_match.start()].rstrip()
        normalized = re.sub(r'(?:\n[ \t]*---[ \t]*)+\s*$', '', normalized).rstrip()

    normalized = re.sub(r"[ \t]+\n", "\n", normalized)
    normalized = _normalize_email_paragraphs(normalized)
    if re.search(r'[\u3040-\u30ff]', normalized):
        footer = CUSTOMER_REPLY_FOOTER_JA
    elif re.search(r'[\u3400-\u9fff]', normalized):
        footer = CUSTOMER_REPLY_FOOTER_ZH
    else:
        footer = CUSTOMER_REPLY_FOOTER
    return f"{normalized}\n\n{footer}"
