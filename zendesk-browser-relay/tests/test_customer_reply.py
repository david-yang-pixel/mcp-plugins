import pytest

from zendesk_browser_relay.customer_reply import format_customer_reply


NEW_YORK_FOOTER = (
    "This reply was sent by Dify AI Agent. If this does not resolve your issue, "
    "reply “human”. A human support engineer will respond as soon as possible "
    "during support hours (New York time): Sunday–Thursday nights, "
    "9:00 PM–6:00 AM EDT (daylight saving time) or "
    "8:00 PM–5:00 AM EST (standard time)."
)


def test_appends_new_york_footer_when_missing() -> None:
    formatted = format_customer_reply("Hello! How can I help you today?")

    assert formatted.endswith(NEW_YORK_FOOTER)
    assert "Beijing Time" not in formatted


def test_agent_footer_is_not_replaced_with_beijing_time() -> None:
    formatted = format_customer_reply(f"Hello!\n\n---\n{NEW_YORK_FOOTER}")

    assert formatted.endswith(NEW_YORK_FOOTER)
    assert formatted.count("This reply was sent by Dify AI Agent.") == 1
    assert "Beijing Time" not in formatted


ZH_FOOTER = (
    '此回复由 Dify AI Agent 发送。如果问题仍未解决，请回复“human”。'
    '人工支持会在服务时间内尽快回复（纽约时间：周日至周四晚间，'
    '夏令时 21:00 至次日 6:00，标准时间 20:00 至次日 5:00）。'
)
JA_FOOTER = (
    'この返信は Dify AI Agent が送信しました。問題が解決しない場合は「human」と返信してください。'
    'サポート担当者が営業時間内に順次対応します（ニューヨーク時間：日曜～木曜、'
    '夏時間 21:00～翌6:00、標準時間 20:00～翌5:00）。'
)


@pytest.mark.parametrize(
    ('body', 'footer'),
    [('Hello!', NEW_YORK_FOOTER), ('您好！已核对您的工作空间。', ZH_FOOTER),
     ('こんにちは。お問い合わせありがとうございます。', JA_FOOTER)],
)
@pytest.mark.parametrize('with_footer', [False, True])
def test_footer_matches_reply_language_and_is_idempotent(body: str, footer: str, with_footer: bool) -> None:
    incoming = f'{body}\n\n---\n{footer}' if with_footer else body
    formatted = format_customer_reply(incoming)

    assert formatted == f'{body}\n\n{footer}'
    assert format_customer_reply(formatted) == formatted
    assert formatted.count('Dify AI Agent') == 1


@pytest.mark.parametrize('footer', [NEW_YORK_FOOTER, ZH_FOOTER, JA_FOOTER])
def test_wrong_language_footer_does_not_affect_language_detection(footer: str) -> None:
    assert format_customer_reply(f'Hello!\n\n---\n{footer}') == f'Hello!\n\n{NEW_YORK_FOOTER}'


@pytest.mark.parametrize('footers', [(ZH_FOOTER, NEW_YORK_FOOTER), (NEW_YORK_FOOTER, ZH_FOOTER), (ZH_FOOTER, ZH_FOOTER)])
def test_duplicate_footers_are_replaced_once(footers: tuple[str, str]) -> None:
    incoming = '您好！已核对您的工作空间。\n\n---\n' + '\n\n'.join(footers)
    assert format_customer_reply(incoming) == f'您好！已核对您的工作空间。\n\n{ZH_FOOTER}'


def test_legacy_footer_hours_are_updated() -> None:
    incoming = '您好！\n\n此回复由 Dify AI Agent 发送。人工支持将在北京时间 9:00-18:00 回复。'
    assert format_customer_reply(incoming) == f'您好！\n\n{ZH_FOOTER}'


def test_body_mentions_are_preserved() -> None:
    body = 'The message says "This reply was sent by Dify AI Agent." What does this mean?'
    assert format_customer_reply(body) == f'{body}\n\n{NEW_YORK_FOOTER}'


def test_list_paragraphs_and_sign_off_are_preserved() -> None:
    incoming = 'Hello!\\n\\n1. First step\\n2. Second step\\n\\nBest regards,\\nSupport'
    expected = f'Hello!\n\n1. First step\n2. Second step\n\nBest regards,\nSupport\n\n{NEW_YORK_FOOTER}'
    assert format_customer_reply(incoming) == expected


@pytest.mark.parametrize('body', ['Hello!', '您好！', 'こんにちは。'])
def test_send_request_validation_does_not_duplicate_footer(body: str) -> None:
    from zendesk_browser_relay.schemas import SendRequest

    first = SendRequest(ticket_id=1, body=body)
    second = SendRequest(ticket_id=1, body=first.body)
    assert first.body == second.body
    assert first.body.count('Dify AI Agent') == 1
