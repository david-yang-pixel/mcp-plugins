import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import pytest

from zendesk_browser_relay.browser_relay import (
    CHANNEL_SWITCHER_SELECTOR,
    DEFAULT_COMPOSER_SELECTORS,
    DEFAULT_SEND_SELECTORS,
    NATIVE_MESSAGING_MENU_ITEM_SELECTOR,
    BrowserResult,
    ZendeskBrowserRelay,
)
from zendesk_browser_relay.coordination import TicketRunCoordinator
from zendesk_browser_relay.schemas import SendRequest
from zendesk_browser_relay.settings import Settings


@pytest.mark.asyncio
async def test_fill_rich_text_composer_preserves_email_paragraphs(tmp_path):
    relay = ZendeskBrowserRelay(
        Settings(
            relay_api_key="x" * 32,
            zendesk_profile_dir=tmp_path,
        )
    )
    composer = AsyncMock()
    composer.evaluate.return_value = True
    keyboard = AsyncMock()
    page = AsyncMock()
    page.keyboard = keyboard

    await relay._fill_composer_preserving_paragraphs(
        page,
        composer,
        "Hi,\n\nFirst paragraph.\nSecond line.\n\nBest regards,\nDify Support",
    )

    composer.fill.assert_awaited_once_with("")
    assert keyboard.method_calls == [
        call.insert_text("Hi,"),
        call.press("Shift+Enter"),
        call.press("Shift+Enter"),
        call.insert_text("First paragraph."),
        call.press("Shift+Enter"),
        call.insert_text("Second line."),
        call.press("Shift+Enter"),
        call.press("Shift+Enter"),
        call.insert_text("Best regards,"),
        call.press("Shift+Enter"),
        call.insert_text("Dify Support"),
    ]


@pytest.mark.asyncio
async def test_fill_plain_text_composer_uses_original_body(tmp_path):
    relay = ZendeskBrowserRelay(
        Settings(
            relay_api_key="x" * 32,
            zendesk_profile_dir=tmp_path,
        )
    )
    composer = AsyncMock()
    composer.evaluate.return_value = False
    page = AsyncMock()

    body = "Hi,\n\nFirst paragraph."
    await relay._fill_composer_preserving_paragraphs(page, composer, body)

    composer.fill.assert_awaited_once_with(body)
    page.keyboard.insert_text.assert_not_awaited()


class _FakeRequest:
    def __init__(self, method, url):
        self.method = method
        self.url = url


class _FakeResponse:
    def __init__(self, request, status):
        self.request = request
        self.status = status


class _FakeLocator:
    def __init__(self, page=None):
        self._page = page

    async def click(self):
        if self._page is not None:
            self._page.simulate_send()
        return None

    async def fill(self, value):
        return None

    async def evaluate(self, expression):
        return True

    async def press(self, value):
        if self._page is not None:
            self._page.simulate_send()
        return None

    async def wait_for(self, *, state, timeout):
        return None

    async def inner_text(self):
        return "messaging"

    @property
    def first(self):
        return self

    def nth(self, index):
        return self

    async def count(self):
        return 0

    async def is_visible(self):
        return True


class _FakePage:
    # Status of the Zendesk write a send click triggers; None sends nothing.
    send_write_status: int | None = 200

    def __init__(self):
        self.url = "about:blank"
        self.load_states: list[str] = []
        self.gotos: list[str] = []
        self.keyboard = _FakeKeyboard()
        self.close_count = 0
        self.closed = False
        self.handlers: dict[str, list] = {}

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    def remove_listener(self, event, handler):
        self.handlers.get(event, []).remove(handler)

    def emit(self, event, value):
        for handler in list(self.handlers.get(event, [])):
            handler(value)

    def simulate_send(self):
        if self.send_write_status is None:
            return
        request = _FakeRequest(
            "PUT", "https://langgenius.zendesk.com/api/v2/tickets/2861.json"
        )
        self.emit("request", request)
        self.emit("response", _FakeResponse(request, self.send_write_status))

    async def close(self):
        self.close_count += 1
        self.closed = True

    async def goto(self, url, *, wait_until=None, timeout=None):
        self.url = url
        self.gotos.append(url)

    async def wait_for_load_state(self, state, *, timeout):
        self.load_states.append(state)

    async def wait_for_timeout(self, timeout):
        return None

    def locator(self, selector):
        return _FakeLocator(self)


class _FakeKeyboard:
    async def insert_text(self, value):
        return None

    async def press(self, value):
        return None


class _LoginRequiredPage(_FakePage):
    async def goto(self, url, *, wait_until=None, timeout=None):
        await super().goto(url, wait_until=wait_until)
        self.url = "https://langgenius.zendesk.com/login"


class _FakePageWithoutSwitcher(_FakePage):
    def locator(self, selector):
        if selector == CHANNEL_SWITCHER_SELECTOR:
            return _MissingLocator()
        return _FakeLocator(self)


class _FakePageWithoutSendButton(_FakePage):
    def __init__(self):
        super().__init__()
        self.pressed: list[str] = []

    def locator(self, selector):
        if selector in DEFAULT_SEND_SELECTORS:
            return _MissingLocator()
        if selector in DEFAULT_COMPOSER_SELECTORS:
            return _PressingLocator(self)
        return _FakeLocator()


class _FakePageWithOnlyGenericComposer(_FakePage):
    def locator(self, selector):
        if selector == DEFAULT_COMPOSER_SELECTORS[0]:
            return _MissingLocator()
        return _FakeLocator(self)


class _FakePageWithFailingSendClick(_FakePage):
    def locator(self, selector):
        if selector in DEFAULT_SEND_SELECTORS:
            return _ClickFailingLocator()
        return _FakeLocator(self)


class _PressingLocator(_FakeLocator):
    async def press(self, value):
        self._page.pressed.append(value)
        await super().press(value)


