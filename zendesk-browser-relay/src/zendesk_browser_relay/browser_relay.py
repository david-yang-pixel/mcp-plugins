from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, async_playwright

from .coordination import TicketRunCoordinator
from .feishu_notifications import FeishuStatusNotifier
from .login_automation import generate_totp_code
from .schemas import SendRequest, SendResponse, SessionResponse
from .settings import Settings

logger = logging.getLogger("uvicorn.error")

# Images are disabled through Blink settings instead of context.route(): a
# route on "**/*" disables the HTTP cache, so every new page would download
# the full Zendesk bundle again.
CHROMIUM_LAUNCH_ARGS = (
    "--no-sandbox",
    "--disable-gpu",
    "--blink-settings=imagesEnabled=false",
)
# Playwright adds --disable-dev-shm-usage by default. The container contract
# sizes /dev/shm (shm_size), so let Chrome use it.
CHROMIUM_IGNORED_DEFAULT_ARGS = ("--disable-dev-shm-usage",)

SEND_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH"})
SESSION_COOKIE_FILE = "relay-session-cookies.json"
SESSION_COOKIE_SAVE_INTERVAL_SECONDS = 600
PROFILE_SINGLETON_FILES = ("SingletonLock", "SingletonSocket", "SingletonCookie")

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


@dataclass
class _SendAttempt:
    """Progress of one send attempt, readable after it is cancelled."""

    stage: str = "goto"
    clicked: bool = False


@dataclass
class _WriteRequestTracker:
    """Follow the Zendesk write requests a send click starts on one page."""

    base_url: str
    pattern: Optional[re.Pattern[str]] = None
    pending: set[Any] = field(default_factory=set)
    succeeded: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    changed: asyncio.Event = field(default_factory=asyncio.Event)

    def matches(self, request) -> bool:
        if request.method.upper() not in SEND_WRITE_METHODS:
            return False
        if not request.url.startswith(self.base_url + "/"):
            return False
        return self.pattern is None or bool(self.pattern.search(request.url))

    def on_request(self, request) -> None:
        if self.matches(request):
            self.pending.add(request)
            self.changed.set()

    def on_response(self, response) -> None:
        request = response.request
        if request not in self.pending:
            return
        self.pending.discard(request)
        target = self.succeeded if 200 <= response.status < 300 else self.failed
        target.append(f"{request.method} {_url_path(request.url)} {response.status}")
        self.changed.set()

    def on_request_failed(self, request) -> None:
        if request not in self.pending:
            return
        self.pending.discard(request)
        self.failed.append(f"{request.method} {_url_path(request.url)} failed")
        self.changed.set()

    def attach(self, page) -> None:
        page.on("request", self.on_request)
        page.on("response", self.on_response)
        page.on("requestfailed", self.on_request_failed)

    def detach(self, page) -> None:
        for event, handler in (
            ("request", self.on_request),
            ("response", self.on_response),
            ("requestfailed", self.on_request_failed),
        ):
            try:
                page.remove_listener(event, handler)
            except Exception:  # noqa: BLE001 - the page may already be gone.
                pass

    async def settle(self, timeout_seconds: float, quiet_seconds: float = 1.0) -> str:
        """Wait for the click's writes to finish and stay quiet briefly.

        Returns confirmed, failed (none succeeded), mixed, pending, or none.
        The quiet period keeps an early unrelated request from deciding the
        outcome before the reply request has started.
        """
        deadline = time.monotonic() + timeout_seconds
        while True:
            settled = bool(self.succeeded or self.failed) and not self.pending
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if settled:
                    return self._verdict()
                return "pending" if self.pending else "none"
            self.changed.clear()
            wait = min(remaining, quiet_seconds) if settled else remaining
            try:
                await asyncio.wait_for(self.changed.wait(), timeout=wait)
            except TimeoutError:
                if settled:
                    return self._verdict()

    def _verdict(self) -> str:
        if self.failed:
            return "mixed" if self.succeeded else "failed"
        return "confirmed"


