from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, async_playwright

from .coordination import TicketRunCoordinator
from .feishu_notifications import FeishuStatusNotifier
from .login_automation import generate_totp_code
from .schemas import SendRequest, SendResponse, SessionResponse
from .settings import Settings

logger = logging.getLogger("uvicorn.error")

CHROMIUM_LAUNCH_ARGS = (
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
)

BLOCKED_RESOURCE_TYPES = frozenset({"image", "media", "font"})

LOGIN_URL_MARKERS = (
    "/auth/",
    "/login",
    "/signin",
    "return_to=",
)

DEFAULT_COMPOSER_SELECTORS = (
    '[data-test-id="omnicomposer-plain-text-ckeditor"]',
    '[data-test-id="rich-text-editor"] [contenteditable="true"]',
    '[contenteditable="true"][role="textbox"]',
    'div[contenteditable="true"]',
    "textarea",
)

DEFAULT_SEND_SELECTORS = (
    '[data-test-id="omnichannel-omnicomposer-send-button"]',
    '[data-test-id="composer-send-button"]',
    'button[data-test-id="submit_button"]',
    'button[aria-label="Send"]',
    'button[aria-label="发送"]',
    'button:has-text("Send")',
    'button:has-text("发送")',
    '[role="button"]:has-text("Send")',
    '[role="button"]:has-text("发送")',
)

CHANNEL_SWITCHER_SELECTOR = '[data-test-id="omnichannel-channel-switcher-button"]'
NATIVE_MESSAGING_MENU_ITEM_SELECTOR = (
    '[data-test-id="omnichannel-channel-switcher-menuitem-native_messaging"]'
)
NATIVE_MESSAGING_LABELS = ("消息传送", "messaging", "message")
COMPOSER_MODE_SWITCHER_SELECTORS = (
    'button:has-text("内部注释")',
    '[role="button"]:has-text("内部注释")',
    'button:has-text("Internal note")',
    '[role="button"]:has-text("Internal note")',
    'button:has-text("消息传送")',
    '[role="button"]:has-text("消息传送")',
    'button:has-text("Messaging")',
    '[role="button"]:has-text("Messaging")',
)
COMPOSER_MODE_MESSAGING_SELECTORS = (
    '[role="menuitem"]:has-text("消息传送")',
    '[role="option"]:has-text("消息传送")',
    'button:has-text("消息传送")',
    '[role="menuitem"]:has-text("Messaging")',
    '[role="option"]:has-text("Messaging")',
    'button:has-text("Messaging")',
)

EMAIL_INPUT_SELECTORS = (
    'input[type="email"]',
    'input[name="email"]',
    'input[name="user[email]"]',
    "input#user_email",
)
PASSWORD_INPUT_SELECTORS = (
    'input[type="password"]',
    'input[name="password"]',
    'input[name="user[password]"]',
    "input#user_password",
)
OTP_INPUT_SELECTORS = (
    'input[autocomplete="one-time-code"]',
    'input[name="otp"]',
    'input[name*="otp"]',
    'input[name*="code"]',
    'input[id*="otp"]',
    'input[id*="code"]',
    'input[type="tel"]',
)
SUBMIT_SELECTORS = (
    'button[type="submit"]',
    'input[type="submit"]',
    'button:has-text("Sign in")',
    'button:has-text("Log in")',
    'button:has-text("Continue")',
    'button:has-text("Next")',
    'button:has-text("Verify")',
    'button:has-text("登录")',
    'button:has-text("继续")',
    'button:has-text("下一步")',
    'button:has-text("验证")',
)


@dataclass(frozen=True)
class BrowserResult:
    ok: bool
    status: str
    current_url: Optional[str] = None
    error: Optional[str] = None