class _ClickFailingLocator(_FakeLocator):
    async def click(self):
        from playwright.async_api import TimeoutError

        raise TimeoutError("click failed")


class _MissingLocator(_FakeLocator):
    async def wait_for(self, *, state, timeout):
        from playwright.async_api import TimeoutError

        raise TimeoutError("missing")


class _FakeApiResponse:
    def __init__(self, status=200, payload=None):
        self.status = status
        self._payload = payload
        self.disposed = False

    async def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    async def dispose(self):
        self.disposed = True


class _FakeApiRequest:
    def __init__(self, response=None, error=None):
        self.response = response or _FakeApiResponse(
            payload={"user": {"id": 1, "role": "agent"}}
        )
        self.error = error
        self.urls: list[str] = []

    async def get(self, url, **kwargs):
        self.urls.append(url)
        if self.error is not None:
            raise self.error
        return self.response


class _FakeContext:
    def __init__(self, page=None, *, new_page_type=_FakePage):
        self._new_page_type = new_page_type
        self.pages = [page or new_page_type()]
        self.close_count = 0
        self.default_timeout = None
        self.is_closed = False
        self.route_count = 0
        self.handlers: dict[str, list] = {}
        self.request = _FakeApiRequest()
        self.cookie_jar: list[dict] = []
        self.added_cookies: list[dict] = []

    def set_default_timeout(self, timeout):
        self.default_timeout = timeout

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    async def route(self, pattern, handler):
        self.route_count += 1

    async def cookies(self, urls=None):
        return list(self.cookie_jar)

    async def add_cookies(self, cookies):
        self.added_cookies.extend(cookies)
        self.cookie_jar.extend(cookies)

    async def new_page(self):
        page = self._new_page_type()
        self.pages.append(page)
        return page

    async def close(self):
        self.close_count += 1
        self.is_closed = True
        for handler in self.handlers.get("close", []):
            handler(self)


class _FakeChromium:
    def __init__(self, context):
        self.context = context
        self.launch_count = 0

    async def launch_persistent_context(self, **kwargs):
        self.launch_count += 1
        self.launch_kwargs = kwargs
        return self.context


class _FakePlaywrightManager:
    def __init__(self, context):
        self.chromium = _FakeChromium(context)
        self.start_count = 0
        self.stop_count = 0

    async def start(self):
        self.start_count += 1
        return self

    async def stop(self):
        self.stop_count += 1


class _SequentialChromium:
    def __init__(self, contexts):
        self.contexts = list(contexts)
        self.launch_count = 0

    async def launch_persistent_context(self, **kwargs):
        self.launch_count += 1
        context = self.contexts.pop(0)
        if isinstance(context, Exception):
            raise context
        return context


class _SequentialPlaywrightManager:
    def __init__(self, contexts):
        self.chromium = _SequentialChromium(contexts)
        self.start_count = 0
        self.stop_count = 0

    async def start(self):
        self.start_count += 1
        return self

    async def stop(self):
        self.stop_count += 1


def test_default_selectors_target_native_messaging_composer_first():
    assert DEFAULT_COMPOSER_SELECTORS[0] == (
        '[data-test-id="omnicomposer-plain-text-ckeditor"]'
    )
    assert DEFAULT_SEND_SELECTORS[0] == (
        '[data-test-id="omnichannel-omnicomposer-send-button"]'
    )
    assert "native_messaging" in NATIVE_MESSAGING_MENU_ITEM_SELECTOR


@pytest.mark.asyncio
async def test_freshness_check_returns_stale_context(monkeypatch, tmp_path):
    relay = ZendeskBrowserRelay(
        Settings(
            relay_api_key="x" * 32,
            zendesk_profile_dir=tmp_path,
        )
    )

    def fake_post(url, headers, payload):
        assert url == "http://127.0.0.1:8080/internal/pre-send-freshness"
        assert headers["Authorization"] == "Bearer relay-secret"
        assert payload["snapshot_customer_message_count"] == 1
        return {
            "ok": True,
            "continue_send": False,
            "status": "stale_context",
            "reason": "substantive_heuristic",
        }

    monkeypatch.setattr(relay, "_post_freshness_check", fake_post)
    result = await relay._check_freshness_before_send(
        SendRequest(
            ticket_id=2861,
            body="hello",
            freshness_check_url="http://127.0.0.1:8080/internal/pre-send-freshness",
            freshness_check_api_key="relay-secret",
            snapshot_customer_message_count=1,
            snapshot_customer_last_message_id="m1",
        )
    )

    assert result is not None
    assert result.ok is False
    assert result.status == "stale_context"


@pytest.mark.asyncio
async def test_fixed_zendesk_freshness_checker_blocks_a_new_customer_cursor(
    tmp_path,
):
    class FakeFreshnessChecker:
        async def current_customer_cursor(self, ticket_id):
            assert ticket_id == 2861
            return "message-2", 2

    relay = ZendeskBrowserRelay(
        Settings(
            relay_api_key="x" * 32,
            zendesk_profile_dir=tmp_path,
        ),
        freshness_checker=FakeFreshnessChecker(),
    )

    result = await relay._check_freshness_before_send(
        SendRequest(
            ticket_id=2861,
            body="stale answer",
            snapshot_customer_message_count=1,
            snapshot_customer_last_message_id="message-1",
        )
    )

    assert result is not None
    assert result.ok is False
    assert result.status == "stale_context"
    assert result.error == "live_customer_cursor_changed"


