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


class _FakeLocator:
    async def click(self):
        return None

    async def fill(self, value):
        return None

    async def evaluate(self, expression):
        return True

    async def press(self, value):
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
    def __init__(self):
        self.url = "about:blank"
        self.load_states: list[str] = []
        self.gotos: list[str] = []
        self.keyboard = _FakeKeyboard()
        self.close_count = 0
        self.closed = False

    async def close(self):
        self.close_count += 1
        self.closed = True

    async def goto(self, url, *, wait_until=None):
        self.url = url
        self.gotos.append(url)

    async def wait_for_load_state(self, state, *, timeout):
        self.load_states.append(state)

    async def wait_for_timeout(self, timeout):
        return None

    def locator(self, selector):
        return _FakeLocator()


class _FakeKeyboard:
    async def insert_text(self, value):
        return None

    async def press(self, value):
        return None


class _LoginRequiredPage(_FakePage):
    async def goto(self, url, *, wait_until=None):
        await super().goto(url, wait_until=wait_until)
        self.url = "https://langgenius.zendesk.com/login"


class _FakePageWithoutSwitcher(_FakePage):
    def locator(self, selector):
        if selector == CHANNEL_SWITCHER_SELECTOR:
            return _MissingLocator()
        return _FakeLocator()


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
        return _FakeLocator()


class _FakePageWithFailingSendClick(_FakePage):
    def locator(self, selector):
        if selector in DEFAULT_SEND_SELECTORS:
            return _ClickFailingLocator()
        return _FakeLocator()


class _PressingLocator(_FakeLocator):
    def __init__(self, page):
        self._page = page

    async def press(self, value):
        self._page.pressed.append(value)


class _ClickFailingLocator(_FakeLocator):
    async def click(self):
        from playwright.async_api import TimeoutError

        raise TimeoutError("click failed")


class _MissingLocator(_FakeLocator):
    async def wait_for(self, *, state, timeout):
        from playwright.async_api import TimeoutError

        raise TimeoutError("missing")


class _FakeContext:
    def __init__(self, page=None, *, new_page_type=_FakePage):
        self._new_page_type = new_page_type
        self.pages = [page or new_page_type()]
        self.close_count = 0
        self.default_timeout = None
        self.is_closed = False
        self.route_count = 0

    def set_default_timeout(self, timeout):
        self.default_timeout = timeout

    async def route(self, pattern, handler):
        self.route_count += 1

    async def new_page(self):
        page = self._new_page_type()
        self.pages.append(page)
        return page

    async def close(self):
        self.close_count += 1
        self.is_closed = True


class _FakeChromium:
    def __init__(self, context):
        self.context = context
        self.launch_count = 0

    async def launch_persistent_context(self, **kwargs):
        self.launch_count += 1
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
        return self.contexts.pop(0)


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

    async def passive_probe(_page):
        return results.pop(0)

    async def with_page(operation, *, count_use=True):
        return await operation(object())

    monkeypatch.setattr(relay, "_check_readiness_page", passive_probe)
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

    async def login_required(_page):
        return BrowserResult(ok=False, status="login_required")

    async def with_page(operation, *, count_use=True):
        return await operation(object())

    clock = [100.0]
    monkeypatch.setattr(
        "zendesk_browser_relay.browser_relay.time.monotonic",
        lambda: clock[0],
    )
    monkeypatch.setattr(relay, "_check_readiness_page", login_required)
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

    async def login_required(_page):
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

    monkeypatch.setattr(relay, "_check_readiness_page", login_required)
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

    async def passive_probe(_page):
        return BrowserResult(ok=False, status="login_required")

    async def with_page(operation, *, count_use=True):
        return await operation(object())

    monkeypatch.setattr(relay, "_check_readiness_page", passive_probe)
    monkeypatch.setattr(relay, "_with_page", with_page)

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
    assert result.status == "send_failed"
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
    assert context.route_count == 1

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

    async def hang(_page):
        await asyncio.Event().wait()

    monkeypatch.setattr(relay, "_check_and_recover_readiness_page", hang)

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