class ZendeskBrowserRelay:
    _RECYCLE_AFTER_USES = 200
    _RECYCLE_AFTER_SECONDS = 6 * 60 * 60
    _RETRYABLE_SEND_FAILURE_STAGES = frozenset(
        (
            "channel",
            "composer",
            "fill",
            "freshness",
            "send_button",
        )
    )

    def __init__(
        self,
        settings: Settings,
        coordinator: Optional[TicketRunCoordinator] = None,
        freshness_checker: Any | None = None,
        feishu_notifier: FeishuStatusNotifier | None = None,
    ):
        self._settings = settings
        self._coordinator = coordinator
        self._freshness_checker = freshness_checker
        self._feishu_notifier = feishu_notifier
        self._lock: Optional[asyncio.Lock] = None
        self._playwright_manager: Any | None = None
        self._playwright: Any | None = None
        self._context: Any | None = None
        self._context_started_at = 0.0
        self._context_uses = 0
        self._last_readiness_status: str | None = None
        self._last_notified_readiness_status: str | None = None
        self._auto_login_failures = 0
        self._next_auto_login_at = 0.0
        self._login_recovery_active = False

    async def check_session(self) -> SessionResponse:
        async with self._get_lock():
            try:
                result = await asyncio.wait_for(
                    self._with_page(self._check_session_page, count_use=True),
                    timeout=self._settings.zendesk_browser_operation_timeout_seconds,
                )
            except TimeoutError:
                await self._close_context(reason="operation_timeout")
                result = BrowserResult(
                    ok=False,
                    status="unknown",
                    error="operation_timeout",
                )
        return SessionResponse(
            ok=result.ok, status=result.status, current_url=result.current_url
        )

    async def check_readiness(self) -> SessionResponse:
        """Verify the session and independently recover expired authentication."""
        lock = self._get_lock()
        if lock.locked():
            if self._login_recovery_active:
                return SessionResponse(ok=False, status="login_required")
            # A send owns the single persistent page. Waiting here would make
            # the control plane mistake normal serialized browser work for a
            # failed dependency.
            return SessionResponse(ok=True, status="busy")
        async with lock:
            try:
                result = await asyncio.wait_for(
                    self._with_page(
                        self._check_and_recover_readiness_page,
                        count_use=False,
                    ),
                    timeout=self._settings.zendesk_browser_operation_timeout_seconds,
                )
            except TimeoutError:
                # Drop the timed-out context before another request can launch
                # a replacement on the same profile.
                await self._close_context(reason="readiness_timeout")
                result = BrowserResult(
                    ok=False,
                    status="unknown",
                    error="operation_timeout",
                )
            except Exception as exc:  # noqa: BLE001 - readiness must fail closed.
                logger.warning(
                    "browser_relay_readiness_failed error_type=%s",
                    type(exc).__name__,
                )
                result = BrowserResult(
                    ok=False, status="unknown", error=type(exc).__name__
                )
        await self._notify_readiness_transition(result.status)
        return SessionResponse(ok=result.ok, status=result.status)

    async def _check_and_recover_readiness_page(self, page: Page) -> BrowserResult:
        result = await self._check_readiness_page(page)
        if result.status == "logged_in":
            self._reset_auto_login_backoff()
            return result
        if result.status != "login_required":
            return result
        return await self._recover_login_if_due(page, observed=result)

    async def _recover_login_if_due(
        self,
        page: Page,
        *,
        observed: BrowserResult,
    ) -> BrowserResult:
        if (
            not self._settings.zendesk_session_supervisor_enabled
            or not self._settings.auto_login_configured
        ):
            return observed
        now = time.monotonic()
        if now < self._next_auto_login_at:
            return observed

        logger.warning(
            "browser_relay_auto_login_attempt attempt=%s",
            self._auto_login_failures + 1,
        )
        self._login_recovery_active = True
        try:
            try:
                login_result = await asyncio.wait_for(
                    self._try_auto_login(page),
                    timeout=self._settings.zendesk_auto_login_attempt_timeout_seconds,
                )
            except TimeoutError:
                login_result = BrowserResult(
                    ok=False,
                    status="login_required",
                    current_url=page.url,
                    error="TimeoutError",
                )
            if login_result.ok:
                verified = await self._check_readiness_page(page)
                if verified.ok and verified.status == "logged_in":
                    self._reset_auto_login_backoff()
                    logger.info("browser_relay_auto_login_recovered")
                    return verified
                login_result = verified
        finally:
            self._login_recovery_active = False

        self._auto_login_failures += 1
        initial = self._settings.zendesk_auto_login_initial_backoff_seconds
        maximum = max(
            initial,
            self._settings.zendesk_auto_login_max_backoff_seconds,
        )
        delay = min(initial * (2 ** (self._auto_login_failures - 1)), maximum)
        self._next_auto_login_at = now + delay
        logger.warning(
            "browser_relay_auto_login_failed attempt=%s retry_in_seconds=%s "
            "status=%s error_type=%s",
            self._auto_login_failures,
            delay,
            login_result.status,
            login_result.error or "none",
        )
        return login_result

    def _reset_auto_login_backoff(self) -> None:
        self._auto_login_failures = 0
        self._next_auto_login_at = 0.0

    async def send_message(self, request: SendRequest) -> SendResponse:
        async with self._get_lock():
            started = time.perf_counter()
            try:
                result, first_result, retried = await asyncio.wait_for(
                    self._send_message_with_optional_retry(request),
                    timeout=self._settings.zendesk_browser_operation_timeout_seconds,
                )
            except TimeoutError:
                await self._close_context(reason="send_timeout")
                result = BrowserResult(
                    ok=False,
                    status="send_failed",
                    error="operation_timeout",
                )
                first_result = result
                retried = False
            total_ms = round((time.perf_counter() - started) * 1000, 1)
            logger.info(
                "browser_relay_send_timing ticket_id=%s status=%s total_ms=%s "
                "reused_context=%s context_uses=%s retried=%s first_status=%s "
                "first_error=%s",
                request.ticket_id,
                result.status,
                total_ms,
                self._context_uses > 1,
                self._context_uses,
                retried,
                first_result.status if retried else None,
                first_result.error if retried else None,
            )
        await self._notify_send_status(request, result)
        return SendResponse(
            ok=result.ok,
            ticket_id=request.ticket_id,
            status=result.status,
            current_url=result.current_url,
            error=result.error,
        )

    async def _send_message_with_optional_retry(
        self,
        request: SendRequest,
    ) -> tuple[BrowserResult, BrowserResult, bool]:
        result = await self._send_message_once(request)
        first_result = result
        retried = False
        if self._should_retry_send(result):
            retried = True
            logger.warning(
                "browser_relay_send_retry ticket_id=%s status=%s error=%s",
                request.ticket_id,
                result.status,
                result.error,
            )
            await self._close_context(reason="send_retry")
            result = await self._send_message_once(request)
        return result, first_result, retried

    async def recycle_context_if_over_memory_limit(self) -> bool:
        limit_mb = self._settings.zendesk_browser_memory_limit_mb
        if limit_mb <= 0:
            return False
        rss_mb = profile_chromium_rss_mb(self._settings.zendesk_profile_dir)
        if rss_mb <= limit_mb:
            return False
        async with self._get_lock():
            if self._context is None:
                return False
            refreshed = profile_chromium_rss_mb(self._settings.zendesk_profile_dir)
            if refreshed <= limit_mb:
                return False
            await self._close_context(reason="memory_limit")
            return True

    async def _notify_send_status(self, request: SendRequest, result: BrowserResult) -> None:
        if self._feishu_notifier is None:
            return
        try:
            await asyncio.wait_for(
                self._feishu_notifier.notify_send(
                    ticket_id=request.ticket_id,
                    status=result.status,
                    body=request.body if result.status == "sent" else "",
                    error=result.error or "",
                ),
                timeout=6.0,
            )
        except Exception as exc:
            logger.warning(
                "feishu_status_notification_failed ticket_id=%s status=%s error_type=%s",
                request.ticket_id,
                result.status,
                type(exc).__name__,
            )

    async def _send_message_once(self, request: SendRequest) -> BrowserResult:
        return await self._with_page(
            lambda page: self._send_message_page(page, request)
        )

    def _should_retry_send(self, result: BrowserResult) -> bool:
        # _send_message_page already attempted configured auto-login. Repeating
        # the same invalid credentials in a fresh context only delays the
        # fail-closed response and can amplify account lockout risk.
        if result.status == "login_required":
            return False
        if result.status != "send_failed" or not result.error:
            return False
        _, separator, stage = result.error.partition(":")
        if not separator:
            return False
        return stage in self._RETRYABLE_SEND_FAILURE_STAGES

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def _with_page(self, operation, *, count_use: bool = True):
        context = await self._get_or_create_context()
        if count_use:
            self._context_uses += 1
        page = await context.new_page()
        try:
            result = operation(page)
            if inspect.isawaitable(result):
                return await result
            return result
        except Exception:
            if self._page_is_closed(page):
                await self._close_context(reason="page_closed")
            raise
        finally:
            await self._close_page(page)

    async def _get_or_create_context(self):
        self._settings.zendesk_profile_dir.mkdir(parents=True, exist_ok=True)
        if await self._should_rebuild_context():
            await self._close_context(reason="scheduled_recycle")
        if self._context is None:
            await self._launch_context()
        return self._context

    async def _ensure_playwright(self) -> None:
        if self._playwright is not None:
            return
        self._playwright_manager = async_playwright()
        self._playwright = await self._playwright_manager.start()

    async def _launch_context(self) -> None:
        started = time.perf_counter()
        await self._ensure_playwright()
        launch_options = {
            "user_data_dir": str(self._settings.zendesk_profile_dir),
            "headless": self._settings.zendesk_headless,
            "args": list(CHROMIUM_LAUNCH_ARGS),
        }
        if self._settings.zendesk_proxy_server:
            launch_options["proxy"] = {"server": self._settings.zendesk_proxy_server}
        self._context = await self._playwright.chromium.launch_persistent_context(
            **launch_options,
        )
        self._context.set_default_timeout(self._settings.zendesk_navigation_timeout_ms)
        await self._install_resource_blocking(self._context)
        for existing in list(self._context.pages):
            await self._close_page(existing)
        self._context_started_at = time.monotonic()
        self._context_uses = 0
        logger.info(
            "browser_relay_context_ready launch_context_ms=%s headless=%s",
            round((time.perf_counter() - started) * 1000, 1),
            self._settings.zendesk_headless,
        )

    async def _install_resource_blocking(self, context) -> None:
        async def handler(route, request):
            if request.resource_type in BLOCKED_RESOURCE_TYPES:
                await route.abort()
                return
            await route.continue_()

        await context.route("**/*", handler)

    async def _should_rebuild_context(self) -> bool:
        if self._context is None:
            return False
        if self._context_uses >= self._RECYCLE_AFTER_USES:
            return True
        if (
            self._context_started_at
            and time.monotonic() - self._context_started_at
            >= self._RECYCLE_AFTER_SECONDS
        ):
            return True
        return False

    def _page_is_closed(self, page: Page) -> bool:
        is_closed = getattr(page, "is_closed", None)
        if callable(is_closed):
            return bool(is_closed())
        return False

    async def _close_page(self, page: Page) -> None:
        if self._page_is_closed(page):
            return
        timeout = self._settings.zendesk_context_close_timeout_seconds
        try:
            await asyncio.wait_for(page.close(), timeout=timeout)
        except TimeoutError:
            logger.warning("browser_relay_page_close_timeout")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - close must not mask the caller.
            logger.warning(
                "browser_relay_page_close_failed error_type=%s",
                type(exc).__name__,
            )

    async def _stop_playwright(self, timeout: float) -> None:
        playwright = self._playwright
        manager = self._playwright_manager
        self._playwright = None
        self._playwright_manager = None
        try:
            stop = getattr(playwright, "stop", None)
        except AttributeError:
            stop = None
        try:
            if callable(stop):
                await asyncio.wait_for(stop(), timeout=timeout)
            elif manager is not None:
                exit_manager = getattr(manager, "__aexit__", None)
                if callable(exit_manager):
                    await exit_manager(None, None, None)
        except TimeoutError:
            logger.warning("browser_relay_playwright_stop_timeout")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - stopping the driver is best effort.
            logger.warning(
                "browser_relay_playwright_stop_failed error_type=%s",
                type(exc).__name__,
            )

    async def _close_context(
        self,
        *,
        reason: Literal[
            "shutdown",
            "scheduled_recycle",
            "page_closed",
            "operation_timeout",
            "readiness_timeout",
            "send_timeout",
            "send_retry",
            "memory_limit",
        ] = "scheduled_recycle",
        stop_playwright: bool = False,
    ) -> None:
        context = self._context
        self._context = None
        self._context_started_at = 0.0
        self._context_uses = 0
        timeout = self._settings.zendesk_context_close_timeout_seconds
        try:
            if context is not None:
                logger.info("browser_relay_context_close reason=%s", reason)
                try:
                    await asyncio.wait_for(context.close(), timeout=timeout)
                except TimeoutError:
                    logger.warning(
                        "browser_relay_context_close_timeout reason=%s",
                        reason,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - keep going to stop().
                    logger.warning(
                        "browser_relay_context_close_failed reason=%s error_type=%s",
                        reason,
                        type(exc).__name__,
                    )
        finally:
            if stop_playwright:
                await self._stop_playwright(timeout)

    async def close(self) -> None:
        async with self._get_lock():
            await self._close_context(reason="shutdown", stop_playwright=True)

    async def _check_session_page(self, page: Page) -> BrowserResult:
        dashboard_url = f"{self._settings.zendesk_base_url}/agent/dashboard"
        try:
            await page.goto(dashboard_url, wait_until="domcontentloaded")
            await page.wait_for_load_state("networkidle", timeout=10_000)
        except PlaywrightError:
            pass

        current_url = page.url
        if await self._is_login_required(page):
            login_result = await self._try_auto_login(page)
            if login_result.ok:
                return login_result
            return BrowserResult(
                ok=False, status="login_required", current_url=current_url
            )
        return BrowserResult(ok=True, status="logged_in", current_url=current_url)

    async def _check_readiness_page(self, page: Page) -> BrowserResult:
        dashboard_url = f"{self._settings.zendesk_base_url}/agent/dashboard"
        nav_timeout = self._settings.zendesk_readiness_navigation_timeout_ms
        try:
            await page.goto(
                dashboard_url,
                wait_until="domcontentloaded",
                timeout=nav_timeout,
            )
            await page.wait_for_load_state("domcontentloaded", timeout=5_000)
        except PlaywrightError:
            pass
        if await self._is_login_required(page):
            return BrowserResult(
                ok=False,
                status="login_required",
                current_url=page.url,
            )
        return BrowserResult(ok=True, status="logged_in", current_url=page.url)

    async def _notify_readiness_transition(self, status: str) -> None:
        previous = self._last_readiness_status
        self._last_readiness_status = status
        if self._feishu_notifier is None:
            return
        if previous is None and status == "logged_in":
            self._last_notified_readiness_status = status
            return
        if self._last_notified_readiness_status == status:
            return
        try:
            await asyncio.wait_for(
                self._feishu_notifier.notify_readiness(status=status),
                timeout=6.0,
            )
            self._last_notified_readiness_status = status
        except Exception as exc:  # noqa: BLE001 - notification is best effort.
            logger.warning(
                "feishu_readiness_notification_failed status=%s error_type=%s",
                status,
                type(exc).__name__,
            )

    async def _send_message_page(
        self, page: Page, request: SendRequest
    ) -> BrowserResult:
        ticket_url = (
            f"{self._settings.zendesk_base_url}/agent/tickets/{request.ticket_id}"
        )
        timings: dict[str, float] = {}
        total_started = time.perf_counter()
        try:
            started = time.perf_counter()
            await page.goto(ticket_url, wait_until="domcontentloaded")
            timings["goto_ms"] = round((time.perf_counter() - started) * 1000, 1)
        except PlaywrightError:
            pass

        if await self._is_login_required(page):
            login_result = await self._try_auto_login(page)
            if not login_result.ok:
                return BrowserResult(
                    ok=False, status="login_required", current_url=page.url
                )
            try:
                started = time.perf_counter()
                await page.goto(ticket_url, wait_until="domcontentloaded")
                timings["goto_after_login_ms"] = round(
                    (time.perf_counter() - started) * 1000, 1
                )
            except PlaywrightError:
                pass

        try:
            stage = "channel"
            started = time.perf_counter()
            await self._ensure_native_messaging_channel(page)
            timings["channel_ms"] = round((time.perf_counter() - started) * 1000, 1)
            stage = "composer"
            started = time.perf_counter()
            await self._ensure_native_composer_mode(page)
            composer = await self._first_visible(
                page, self._native_composer_selectors()
            )
            timings["composer_ms"] = round((time.perf_counter() - started) * 1000, 1)
            stage = "fill"
            started = time.perf_counter()
            await composer.click()
            await self._fill_composer_preserving_paragraphs(
                page,
                composer,
                request.body,
            )
            timings["fill_ms"] = round((time.perf_counter() - started) * 1000, 1)
            stage = "freshness"
            started = time.perf_counter()
            freshness = await self._check_freshness_before_send(request)
            timings["freshness_ms"] = round((time.perf_counter() - started) * 1000, 1)
            if freshness is not None and not freshness.ok:
                try:
                    await composer.fill("")
                except PlaywrightError:
                    pass
                timings["total_page_ms"] = round(
                    (time.perf_counter() - total_started) * 1000, 1
                )
                logger.info(
                    "browser_relay_send_page_timing ticket_id=%s status=%s timings=%s",
                    request.ticket_id,
                    freshness.status,
                    timings,
                )
                return freshness
            stage = "send_button"
            started = time.perf_counter()
            send_button = await self._first_visible_optional(
                page,
                self._send_selectors(),
                timeout=700,
            )
            timings["send_button_ms"] = round((time.perf_counter() - started) * 1000, 1)
            stage = "click"
            started = time.perf_counter()
            if send_button is not None:
                await send_button.click()
            else:
                await composer.press("Control+Enter")
            await page.wait_for_timeout(2_000)
            timings["click_ms"] = round((time.perf_counter() - started) * 1000, 1)
            timings["total_page_ms"] = round(
                (time.perf_counter() - total_started) * 1000, 1
            )
            stage = "confirm"
            if await self._locator_contains_text(composer, request.body):
                logger.warning(
                    "browser_relay_send_not_confirmed ticket_id=%s timings=%s",
                    request.ticket_id,
                    timings,
                )
                return BrowserResult(
                    ok=False,
                    status="send_not_confirmed",
                    current_url=page.url,
                    error="composer_still_contains_body",
                )
            logger.info(
                "browser_relay_send_page_timing ticket_id=%s status=sent timings=%s",
                request.ticket_id,
                timings,
            )
            return BrowserResult(ok=True, status="sent", current_url=page.url)
        except PlaywrightError as exc:
            timings["total_page_ms"] = round(
                (time.perf_counter() - total_started) * 1000, 1
            )
            error = f"{type(exc).__name__}:{stage}"
            logger.warning(
                "browser_relay_send_page_failed ticket_id=%s status=send_failed "
                "error=%s stage=%s message=%s timings=%s",
                request.ticket_id,
                type(exc).__name__,
                stage,
                str(exc)[:500],
                timings,
            )
            return BrowserResult(
                ok=False,
                status="send_failed",
                current_url=page.url,
                error=error,
            )

    async def _check_freshness_before_send(
        self,
        request: SendRequest,
    ) -> BrowserResult | None:
        """Ask zd-agent whether the reply is still fresh before clicking send."""
        if request.run_token is not None and self._coordinator is not None:
            assert request.snapshot_customer_last_message_id is not None
            assert request.snapshot_customer_message_count is not None
            local_check = self._coordinator.check(
                ticket_id=request.ticket_id,
                run_token=request.run_token,
                customer_last_message_id=request.snapshot_customer_last_message_id,
                customer_message_count=request.snapshot_customer_message_count,
            )
            if not local_check.is_current:
                return BrowserResult(
                    ok=False,
                    status="stale_context",
                    error=f"run_generation_{local_check.reason}"[:300],
                )

        if (
            self._freshness_checker is not None
            and request.snapshot_customer_message_count is not None
            and request.snapshot_customer_last_message_id is not None
        ):
            try:
                live_last_id, live_count = (
                    await self._freshness_checker.current_customer_cursor(
                        request.ticket_id
                    )
                )
            except Exception as exc:  # noqa: BLE001 - fail-closed boundary
                logger.warning(
                    "zendesk_mcp_freshness_failed ticket_id=%s error_type=%s",
                    request.ticket_id,
                    type(exc).__name__,
                )
                return BrowserResult(
                    ok=False,
                    status="freshness_check_failed",
                    error=type(exc).__name__,
                )
            cursor_matches = (
                live_last_id == request.snapshot_customer_last_message_id
                if live_last_id and request.snapshot_customer_last_message_id
                else (
                    live_last_id == request.snapshot_customer_last_message_id
                    and live_count == request.snapshot_customer_message_count
                )
            )
            if not cursor_matches:
                return BrowserResult(
                    ok=False,
                    status="stale_context",
                    error="live_customer_cursor_changed",
                )
            return None

        if self._settings.zendesk_mcp_url and self._freshness_checker is None:
            return BrowserResult(
                ok=False,
                status="freshness_check_failed",
                error="authorization_unavailable",
            )

        if (
            not request.freshness_check_url
            or not request.freshness_check_api_key
            or request.snapshot_customer_message_count is None
        ):
            return None

        payload = {
            "ticket_id": request.ticket_id,
            "snapshot_customer_message_count": request.snapshot_customer_message_count,
            "snapshot_customer_last_message_id": (
                request.snapshot_customer_last_message_id
            ),
        }
        headers = {"Authorization": f"Bearer {request.freshness_check_api_key}"}
        try:
            data = await asyncio.to_thread(
                self._post_freshness_check,
                request.freshness_check_url,
                headers,
                payload,
            )
        except (HTTPError, URLError, TimeoutError, ValueError) as exc:
            return BrowserResult(
                ok=False,
                status="freshness_check_failed",
                error=type(exc).__name__,
            )

        if bool(data.get("continue_send")):
            return None
        return BrowserResult(
            ok=False,
            status="stale_context",
            error=str(data.get("reason") or "stale_context")[:300],
        )

    def _post_freshness_check(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, object],
    ) -> dict:
        body = json.dumps(payload).encode("utf-8")
        req = Request(
            url,
            data=body,
            headers={**headers, "Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req, timeout=12) as response:
            raw = response.read().decode("utf-8")
        return json.loads(raw)

    async def _ensure_native_messaging_channel(self, page: Page) -> None:
        switcher = await self._first_visible_optional(
            page,
            (CHANNEL_SWITCHER_SELECTOR,),
            timeout=12_000,
        )
        if switcher is None:
            return

        current = (await switcher.inner_text()).strip().lower()
        if any(label in current for label in NATIVE_MESSAGING_LABELS):
            return

        await switcher.click()
        menu_item = page.locator(NATIVE_MESSAGING_MENU_ITEM_SELECTOR).first
        await menu_item.wait_for(state="visible", timeout=5_000)
        await menu_item.click()
        await page.wait_for_timeout(500)

    async def _ensure_native_composer_mode(self, page: Page) -> None:
        native_composer = await self._first_visible_optional(
            page,
            self._native_composer_selectors(),
            timeout=1_500,
        )
        if native_composer is not None:
            return

        switcher = await self._first_visible_optional(
            page,
            COMPOSER_MODE_SWITCHER_SELECTORS,
            timeout=2_000,
        )
        if switcher is not None:
            await switcher.click()
            item = await self._first_visible_optional(
                page,
                COMPOSER_MODE_MESSAGING_SELECTORS,
                timeout=3_000,
            )
            if item is not None:
                await item.click()
                await page.wait_for_timeout(500)

    async def _is_login_required(self, page: Page) -> bool:
        lowered_url = page.url.lower()
        if any(marker in lowered_url for marker in LOGIN_URL_MARKERS):
            return True
        password_inputs = await page.locator('input[type="password"]').count()
        email_inputs = await page.locator('input[type="email"]').count()
        return password_inputs > 0 or email_inputs > 0

    async def _try_auto_login(self, page: Page) -> BrowserResult:
        try:
            acted = False
            acted = await self._submit_credentials_if_present(page) or acted
            acted = await self._submit_totp_if_present(page) or acted
            if acted:
                try:
                    await page.wait_for_load_state("networkidle", timeout=15_000)
                except PlaywrightError:
                    pass
                await page.wait_for_timeout(1_000)

            if not acted or await self._is_login_required(page):
                return BrowserResult(
                    ok=False,
                    status="login_required",
                    current_url=page.url,
                    error="auto_login_incomplete",
                )
            return BrowserResult(ok=True, status="logged_in", current_url=page.url)
        except PlaywrightError as exc:
            return BrowserResult(
                ok=False,
                status="login_required",
                current_url=page.url,
                error=type(exc).__name__,
            )

    async def _submit_credentials_if_present(self, page: Page) -> bool:
        email = self._settings.zendesk_login_email
        password = self._settings.login_password_value
        if not email or not password:
            return False

        acted = False
        email_input = await self._first_visible_optional(
            page, EMAIL_INPUT_SELECTORS, timeout=2_000
        )
        password_input = await self._first_visible_optional(
            page,
            PASSWORD_INPUT_SELECTORS,
            timeout=2_000,
        )

        if email_input is not None and password_input is not None:
            await email_input.fill(email)
            await password_input.fill(password)
            await self._click_submit_if_present(page)
            await page.wait_for_timeout(2_000)
            return True

        if email_input is not None:
            await email_input.fill(email)
            await self._click_submit_if_present(page)
            await page.wait_for_timeout(1_000)
            acted = True

        password_input = await self._first_visible_optional(
            page, PASSWORD_INPUT_SELECTORS, timeout=5_000
        )
        if password_input is not None:
            await password_input.fill(password)
            await self._click_submit_if_present(page)
            await page.wait_for_timeout(2_000)
            acted = True

        return acted

    async def _submit_totp_if_present(self, page: Page) -> bool:
        secret = self._settings.totp_secret_value
        if not secret:
            return False

        otp_input = await self._first_visible_optional(
            page, OTP_INPUT_SELECTORS, timeout=5_000
        )
        if otp_input is None and "/mfa" in page.url.lower():
            otp_input = await self._first_visible_input(page, timeout=5_000)
        if otp_input is None:
            return False

        remaining = 30 - (int(time.time()) % 30)
        if remaining <= 5:
            await page.wait_for_timeout((remaining + 1) * 1000)

        await otp_input.fill(generate_totp_code(secret))
        await self._click_submit_if_present(page)
        await page.wait_for_timeout(2_000)
        return True

    async def _click_submit_if_present(self, page: Page) -> bool:
        submit = await self._first_visible_optional(
            page, SUBMIT_SELECTORS, timeout=2_000
        )
        if submit is None:
            return False
        await submit.click()
        return True

    def _composer_selectors(self) -> tuple[str, ...]:
        if self._settings.zendesk_composer_selector:
            return (
                self._settings.zendesk_composer_selector,
                *DEFAULT_COMPOSER_SELECTORS,
            )
        return DEFAULT_COMPOSER_SELECTORS

    def _native_composer_selectors(self) -> tuple[str, ...]:
        if self._settings.zendesk_composer_selector:
            return (
                self._settings.zendesk_composer_selector,
                DEFAULT_COMPOSER_SELECTORS[0],
            )
        return (DEFAULT_COMPOSER_SELECTORS[0],)

    def _send_selectors(self) -> tuple[str, ...]:
        if self._settings.zendesk_send_selector:
            return (self._settings.zendesk_send_selector, *DEFAULT_SEND_SELECTORS)
        return DEFAULT_SEND_SELECTORS

    async def _fill_composer_preserving_paragraphs(
        self,
        page: Page,
        composer,
        body: str,
    ) -> None:
        is_contenteditable = bool(
            await composer.evaluate("element => element.isContentEditable")
        )
        if not is_contenteditable:
            await composer.fill(body)
            return

        await composer.fill("")
        normalized_body = body.replace("\r\n", "\n").replace("\r", "\n")
        for part in re.split(r"(\n+)", normalized_body):
            if not part:
                continue
            if not part.startswith("\n"):
                await page.keyboard.insert_text(part)
                continue
            for _ in part:
                await page.keyboard.press("Shift+Enter")

    async def _first_visible(self, page: Page, selectors: tuple[str, ...]):
        last_error: Optional[PlaywrightError] = None
        for selector in selectors:
            locator = page.locator(selector).first
            try:
                await locator.wait_for(state="visible", timeout=5_000)
                return locator
            except PlaywrightError as exc:
                last_error = exc
        if last_error:
            raise last_error
        raise PlaywrightError("No selectors configured")

    async def _first_visible_optional(
        self,
        page: Page,
        selectors: tuple[str, ...],
        *,
        timeout: int,
    ):
        deadline = time.monotonic() + timeout / 1_000
        for selector in selectors:
            remaining_ms = int((deadline - time.monotonic()) * 1_000)
            if remaining_ms <= 0:
                break
            locator = page.locator(selector).first
            try:
                await locator.wait_for(state="visible", timeout=remaining_ms)
                return locator
            except PlaywrightError:
                continue
        return None

    async def _first_visible_input(self, page: Page, *, timeout: int):
        deadline = time.monotonic() + timeout / 1000
        while time.monotonic() < deadline:
            inputs = page.locator("input:not([type='hidden'])")
            for index in range(await inputs.count()):
                locator = inputs.nth(index)
                try:
                    if await locator.is_visible():
                        return locator
                except PlaywrightError:
                    continue
            await page.wait_for_timeout(200)
        return None

    async def _locator_contains_text(self, locator, text: str) -> bool:
        try:
            return text in (await locator.inner_text())
        except PlaywrightError:
            return False


def profile_chromium_rss_mb(profile_dir: Path) -> float:
    needle = str(profile_dir.resolve())
    total_kb = 0
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return 0.0
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(
                "utf-8",
                errors="ignore",
            )
            if needle not in cmdline:
                continue
            for line in (entry / "status").read_text(encoding="utf-8").splitlines():
                if line.startswith("VmRSS:"):
                    total_kb += int(line.split()[1])
                    break
        except (OSError, ValueError):
            continue
    return total_kb / 1024.0