@pytest.mark.asyncio
async def test_click_time_generation_check_rejects_coalesced_customer_update(
    tmp_path,
):
    coordinator = TicketRunCoordinator(tmp_path / "ticket-runs.sqlite3")
    first = coordinator.claim(
        ticket_id=2861,
        event_id="message-1",
        customer_last_message_id="message-1",
        customer_message_count=1,
    )
    relay = ZendeskBrowserRelay(
        Settings(
            relay_api_key="x" * 32,
            zendesk_profile_dir=tmp_path / "profile",
        ),
        coordinator=coordinator,
    )
    coordinator.claim(
        ticket_id=2861,
        event_id="message-2",
        customer_last_message_id="message-2",
        customer_message_count=2,
    )

    result = await relay._check_freshness_before_send(
        SendRequest(
            ticket_id=2861,
            body="stale answer",
            run_token=first.run_token,
            snapshot_customer_message_count=1,
            snapshot_customer_last_message_id="message-1",
        )
    )

    assert result is not None
    assert result.ok is False
    assert result.status == "stale_context"
    assert result.error == "run_generation_superseded"


@pytest.mark.asyncio
async def test_send_message_page_does_not_wait_for_networkidle(monkeypatch, tmp_path):
    relay = ZendeskBrowserRelay(
        Settings(
            relay_api_key="x" * 32,
            zendesk_profile_dir=tmp_path,
        )
    )
    page = _FakePage()

    async def fresh(request):
        return None

    monkeypatch.setattr(relay, "_check_freshness_before_send", fresh)

    result = await relay._send_message_page(
        page,
        SendRequest(ticket_id=2861, body="hello"),
    )

    assert result.ok is True
    assert result.status == "sent"
    assert "networkidle" not in page.load_states


@pytest.mark.asyncio
async def test_send_message_page_allows_composer_without_channel_switcher(
    monkeypatch, tmp_path
):
    relay = ZendeskBrowserRelay(
        Settings(
            relay_api_key="x" * 32,
            zendesk_profile_dir=tmp_path,
        )
    )
    page = _FakePageWithoutSwitcher()

    async def fresh(request):
        return None

    monkeypatch.setattr(relay, "_check_freshness_before_send", fresh)

    result = await relay._send_message_page(
        page,
        SendRequest(ticket_id=2861, body="hello"),
    )

    assert result.ok is True
    assert result.status == "sent"


@pytest.mark.asyncio
async def test_send_message_page_falls_back_to_keyboard_shortcut(monkeypatch, tmp_path):
    relay = ZendeskBrowserRelay(
        Settings(
            relay_api_key="x" * 32,
            zendesk_profile_dir=tmp_path,
        )
    )
    page = _FakePageWithoutSendButton()

    async def fresh(request):
        return None

    monkeypatch.setattr(relay, "_check_freshness_before_send", fresh)

    result = await relay._send_message_page(
        page,
        SendRequest(ticket_id=2861, body="hello"),
    )

    assert result.ok is True
    assert result.status == "sent"
    assert page.pressed == ["Control+Enter"]


@pytest.mark.asyncio
async def test_send_message_page_rejects_generic_composer_without_native_composer(
    monkeypatch,
    tmp_path,
):
    relay = ZendeskBrowserRelay(
        Settings(
            relay_api_key="x" * 32,
            zendesk_profile_dir=tmp_path,
        )
    )
    page = _FakePageWithOnlyGenericComposer()

    async def fresh(request):
        return None

    monkeypatch.setattr(relay, "_check_freshness_before_send", fresh)

    result = await relay._send_message_page(
        page,
        SendRequest(ticket_id=2861, body="hello"),
    )

    assert result.ok is False
    assert result.status == "send_failed"


@pytest.mark.asyncio
async def test_send_message_does_not_retry_login_required_after_auto_login_fails(
    monkeypatch,
    tmp_path,
):
    relay = ZendeskBrowserRelay(
        Settings(
            relay_api_key="x" * 32,
            zendesk_profile_dir=tmp_path,
        )
    )
    first_context = _FakeContext(new_page_type=_LoginRequiredPage)
    second_context = _FakeContext()
    manager = _SequentialPlaywrightManager([first_context, second_context])

    async def login_still_required(page):
        return BrowserResult(
            ok=False,
            status="login_required",
            current_url=page.url,
            error="auto_login_incomplete",
        )

    async def fresh(request):
        return None

    monkeypatch.setattr(
        "zendesk_browser_relay.browser_relay.async_playwright",
        lambda: manager,
    )
    monkeypatch.setattr(relay, "_try_auto_login", login_still_required)
    monkeypatch.setattr(relay, "_check_freshness_before_send", fresh)

    result = await relay.send_message(SendRequest(ticket_id=2861, body="hello"))

    assert result.ok is False
    assert result.status == "login_required"
    assert first_context.close_count == 0
    assert second_context.close_count == 0
    assert manager.chromium.launch_count == 1


@pytest.mark.asyncio
async def test_readiness_auto_recovers_login_and_notifies_only_on_transitions(
    monkeypatch,
    tmp_path,
):
    notifier = AsyncMock()
    relay = ZendeskBrowserRelay(
        Settings(
            relay_api_key="x" * 32,
            zendesk_profile_dir=tmp_path,
            zendesk_login_email="support@example.com",
            zendesk_login_password="secret-password",
        ),
        feishu_notifier=notifier,
    )
    results = [
        BrowserResult(ok=False, status="login_required"),
        BrowserResult(ok=True, status="logged_in"),
        BrowserResult(ok=True, status="logged_in"),
    ]

    async def passive_probe():
        return results.pop(0)

    async def with_page(operation, *, count_use=True):
        return await operation(_FakePage())

    monkeypatch.setattr(relay, "_check_login_status", passive_probe)
    monkeypatch.setattr(relay, "_with_page", with_page)
    auto_login = AsyncMock(
        return_value=BrowserResult(ok=True, status="logged_in"),
    )
    monkeypatch.setattr(relay, "_try_auto_login", auto_login)

    first = await relay.check_readiness()
    second = await relay.check_readiness()

    assert first.status == "logged_in"
    assert second.status == "logged_in"
    auto_login.assert_awaited_once()
    notifier.notify_readiness.assert_not_awaited()