def _url_path(url: str) -> str:
    return urlsplit(url).path


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
        freshness_checker_factory: Callable[[], Any | None] | None = None,
    ):
        self._settings = settings
        self._coordinator = coordinator
        self._freshness_checker = freshness_checker
        # Resolved on first send so a bad authorization file only blocks
        # sends instead of the whole service.
        self._freshness_checker_factory = freshness_checker_factory
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
        self._cookies_saved_at = 0.0
        self._cpu_sample: tuple[float, float] | None = None
        self._cpu_high_samples = 0
        self._closing = False

    async def check_session(self) -> SessionResponse:
        async with self._get_lock():
            try:
                result = await asyncio.wait_for(
                    self._check_session(),
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
                    self._check_and_recover_readiness(),
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

    async def _check_session(self) -> BrowserResult:
        result = await self._check_login_status()
        if result.status != "login_required":
            return result
        login_result = await self._with_page(self._open_login_and_submit)
        if not login_result.ok:
            return BrowserResult(
                ok=False,
                status="login_required",
                current_url=login_result.current_url,
                error=login_result.error,
            )
        return await self._check_login_status()

    async def _check_and_recover_readiness(self) -> BrowserResult:
        result = await self._check_login_status()
        if result.status == "logged_in":
            self._reset_auto_login_backoff()
            return result
        if result.status != "login_required":
            return result
        return await self._recover_login_if_due(observed=result)

    async def _recover_login_if_due(
        self,
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
                    self._with_page(self._open_login_and_submit, count_use=False),
                    timeout=self._settings.zendesk_auto_login_attempt_timeout_seconds,
                )
            except TimeoutError:
                login_result = BrowserResult(
                    ok=False,
                    status="login_required",
                    error="TimeoutError",
                )
            if login_result.ok:
                verified = await self._check_login_status()
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
            result, first_result, retried = (
                await self._send_message_with_optional_retry(request)
            )
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

    async def recycle_context_if_over_resource_limit(self) -> str | None:
        """Recycle the browser when it uses too much memory or idles hot.

        Returns the recycle reason, or None when the browser was left alone.
        """
        profile_dir = self._settings.zendesk_profile_dir
        if self._memory_over_limit(profile_dir) and await self._recycle_if(
            lambda: self._memory_over_limit(profile_dir),
            reason="memory_limit",
        ):
            return "memory_limit"
        if self._cpu_over_limit(profile_dir) and await self._recycle_if(
            lambda: True,
            reason="cpu_limit",
        ):
            return "cpu_limit"
        return None

    async def _recycle_if(self, still_needed: Callable[[], bool], *, reason) -> bool:
        async with self._get_lock():
            if self._context is None or not still_needed():
                return False
            await self._close_context(reason=reason)
            return True

    def _memory_over_limit(self, profile_dir: Path) -> bool:
        limit_mb = self._settings.zendesk_browser_memory_limit_mb
        if limit_mb <= 0:
            return False
        return profile_chromium_memory_mb(profile_dir) > limit_mb

    def _cpu_over_limit(self, profile_dir: Path) -> bool:
        """Count consecutive idle samples where Chrome stays above the limit.

        Samples taken while a browser operation holds the lock reset the
        streak: a send legitimately burns CPU, an idle workspace should not.
        """
        limit = self._settings.zendesk_browser_cpu_limit_percent
        if limit <= 0:
            return False
        now = time.monotonic()
        cpu_seconds = profile_chromium_cpu_seconds(profile_dir)
        previous = self._cpu_sample
        self._cpu_sample = (now, cpu_seconds)
        if previous is None or self._get_lock().locked() or self._context is None:
            self._cpu_high_samples = 0
            return False
        wall = now - previous[0]
        used = cpu_seconds - previous[1]
        if wall <= 0 or used < 0:
            # Processes were replaced between samples.
            self._cpu_high_samples = 0
            return False
        if used / wall * 100 <= limit:
            self._cpu_high_samples = 0
            return False
        self._cpu_high_samples += 1
        if self._cpu_high_samples < self._settings.zendesk_browser_cpu_high_samples:
            return False
        self._cpu_high_samples = 0
        self._cpu_sample = None
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
        """Run one send attempt under its own timeout.

        Once the send control has been clicked the reply may already be
        stored in Zendesk, so any later timeout or error is reported as
        send_unknown. Callers must not retry that status automatically.
        """
        attempt = _SendAttempt()
        try:
            return await asyncio.wait_for(
                self._with_page(
                    lambda page: self._send_message_page(page, request, attempt)
                ),
                timeout=self._settings.zendesk_browser_operation_timeout_seconds,
            )
        except TimeoutError:
            await self._close_context(reason="send_timeout")
            error = f"operation_timeout:{attempt.stage}"
        except Exception as exc:  # noqa: BLE001 - see send_unknown below.
            if not attempt.clicked:
                raise
            error = f"{type(exc).__name__}:{attempt.stage}"
        logger.warning(
            "browser_relay_send_attempt_aborted ticket_id=%s clicked=%s error=%s",
            request.ticket_id,
            attempt.clicked,
            error,
        )
        if attempt.clicked:
            return BrowserResult(ok=False, status="send_unknown", error=error)
        return BrowserResult(ok=False, status="send_failed", error=error)

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
        if self._closing:
            # An abandoned send must not relaunch Chrome during shutdown.
            raise RuntimeError("browser relay is shutting down")
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
        # Nothing should hold the profile while no context is open. Leftovers
        # from a crashed or unclosable browser would block the new launch.
        await self._kill_profile_processes(reason="prelaunch")
        try:
            context = await self._launch_persistent_context()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - retried once on a new driver.
            # A dead Playwright driver fails every launch until it is replaced.
            logger.warning(
                "browser_relay_launch_failed error_type=%s restarting_driver=true",
                type(exc).__name__,
            )
            await self._stop_playwright(
                self._settings.zendesk_context_close_timeout_seconds
            )
            await self._kill_profile_processes(reason="launch_failed")
            context = await self._launch_persistent_context()
        self._context = context
        context.on("close", lambda _closed: self._forget_context(context))
        context.set_default_timeout(self._settings.zendesk_navigation_timeout_ms)
        for existing in list(context.pages):
            await self._close_page(existing)
        await self._restore_session_cookies(context)
        self._context_started_at = time.monotonic()
        self._context_uses = 0
        logger.info(
            "browser_relay_context_ready launch_context_ms=%s headless=%s",
            round((time.perf_counter() - started) * 1000, 1),
            self._settings.zendesk_headless,
        )

    async def _launch_persistent_context(self):
        await self._ensure_playwright()
        launch_options = {
            "user_data_dir": str(self._settings.zendesk_profile_dir),
            "headless": self._settings.zendesk_headless,
            "args": list(CHROMIUM_LAUNCH_ARGS),
            "ignore_default_args": list(CHROMIUM_IGNORED_DEFAULT_ARGS),
        }
        if self._settings.zendesk_proxy_server:
            launch_options["proxy"] = {"server": self._settings.zendesk_proxy_server}
        return await self._playwright.chromium.launch_persistent_context(
            **launch_options,
        )

    def _forget_context(self, context) -> None:
        """Drop a context Chrome closed on its own, e.g. after a crash."""
        if self._context is not context:
            return
        logger.warning("browser_relay_context_disconnected")
        self._context = None
        self._context_started_at = 0.0
        self._context_uses = 0

    @property
    def _session_cookie_path(self) -> Path:
        return self._settings.zendesk_profile_dir / SESSION_COOKIE_FILE

    async def _save_session_cookies(self, *, force: bool = False) -> None:
        """Keep Zendesk session cookies that Chrome drops on every restart.

        Zendesk issues its agent session as a session cookie, which a
        persistent profile does not restore after the browser process exits.
        """
        context = self._context
        if context is None:
            return
        now = time.monotonic()
        if (
            not force
            and self._cookies_saved_at
            and now - self._cookies_saved_at < SESSION_COOKIE_SAVE_INTERVAL_SECONDS
        ):
            return
        try:
            cookies = [
                cookie
                for cookie in await context.cookies(self._settings.zendesk_base_url)
                if cookie.get("expires", -1) == -1
            ]
            path = self._session_cookie_path
            temporary = path.with_name(path.name + ".tmp")
            descriptor = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(cookies, handle)
            os.replace(temporary, path)
            self._cookies_saved_at = now
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - persistence is best effort.
            logger.warning(
                "browser_relay_session_cookie_save_failed error_type=%s",
                type(exc).__name__,
            )

    async def _restore_session_cookies(self, context) -> None:
        path = self._session_cookie_path
        if not path.is_file():
            return
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            current = {
                (cookie.get("name"), cookie.get("domain"), cookie.get("path"))
                for cookie in await context.cookies(self._settings.zendesk_base_url)
            }
            # Never overwrite cookies the profile already holds; they are newer.
            missing = [
                cookie
                for cookie in saved
                if isinstance(cookie, dict)
                and (cookie.get("name"), cookie.get("domain"), cookie.get("path"))
                not in current
            ]
            if missing:
                await context.add_cookies(missing)
                logger.info("browser_relay_session_cookies_restored count=%s", len(missing))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a bad snapshot must not block launch.
            logger.warning(
                "browser_relay_session_cookie_restore_failed error_type=%s",
                type(exc).__name__,
            )

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
            "cpu_limit",
        ] = "scheduled_recycle",
        stop_playwright: bool = False,
        timeout: float | None = None,
    ) -> None:
        context = self._context
        self._context = None
        self._context_started_at = 0.0
        self._context_uses = 0
        if timeout is None:
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
                    await self._kill_profile_processes(reason=reason)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - keep going to stop().
                    logger.warning(
                        "browser_relay_context_close_failed reason=%s error_type=%s",
                        reason,
                        type(exc).__name__,
                    )
                    await self._kill_profile_processes(reason=reason)
        finally:
            if stop_playwright:
                await self._stop_playwright(timeout)

    async def _kill_profile_processes(self, *, reason: str) -> int:
        """SIGKILL Chrome processes still holding the profile directory."""
        profile_dir = self._settings.zendesk_profile_dir
        pids = profile_chromium_pids(profile_dir)
        if not pids:
            return 0
        killed = 0
        for pid in sorted(pids):
            try:
                os.kill(pid, signal.SIGKILL)
                killed += 1
            except ProcessLookupError:
                continue
            except PermissionError:
                logger.warning("browser_relay_kill_denied pid=%s", pid)
        deadline = time.monotonic() + 5
        while profile_chromium_pids(profile_dir) and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        # Chrome refuses a profile whose Singleton lock names a dead process.
        for name in PROFILE_SINGLETON_FILES:
            try:
                (profile_dir / name).unlink(missing_ok=True)
            except OSError:
                pass
        logger.warning(
            "browser_relay_profile_processes_killed reason=%s count=%s",
            reason,
            killed,
        )
        return killed

    async def close(self) -> None:
        """Release the browser within the deployer's stop window.

        A send that still holds the lock after the grace period is abandoned:
        its context is closed underneath it and Chrome is killed if needed.
        """
        self._closing = True
        lock = self._get_lock()
        acquired = False
        try:
            await asyncio.wait_for(
                lock.acquire(),
                timeout=self._settings.zendesk_shutdown_lock_timeout_seconds,
            )
            acquired = True
        except TimeoutError:
            logger.warning("browser_relay_shutdown_lock_timeout")
        try:
            await self._close_context(
                reason="shutdown",
                stop_playwright=True,
                timeout=min(10, self._settings.zendesk_context_close_timeout_seconds),
            )
        finally:
            if acquired:
                lock.release()

    async def _check_login_status(self) -> BrowserResult:
        """Ask the Zendesk API who the browser session belongs to.

        This reuses the context cookies without loading the agent workspace.
        Anything other than a clear answer is reported as unknown, never as
        logged in.
        """
        context = await self._get_or_create_context()
        url = f"{self._settings.zendesk_base_url}/api/v2/users/me.json"
        try:
            response = await context.request.get(
                url,
                timeout=self._settings.zendesk_readiness_navigation_timeout_ms,
                fail_on_status_code=False,
            )
        except PlaywrightError as exc:
            return BrowserResult(ok=False, status="unknown", error=type(exc).__name__)
        try:
            if response.status in (401, 403):
                return BrowserResult(ok=False, status="login_required")
            if response.status != 200:
                return BrowserResult(
                    ok=False, status="unknown", error=f"http_{response.status}"
                )
            try:
                payload = await response.json()
            except (PlaywrightError, ValueError) as exc:
                return BrowserResult(
                    ok=False, status="unknown", error=type(exc).__name__
                )
        finally:
            try:
                await response.dispose()
            except PlaywrightError:
                pass
        user = payload.get("user") if isinstance(payload, dict) else None
        if not isinstance(user, dict):
            return BrowserResult(ok=False, status="unknown", error="invalid_user_payload")
        # Anonymous visitors get a user with a null id; end users are not
        # allowed to reply as agents.
        if type(user.get("id")) is int and user.get("role") in ("agent", "admin"):
            await self._save_session_cookies()
            return BrowserResult(ok=True, status="logged_in")
        return BrowserResult(ok=False, status="login_required")

    async def _open_login_and_submit(self, page: Page) -> BrowserResult:
        dashboard_url = f"{self._settings.zendesk_base_url}/agent/dashboard"
        try:
            await page.goto(
                dashboard_url,
                wait_until="domcontentloaded",
                timeout=self._settings.zendesk_readiness_navigation_timeout_ms,
            )
        except PlaywrightError:
            pass
        result = await self._try_auto_login(page)
        if result.ok:
            await self._save_session_cookies(force=True)
        return result

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
        self,
        page: Page,
        request: SendRequest,
        attempt: _SendAttempt | None = None,
    ) -> BrowserResult:
        attempt = attempt or _SendAttempt()
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
            attempt.stage = "login"
            try:
                login_result = await asyncio.wait_for(
                    self._try_auto_login(page),
                    timeout=self._settings.zendesk_auto_login_attempt_timeout_seconds,
                )
            except TimeoutError:
                login_result = BrowserResult(
                    ok=False, status="login_required", error="TimeoutError"
                )
            if not login_result.ok:
                return BrowserResult(
                    ok=False, status="login_required", current_url=page.url
                )
            await self._save_session_cookies(force=True)
            attempt.stage = "goto_after_login"
            try:
                started = time.perf_counter()
                await page.goto(ticket_url, wait_until="domcontentloaded")
                timings["goto_after_login_ms"] = round(
                    (time.perf_counter() - started) * 1000, 1
                )
            except PlaywrightError:
                pass

        tracker: _WriteRequestTracker | None = None
        stage = attempt.stage
        try:
            stage = attempt.stage = "channel"
            started = time.perf_counter()
            await self._ensure_native_messaging_channel(page)
            timings["channel_ms"] = round((time.perf_counter() - started) * 1000, 1)
            stage = attempt.stage = "composer"
            started = time.perf_counter()
            await self._ensure_native_composer_mode(page)
            composer = await self._first_visible(
                page, self._native_composer_selectors()
            )
            timings["composer_ms"] = round((time.perf_counter() - started) * 1000, 1)
            stage = attempt.stage = "fill"
            started = time.perf_counter()
            await composer.click()
            await self._fill_composer_preserving_paragraphs(
                page,
                composer,
                request.body,
            )
            timings["fill_ms"] = round((time.perf_counter() - started) * 1000, 1)
            stage = attempt.stage = "freshness"
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
            stage = attempt.stage = "send_button"
            started = time.perf_counter()
            send_button = await self._first_visible_optional(
                page,
                self._send_selectors(),
                timeout=700,
            )
            timings["send_button_ms"] = round((time.perf_counter() - started) * 1000, 1)
            stage = attempt.stage = "click"
            tracker = _WriteRequestTracker(
                base_url=self._settings.zendesk_base_url,
                pattern=self._send_confirm_pattern(request.ticket_id),
            )
            tracker.attach(page)
            started = time.perf_counter()
            # From here on the reply may reach the customer even if this
            # attempt fails or times out.
            attempt.clicked = True
            if send_button is not None:
                await send_button.click()
            else:
                await composer.press("Control+Enter")
            timings["click_ms"] = round((time.perf_counter() - started) * 1000, 1)
            stage = attempt.stage = "confirm"
            started = time.perf_counter()
            # Keep the page open until the write that stores the reply has
            # returned; closing it earlier can abort the request.
            writes = await tracker.settle(
                self._settings.zendesk_send_confirm_timeout_ms / 1_000
            )
            body_remains = await self._locator_contains_text(composer, request.body)
            timings["confirm_ms"] = round((time.perf_counter() - started) * 1000, 1)
            timings["total_page_ms"] = round(
                (time.perf_counter() - total_started) * 1000, 1
            )
            result = self._send_outcome(writes, body_remains, page.url)
            log = logger.info if result.ok else logger.warning
            log(
                "browser_relay_send_page_timing ticket_id=%s status=%s error=%s "
                "writes=%s succeeded=%s failed=%s timings=%s",
                request.ticket_id,
                result.status,
                result.error,
                writes,
                tracker.succeeded,
                tracker.failed,
                timings,
            )
            return result
        except PlaywrightError as exc:
            timings["total_page_ms"] = round(
                (time.perf_counter() - total_started) * 1000, 1
            )
            error = f"{type(exc).__name__}:{stage}"
            status = "send_unknown" if attempt.clicked else "send_failed"
            logger.warning(
                "browser_relay_send_page_failed ticket_id=%s status=%s "
                "error=%s stage=%s message=%s timings=%s",
                request.ticket_id,
                status,
                type(exc).__name__,
                stage,
                str(exc)[:500],
                timings,
            )
            return BrowserResult(
                ok=False,
                status=status,
                current_url=page.url,
                error=error,
            )
        finally:
            if tracker is not None:
                tracker.detach(page)

    def _send_confirm_pattern(self, ticket_id: int) -> re.Pattern[str] | None:
        pattern = self._settings.zendesk_send_confirm_url_pattern
        if not pattern:
            return None
        return re.compile(pattern.replace("{ticket_id}", str(ticket_id)))

    def _send_outcome(
        self,
        writes: str,
        body_remains: bool,
        current_url: str,
    ) -> BrowserResult:
        """Map the observed write requests and composer state to a status.

        Only a settled, successful write plus a cleared composer is "sent".
        A rejected write with the body still in the composer means Zendesk
        did not take the reply. Every other combination is send_unknown.
        """
        if writes == "confirmed" and not body_remains:
            return BrowserResult(ok=True, status="sent", current_url=current_url)
        if writes == "failed" and body_remains:
            return BrowserResult(
                ok=False,
                status="send_failed",
                current_url=current_url,
                error="write_request_failed:confirm",
            )
        if writes == "none" and body_remains:
            return BrowserResult(
                ok=False,
                status="send_not_confirmed",
                current_url=current_url,
                error="composer_still_contains_body",
            )
        error = {
            "confirmed": "composer_still_contains_body",
            "failed": "write_request_failed",
            "mixed": "write_request_failed",
            "pending": "write_request_pending",
            "none": "no_write_request_observed",
        }[writes]
        return BrowserResult(
            ok=False,
            status="send_unknown",
            current_url=current_url,
            error=error + ":confirm",
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

        freshness_checker = self._resolve_freshness_checker()
        if (
            freshness_checker is not None
            and request.snapshot_customer_message_count is not None
            and request.snapshot_customer_last_message_id is not None
        ):
            try:
                live_last_id, live_count = (
                    await freshness_checker.current_customer_cursor(
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

        if self._settings.zendesk_mcp_url and freshness_checker is None:
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

    def _resolve_freshness_checker(self) -> Any | None:
        if self._freshness_checker is None and self._freshness_checker_factory:
            self._freshness_checker = self._freshness_checker_factory()
        return self._freshness_checker

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


def _read_process(entry: Path) -> tuple[int, list[str]] | None:
    """Return (parent pid, argv) for one /proc entry, or None if it is gone."""
    try:
        argv = [
            arg.decode("utf-8", errors="ignore")
            for arg in (entry / "cmdline").read_bytes().split(b"\0")
            if arg
        ]
        stat = (entry / "stat").read_text(encoding="utf-8")
    except OSError:
        return None
    # The command name may contain spaces or parentheses; fields after it don't.
    fields = stat.rpartition(")")[2].split()
    try:
        return int(fields[1]), argv
    except (IndexError, ValueError):
        return None


def profile_chromium_pids(profile_dir: Path, proc_root: Path = Path("/proc")) -> set[int]:
    """Return Chrome processes started on the profile, plus their children."""
    if not proc_root.is_dir():
        return set()
    needles = {str(profile_dir), str(profile_dir.resolve())}
    own_pid = os.getpid()
    children: dict[int, list[int]] = {}
    roots: set[int] = set()
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        process = _read_process(entry)
        if process is None or pid == own_pid:
            continue
        parent, argv = process
        children.setdefault(parent, []).append(pid)
        if any(
            arg.startswith("--user-data-dir=")
            and arg.removeprefix("--user-data-dir=") in needles
            for arg in argv
        ):
            roots.add(pid)
    found: set[int] = set()
    queue = list(roots)
    while queue:
        pid = queue.pop()
        if pid in found:
            continue
        found.add(pid)
        queue.extend(children.get(pid, ()))
    return found


def _process_memory_kb(entry: Path) -> int:
    # PSS splits shared pages between processes, so summing it does not count
    # Chrome's shared libraries and shared memory once per renderer like RSS.
    for name, key in (("smaps_rollup", "Pss:"), ("status", "VmRSS:")):
        try:
            for line in (entry / name).read_text(encoding="utf-8").splitlines():
                if line.startswith(key):
                    return int(line.split()[1])
        except (OSError, ValueError, IndexError):
            continue
    return 0


def profile_chromium_memory_mb(
    profile_dir: Path,
    proc_root: Path = Path("/proc"),
) -> float:
    total_kb = sum(
        _process_memory_kb(proc_root / str(pid))
        for pid in profile_chromium_pids(profile_dir, proc_root)
    )
    return total_kb / 1024.0


def profile_chromium_cpu_seconds(
    profile_dir: Path,
    proc_root: Path = Path("/proc"),
) -> float:
    ticks = 0
    for pid in profile_chromium_pids(profile_dir, proc_root):
        try:
            stat = (proc_root / str(pid) / "stat").read_text(encoding="utf-8")
            fields = stat.rpartition(")")[2].split()
            # utime and stime are fields 14 and 15 of /proc/<pid>/stat.
            ticks += int(fields[11]) + int(fields[12])
        except (OSError, ValueError, IndexError):
            continue
    return ticks / os.sysconf("SC_CLK_TCK")