@pytest.mark.asyncio
async def test_readiness_auto_login_uses_exponential_backoff_after_failure(
    monkeypatch,
    tmp_path,
):
    relay = ZendeskBrowserRelay(
        Settings(
            relay_api_key="x" * 32,
            zendesk_profile_dir=tmp_path,
            zendesk_login_email="support@example.com",
            zendesk_login_password="secret-password",
            zendesk_auto_login_initial_backoff_seconds=30,
            zendesk_auto_login_max_backoff_seconds=300,
        )
    )

    async def login_required():
        return BrowserResult(ok=False, status="login_required")

    async def with_page(operation, *, count_use=True):
        return await operation(_FakePage())

    clock = [100.0]
    monkeypatch.setattr(
        "zendesk_browser_relay.browser_relay.time.monotonic",
        lambda: clock[0],
    )
    monkeypatch.setattr(relay, "_check_login_status", login_required)
    monkeypatch.setattr(relay, "_with_page", with_page)
    auto_login = AsyncMock(
        return_value=BrowserResult(
            ok=False,
            status="login_required",
            error="auto_login_incomplete",
        )
    )
    monkeypatch.setattr(relay, "_try_auto_login", auto_login)

    first = await relay.check_readiness()
    immediate_retry = await relay.check_readiness()
    clock[0] = 130.0
    delayed_retry = await relay.check_readiness()

    assert first.status == "login_required"
    assert immediate_retry.status == "login_required"
    assert delayed_retry.status == "login_required"
    assert auto_login.await_count == 2


@pytest.mark.asyncio
async def test_readiness_auto_login_attempt_has_a_total_timeout(
    monkeypatch,
    tmp_path,
):
    relay = ZendeskBrowserRelay(
        Settings(
            relay_api_key="x" * 32,
            zendesk_profile_dir=tmp_path,
            zendesk_login_email="support@example.com",
            zendesk_login_password="secret-password",
            zendesk_auto_login_attempt_timeout_seconds=45,
        )
    )

    async def login_required():
        return BrowserResult(ok=False, status="login_required")

    async def with_page(operation, *, count_use=True):
        return await operation(_FakePage())

    observed_timeouts = []
    original_wait_for = asyncio.wait_for

    async def timeout_on_auto_login(awaitable, *, timeout):
        observed_timeouts.append(timeout)
        if timeout == 45:
            if hasattr(awaitable, "close"):
                awaitable.close()
            raise TimeoutError
        return await original_wait_for(awaitable, timeout=timeout)

    monkeypatch.setattr(relay, "_check_login_status", login_required)
    monkeypatch.setattr(relay, "_with_page", with_page)
    monkeypatch.setattr(
        "zendesk_browser_relay.browser_relay.asyncio.wait_for",
        timeout_on_auto_login,
    )

    result = await relay.check_readiness()

    assert result.status == "login_required"
    assert relay._login_recovery_active is False
    assert relay._auto_login_failures == 1
    assert 45 in observed_timeouts


@pytest.mark.asyncio
async def test_optional_selector_timeout_is_shared_across_all_selectors(
    monkeypatch,
    tmp_path,
):
    relay = ZendeskBrowserRelay(
        Settings(relay_api_key="x" * 32, zendesk_profile_dir=tmp_path)
    )
    page = _FakePage()
    observed_timeouts = []

    class MissingWithTimeout(_MissingLocator):
        async def wait_for(self, *, state, timeout):
            observed_timeouts.append(timeout)
            await super().wait_for(state=state, timeout=timeout)

    page.locator = lambda _selector: MissingWithTimeout()
    clock = iter((100.0, 100.5, 101.2, 102.1))
    monkeypatch.setattr(
        "zendesk_browser_relay.browser_relay.time",
        SimpleNamespace(monotonic=lambda: next(clock), time=lambda: 0),
    )

    result = await relay._first_visible_optional(
        page,
        ("one", "two", "three", "four"),
        timeout=2_000,
    )

    assert result is None
    assert observed_timeouts[0] == 1_500
    assert 799 <= observed_timeouts[1] <= 800


@pytest.mark.asyncio
async def test_failed_readiness_notification_is_retried_on_next_probe(
    monkeypatch,
    tmp_path,
):
    notifier = AsyncMock()
    notifier.notify_readiness.side_effect = [RuntimeError("offline"), None]
    relay = ZendeskBrowserRelay(
        Settings(relay_api_key="x" * 32, zendesk_profile_dir=tmp_path),
        feishu_notifier=notifier,
    )

    async def passive_probe():
        return BrowserResult(ok=False, status="login_required")

    monkeypatch.setattr(relay, "_check_login_status", passive_probe)

    await relay.check_readiness()
    await relay.check_readiness()

    assert notifier.notify_readiness.await_count == 2


@pytest.mark.asyncio
async def test_readiness_reports_busy_without_waiting_for_an_active_send(tmp_path):
    notifier = AsyncMock()
    relay = ZendeskBrowserRelay(
        Settings(relay_api_key="x" * 32, zendesk_profile_dir=tmp_path),
        feishu_notifier=notifier,
    )
    lock = relay._get_lock()
    await lock.acquire()
    try:
        result = await asyncio.wait_for(relay.check_readiness(), timeout=0.1)
    finally:
        lock.release()

    assert result.ok is True
    assert result.status == "busy"
    notifier.notify_readiness.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_message_retries_safe_send_failed_with_fresh_context(
    monkeypatch,
    tmp_path,
):
    relay = ZendeskBrowserRelay(
        Settings(
            relay_api_key="x" * 32,
            zendesk_profile_dir=tmp_path,
        )
    )
    first_context = _FakeContext(new_page_type=_FakePageWithOnlyGenericComposer)
    second_context = _FakeContext()
    manager = _SequentialPlaywrightManager([first_context, second_context])

    async def fresh(request):
        return None

    monkeypatch.setattr(
        "zendesk_browser_relay.browser_relay.async_playwright",
        lambda: manager,
    )
    monkeypatch.setattr(relay, "_check_freshness_before_send", fresh)

    result = await relay.send_message(SendRequest(ticket_id=2861, body="hello"))

    assert result.ok is True
    assert result.status == "sent"
    assert first_context.close_count == 1
    assert second_context.close_count == 0
    assert manager.chromium.launch_count == 2


@pytest.mark.asyncio
async def test_send_message_does_not_retry_after_click_stage_failure(
    monkeypatch,
    tmp_path,
):
    relay = ZendeskBrowserRelay(
        Settings(
            relay_api_key="x" * 32,
            zendesk_profile_dir=tmp_path,
        )
    )
    context = _FakeContext(new_page_type=_FakePageWithFailingSendClick)
    manager = _SequentialPlaywrightManager([context])

    async def fresh(request):
        return None

    monkeypatch.setattr(
        "zendesk_browser_relay.browser_relay.async_playwright",
        lambda: manager,
    )
    monkeypatch.setattr(relay, "_check_freshness_before_send", fresh)

    result = await relay.send_message(SendRequest(ticket_id=2861, body="hello"))

    assert result.ok is False
    assert result.status == "send_unknown"
    assert result.error == "TimeoutError:click"
    assert context.close_count == 0
    assert manager.chromium.launch_count == 1


@pytest.mark.asyncio
async def test_with_page_reuses_context_but_closes_each_page(monkeypatch, tmp_path):
    relay = ZendeskBrowserRelay(
        Settings(
            relay_api_key="x" * 32,
            zendesk_profile_dir=tmp_path,
        )
    )
    context = _FakeContext()
    manager = _FakePlaywrightManager(context)

    monkeypatch.setattr(
        "zendesk_browser_relay.browser_relay.async_playwright",
        lambda: manager,
    )

    first = await relay._with_page(lambda page: page)
    second = await relay._with_page(lambda page: page)

    assert first is not second
    assert first.close_count == 1
    assert second.close_count == 1
    assert manager.start_count == 1
    assert manager.chromium.launch_count == 1
    assert context.close_count == 0
    assert context.route_count == 0
    assert "--disable-dev-shm-usage" not in manager.chromium.launch_kwargs["args"]
    assert manager.chromium.launch_kwargs["ignore_default_args"] == [
        "--disable-dev-shm-usage"
    ]
    assert "--blink-settings=imagesEnabled=false" in (
        manager.chromium.launch_kwargs["args"]
    )

    await relay.close()

    assert context.close_count == 1
    assert manager.stop_count == 1


@pytest.mark.asyncio
async def test_readiness_probe_does_not_increment_context_uses(monkeypatch, tmp_path):
    relay = ZendeskBrowserRelay(
        Settings(
            relay_api_key="x" * 32,
            zendesk_profile_dir=tmp_path,
        )
    )
    context = _FakeContext()
    manager = _FakePlaywrightManager(context)
    monkeypatch.setattr(
        "zendesk_browser_relay.browser_relay.async_playwright",
        lambda: manager,
    )

    await relay._with_page(lambda page: page, count_use=False)
    await relay._with_page(lambda page: page, count_use=False)

    assert relay._context_uses == 0

    await relay._with_page(lambda page: page, count_use=True)

    assert relay._context_uses == 1


class _DisconnectingPage(_FakePage):
    async def close(self):
        raise RuntimeError("Browser disconnected")


class _DisconnectingContext(_FakeContext):
    async def close(self):
        self.close_count += 1
        raise RuntimeError("Browser disconnected")


class _CancelledCloseContext(_FakeContext):
    async def close(self):
        raise asyncio.CancelledError()


@pytest.mark.asyncio
async def test_readiness_timeout_cleanup_holds_the_profile_lock(monkeypatch, tmp_path):
    relay = ZendeskBrowserRelay(
        Settings(relay_api_key="x" * 32, zendesk_profile_dir=tmp_path)
    )
    relay._settings.zendesk_browser_operation_timeout_seconds = 0.05
    release_close = asyncio.Event()
    close_entered = asyncio.Event()
    competitor_finished = asyncio.Event()

    class _SlowCloseContext(_FakeContext):
        async def close(self):
            close_entered.set()
            await release_close.wait()
            await super().close()

    first = _SlowCloseContext()
    second = _FakeContext()
    relay._context = first

    async def hang():
        await asyncio.Event().wait()

    monkeypatch.setattr(relay, "_check_and_recover_readiness", hang)

    async def competitor():
        await close_entered.wait()
        async with relay._get_lock():
            assert first.is_closed
            assert relay._context is None
            relay._context = second
        competitor_finished.set()

    readiness = asyncio.create_task(relay.check_readiness())
    other = asyncio.create_task(competitor())
    await asyncio.wait_for(close_entered.wait(), timeout=2)
    await asyncio.sleep(0.05)
    assert not competitor_finished.is_set()
    assert relay._get_lock().locked()
    release_close.set()
    result = await asyncio.wait_for(readiness, timeout=2)
    await asyncio.wait_for(other, timeout=2)

    assert result.ok is False
    assert result.status == "unknown"
    assert first.close_count == 1
    assert second.close_count == 0
    assert relay._context is second


@pytest.mark.asyncio
async def test_page_close_exception_does_not_mask_or_abort(tmp_path):
    relay = ZendeskBrowserRelay(
        Settings(relay_api_key="x" * 32, zendesk_profile_dir=tmp_path)
    )
    context = _FakeContext(new_page_type=_DisconnectingPage)
    relay._context = context

    async def fail(_page):
        raise RuntimeError("navigation failed")

    with pytest.raises(RuntimeError, match="navigation failed"):
        await relay._with_page(fail)

    assert await relay._with_page(lambda _page: "ok") == "ok"
    assert context.close_count == 0


@pytest.mark.asyncio
async def test_context_close_exception_still_stops_playwright(tmp_path):
    relay = ZendeskBrowserRelay(
        Settings(relay_api_key="x" * 32, zendesk_profile_dir=tmp_path)
    )
    context = _DisconnectingContext()
    manager = _FakePlaywrightManager(context)
    relay._context = context
    relay._playwright = manager
    relay._playwright_manager = manager

    await relay._close_context(reason="readiness_timeout", stop_playwright=True)

    assert context.close_count == 1
    assert manager.stop_count == 1
    assert relay._context is None
    assert relay._playwright is None
    assert relay._playwright_manager is None


@pytest.mark.asyncio
async def test_context_close_cancellation_propagates_after_stopping(tmp_path):
    relay = ZendeskBrowserRelay(
        Settings(relay_api_key="x" * 32, zendesk_profile_dir=tmp_path)
    )
    context = _CancelledCloseContext()
    manager = _FakePlaywrightManager(context)
    relay._context = context
    relay._playwright = manager
    relay._playwright_manager = manager

    with pytest.raises(asyncio.CancelledError):
        await relay._close_context(reason="shutdown", stop_playwright=True)

    assert manager.stop_count == 1
    assert relay._playwright is None


@pytest.mark.asyncio
async def test_configured_mcp_url_without_checker_fails_closed(tmp_path):
    relay = ZendeskBrowserRelay(
        Settings(
            relay_api_key="x" * 32,
            zendesk_profile_dir=tmp_path,
            zendesk_mcp_url="https://zendesk.example/mcp",
        )
    )

    result = await relay._check_freshness_before_send(
        SendRequest(ticket_id=2861, body="hello")
    )

    assert result is not None
    assert result.ok is False
    assert result.status == "freshness_check_failed"
    assert result.error == "authorization_unavailable"


# --- Send outcome after the click -------------------------------------------


def _relay(tmp_path, **overrides):
    return ZendeskBrowserRelay(
        Settings(relay_api_key="x" * 32, zendesk_profile_dir=tmp_path, **overrides)
    )


async def _always_fresh(request):
    return None


class _HangAfterClickPage(_FakePage):
    """The click starts a write that never returns."""

    def simulate_send(self):
        self.emit(
            "request",
            _FakeRequest(
                "PUT", "https://langgenius.zendesk.com/api/v2/tickets/2861.json"
            ),
        )


@pytest.mark.asyncio
async def test_timeout_after_click_reports_send_unknown_without_retry(
    monkeypatch, tmp_path
):
    relay = _relay(tmp_path, zendesk_send_confirm_timeout_ms=60_000)
    relay._settings.zendesk_browser_operation_timeout_seconds = 0.2
    first = _FakeContext(new_page_type=_HangAfterClickPage)
    second = _FakeContext()
    manager = _SequentialPlaywrightManager([first, second])
    monkeypatch.setattr(
        "zendesk_browser_relay.browser_relay.async_playwright", lambda: manager
    )
    monkeypatch.setattr(relay, "_check_freshness_before_send", _always_fresh)

    result = await relay.send_message(SendRequest(ticket_id=2861, body="hello"))

    assert result.ok is False
    assert result.status == "send_unknown"
    assert result.error == "operation_timeout:confirm"
    assert first.close_count == 1
    assert manager.chromium.launch_count == 1


@pytest.mark.asyncio
async def test_timeout_before_click_is_send_failed_and_each_attempt_has_its_own_timer(
    monkeypatch, tmp_path
):
    relay = _relay(tmp_path)
    relay._settings.zendesk_browser_operation_timeout_seconds = 0.2
    calls = []

    async def channel(page):
        calls.append(asyncio.get_running_loop().time())
        if len(calls) == 1:
            # The first attempt spends its whole budget before the click.
            await asyncio.sleep(1)

    first = _FakeContext()
    second = _FakeContext()
    manager = _SequentialPlaywrightManager([first, second])
    monkeypatch.setattr(
        "zendesk_browser_relay.browser_relay.async_playwright", lambda: manager
    )
    monkeypatch.setattr(relay, "_check_freshness_before_send", _always_fresh)
    monkeypatch.setattr(relay, "_ensure_native_messaging_channel", channel)
    observed = []
    original_wait_for = asyncio.wait_for

    async def wait_for(awaitable, *, timeout):
        observed.append(timeout)
        return await original_wait_for(awaitable, timeout=timeout)

    monkeypatch.setattr(
        "zendesk_browser_relay.browser_relay.asyncio.wait_for", wait_for
    )
    # The second attempt needs about a second for the confirm quiet period,
    # longer than the 0.2s budget the first attempt used up.
    relay._settings.zendesk_browser_operation_timeout_seconds = 0.5
    monkeypatch.setattr(
        "zendesk_browser_relay.browser_relay._WriteRequestTracker.settle",
        _quick_settle,
    )

    result = await relay.send_message(SendRequest(ticket_id=2861, body="hello"))

    assert result.status == "sent"
    assert observed.count(0.5) == 2
    assert first.close_count == 1
    assert manager.chromium.launch_count == 2


async def _quick_settle(self, timeout_seconds, quiet_seconds=1.0):
    return await _ORIGINAL_SETTLE(self, timeout_seconds, quiet_seconds=0.01)


from zendesk_browser_relay.browser_relay import _WriteRequestTracker  # noqa: E402

_ORIGINAL_SETTLE = _WriteRequestTracker.settle


@pytest.mark.asyncio
async def test_page_stays_open_until_the_reply_write_returns(monkeypatch, tmp_path):
    relay = _relay(tmp_path)
    events = []

    class DelayedWritePage(_FakePage):
        def simulate_send(self):
            if not self.handlers.get("request"):
                return  # The composer click before the tracker is attached.
            request = _FakeRequest(
                "PUT", "https://langgenius.zendesk.com/api/v2/tickets/2861.json"
            )
            self.emit("request", request)

            async def respond():
                await asyncio.sleep(0.2)
                events.append("response")
                self.emit("response", _FakeResponse(request, 200))

            asyncio.get_running_loop().create_task(respond())

        async def close(self):
            events.append("close")
            await super().close()

    relay._context = _FakeContext(new_page_type=DelayedWritePage)
    monkeypatch.setattr(relay, "_check_freshness_before_send", _always_fresh)
    monkeypatch.setattr(_WriteRequestTracker, "settle", _quick_settle)

    result = await relay._send_message_once(SendRequest(ticket_id=2861, body="hello"))

    assert result.status == "sent"
    assert events == ["response", "close"]


class _BodyStaysComposer(_FakeLocator):
    def __init__(self, page, body):
        super().__init__(page)
        self._body = body

    async def inner_text(self):
        return self._body


@pytest.mark.parametrize(
    ("write_status", "body_remains", "status", "error"),
    [
        (200, False, "sent", None),
        (500, True, "send_failed", "write_request_failed:confirm"),
        (500, False, "send_unknown", "write_request_failed:confirm"),
        (200, True, "send_unknown", "composer_still_contains_body:confirm"),
        (None, True, "send_not_confirmed", "composer_still_contains_body"),
        (None, False, "send_unknown", "no_write_request_observed:confirm"),
    ],
)
@pytest.mark.asyncio
async def test_send_outcome_follows_write_request_and_composer(
    monkeypatch, tmp_path, write_status, body_remains, status, error
):
    relay = _relay(tmp_path, zendesk_send_confirm_timeout_ms=1_000)
    request = SendRequest(ticket_id=2861, body="hello")

    class Page(_FakePage):
        send_write_status = write_status

        def locator(self, selector):
            if body_remains and selector in DEFAULT_COMPOSER_SELECTORS:
                return _BodyStaysComposer(self, request.body)
            return _FakeLocator(self)

    monkeypatch.setattr(relay, "_check_freshness_before_send", _always_fresh)
    monkeypatch.setattr(_WriteRequestTracker, "settle", _quick_settle)

    result = await relay._send_message_page(Page(), request)

    assert result.status == status
    assert result.error == error


@pytest.mark.asyncio
async def test_write_tracker_ignores_reads_other_hosts_and_unmatched_paths():
    page = _FakePage()
    tracker = _WriteRequestTracker(
        base_url="https://langgenius.zendesk.com",
        pattern=__import__("re").compile(r"/api/v2/tickets/2861\b"),
    )
    tracker.attach(page)
    for method, url in (
        ("GET", "https://langgenius.zendesk.com/api/v2/tickets/2861.json"),
        ("POST", "https://langgenius.zendesk.com.evil.test/api/v2/tickets/2861"),
        ("POST", "https://langgenius.zendesk.com/frontendevents/pv"),
    ):
        request = _FakeRequest(method, url)
        page.emit("request", request)
        page.emit("response", _FakeResponse(request, 200))
    tracker.detach(page)

    assert tracker.pending == set()
    assert tracker.succeeded == []
    assert page.handlers == {"request": [], "response": [], "requestfailed": []}


@pytest.mark.asyncio
async def test_send_confirm_pattern_substitutes_ticket_id(tmp_path):
    relay = _relay(
        tmp_path,
        zendesk_send_confirm_url_pattern=r"/api/v2/tickets/{ticket_id}\.json",
    )

    pattern = relay._send_confirm_pattern(2861)

    assert pattern.search("https://x.zendesk.com/api/v2/tickets/2861.json")
    assert not pattern.search("https://x.zendesk.com/api/v2/tickets/2862.json")


def test_invalid_send_confirm_pattern_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        _relay(tmp_path, zendesk_send_confirm_url_pattern="(")


# --- Browser and driver lifecycle -------------------------------------------


@pytest.mark.asyncio
async def test_context_close_timeout_kills_profile_processes(monkeypatch, tmp_path):
    relay = _relay(tmp_path)

    class HangingClose(_FakeContext):
        async def close(self):
            await asyncio.Event().wait()

    relay._context = HangingClose()
    relay._settings.zendesk_context_close_timeout_seconds = 0.05
    alive = {41, 42}
    killed = []
    (tmp_path / "SingletonLock").symlink_to("host-41")

    def fake_kill(pid, sig):
        killed.append((pid, sig))
        alive.discard(pid)

    monkeypatch.setattr(
        "zendesk_browser_relay.browser_relay.profile_chromium_pids",
        lambda _profile: set(alive),
    )
    monkeypatch.setattr("zendesk_browser_relay.browser_relay.os.kill", fake_kill)

    await relay._close_context(reason="send_timeout")

    import signal

    assert sorted(killed) == [(41, signal.SIGKILL), (42, signal.SIGKILL)]
    assert not (tmp_path / "SingletonLock").is_symlink()
    assert relay._context is None


@pytest.mark.asyncio
async def test_dead_driver_is_restarted_on_launch_failure(monkeypatch, tmp_path):
    relay = _relay(tmp_path)
    context = _FakeContext()
    managers = [
        _SequentialPlaywrightManager([RuntimeError("Connection closed")]),
        _SequentialPlaywrightManager([context]),
    ]
    created = list(managers)
    monkeypatch.setattr(
        "zendesk_browser_relay.browser_relay.async_playwright",
        lambda: managers.pop(0),
    )

    assert await relay._with_page(lambda page: "ok") == "ok"

    assert created[0].stop_count == 1
    assert created[1].start_count == 1
    assert relay._context is context


@pytest.mark.asyncio
async def test_context_closed_by_chrome_is_relaunched(monkeypatch, tmp_path):
    relay = _relay(tmp_path)
    first = _FakeContext()
    second = _FakeContext()
    manager = _SequentialPlaywrightManager([first, second])
    monkeypatch.setattr(
        "zendesk_browser_relay.browser_relay.async_playwright", lambda: manager
    )

    await relay._with_page(lambda page: None)
    # Chrome crashed: Playwright emits "close" on the context.
    for handler in first.handlers["close"]:
        handler(first)
    await relay._with_page(lambda page: None)

    assert relay._context is second
    assert manager.chromium.launch_count == 2
    assert manager.start_count == 1


@pytest.mark.asyncio
async def test_shutdown_does_not_wait_for_a_stuck_send(tmp_path):
    relay = _relay(tmp_path, zendesk_shutdown_lock_timeout_seconds=1)
    context = _FakeContext()
    manager = _FakePlaywrightManager(context)
    relay._context = context
    relay._playwright = manager
    relay._playwright_manager = manager
    lock = relay._get_lock()
    await lock.acquire()
    try:
        started = asyncio.get_running_loop().time()
        await asyncio.wait_for(relay.close(), timeout=5)
        elapsed = asyncio.get_running_loop().time() - started
    finally:
        lock.release()

    assert elapsed < 2
    assert context.close_count == 1
    assert manager.stop_count == 1
    # The abandoned send cannot bring Chrome back.
    with pytest.raises(RuntimeError, match="shutting down"):
        await relay._get_or_create_context()


# --- Login state ------------------------------------------------------------


@pytest.mark.parametrize(
    ("response", "error", "status"),
    [
        (_FakeApiResponse(payload={"user": {"id": 7, "role": "agent"}}), None, "logged_in"),
        (_FakeApiResponse(payload={"user": {"id": 7, "role": "admin"}}), None, "logged_in"),
        (_FakeApiResponse(payload={"user": {"id": None, "role": "end-user"}}), None, "login_required"),
        (_FakeApiResponse(payload={"user": {"id": 7, "role": "end-user"}}), None, "login_required"),
        (_FakeApiResponse(status=401), None, "login_required"),
        (_FakeApiResponse(status=503), None, "unknown"),
        (_FakeApiResponse(payload=ValueError("not json")), None, "unknown"),
        (_FakeApiResponse(payload={"error": "x"}), None, "unknown"),
        (None, "network", "unknown"),
    ],
)
@pytest.mark.asyncio
async def test_login_status_comes_from_users_me_api(tmp_path, response, error, status):
    from playwright.async_api import Error as PlaywrightError

    relay = _relay(tmp_path)
    context = _FakeContext()
    context.request = _FakeApiRequest(
        response=response,
        error=PlaywrightError("net::ERR_TIMED_OUT") if error else None,
    )
    relay._context = context

    result = await relay._check_login_status()

    assert result.status == status
    assert result.ok is (status == "logged_in")
    assert context.request.urls == [
        "https://langgenius.zendesk.com/api/v2/users/me.json"
    ]
    if response is not None:
        assert response.disposed
    # The probe never opens a page.
    assert len(context.pages) == 1


@pytest.mark.asyncio
async def test_readiness_reports_unknown_when_the_api_is_unreachable(tmp_path):
    from playwright.async_api import Error as PlaywrightError

    relay = _relay(tmp_path)
    context = _FakeContext()
    context.request = _FakeApiRequest(error=PlaywrightError("net::ERR_FAILED"))
    relay._context = context

    result = await relay.check_readiness()

    assert result.ok is False
    assert result.status == "unknown"


@pytest.mark.asyncio
async def test_session_cookies_survive_a_browser_restart(monkeypatch, tmp_path):
    relay = _relay(tmp_path)
    first = _FakeContext()
    first.cookie_jar = [
        {"name": "_zendesk_session", "value": "s", "domain": "langgenius.zendesk.com",
         "path": "/", "expires": -1},
        {"name": "persistent", "value": "p", "domain": "langgenius.zendesk.com",
         "path": "/", "expires": 2_000_000_000},
    ]
    second = _FakeContext()
    second.cookie_jar = [
        {"name": "persistent", "value": "newer", "domain": "langgenius.zendesk.com",
         "path": "/", "expires": 2_000_000_000},
    ]
    manager = _SequentialPlaywrightManager([first, second])
    monkeypatch.setattr(
        "zendesk_browser_relay.browser_relay.async_playwright", lambda: manager
    )

    assert (await relay._check_login_status()).status == "logged_in"
    saved = tmp_path / "relay-session-cookies.json"
    assert saved.stat().st_mode & 0o777 == 0o600
    await relay._close_context(reason="scheduled_recycle")
    await relay._with_page(lambda page: None)

    assert relay._context is second
    assert [c["name"] for c in second.added_cookies] == ["_zendesk_session"]
    assert {c["value"] for c in second.cookie_jar} == {"s", "newer"}


@pytest.mark.asyncio
async def test_corrupt_cookie_snapshot_does_not_block_launch(monkeypatch, tmp_path):
    relay = _relay(tmp_path)
    (tmp_path / "relay-session-cookies.json").write_text("{not json")
    context = _FakeContext()
    manager = _FakePlaywrightManager(context)
    monkeypatch.setattr(
        "zendesk_browser_relay.browser_relay.async_playwright", lambda: manager
    )

    assert await relay._with_page(lambda page: "ok") == "ok"
    assert context.added_cookies == []
