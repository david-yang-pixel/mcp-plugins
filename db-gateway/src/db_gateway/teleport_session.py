"""db-gateway's own Teleport session: no host `.tsh`, no shadow copy.

Layout under ``teleport.sessions_dir``::

    gen-<N>/home/         TELEPORT_HOME of login N
    gen-<N>/kubeconfig    KUBECONFIG written by `tsh kube login --all` in login N
    current -> gen-<N>    the live login (relative symlink)

A renewal logs into a brand-new ``gen-<N+1>`` (password + TOTP, then the kube and
AWS app logins), reads the certificate lifetime from `tsh status`, and only then
switches ``current`` atomically (``os.replace`` of a symlink). Nothing ever writes
into a home that is in use:

* long-running tsh processes (DB tunnels) run with ``TELEPORT_HOME=current/home``;
  measured on the server, a running `tsh proxy db --tunnel` follows the switch and
  keeps opening connections after the certificate it started with expired;
* one-shot calls (kube_read) pin the generation of the snapshot they got, so a
  switch never changes paths under a running command; old generations are removed
  ``session_retain_seconds`` after the switch.

Teleport locks a user after a few rejected logins, and this is a person's account:
a rejected password/OTP is retried once, in the next 30 s TOTP window (another
client may have used the current code); after ``max_auth_failures`` consecutive
rejections, or a "locked" answer, automatic login stops until a manual login
through /teleport-auth succeeds. Network failures are not counted.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from db_gateway.log import get_logger
from db_gateway.postgres import TeleportAuthExpired
from db_gateway.teleport import get_tsh_status
from db_gateway.teleport_login import login_fresh

logger = get_logger(module="teleport_session")

GEN_PREFIX = "gen-"
CURRENT = "current"
TOTP_INTERVAL_SECONDS = 30
# A snapshot this close to expiry is treated as expired (renew instead of failing midway).
MIN_VALID_SECONDS = 60
SUBLOGIN_TIMEOUT_SECONDS = 30.0
STATUS_TIMEOUT_SECONDS = 10.0
# When `tsh status` cannot be parsed after a successful login (certificates last 1 h).
FALLBACK_VALIDITY = timedelta(minutes=50)


class LoginFailed(TeleportAuthExpired):
    """A login did not succeed for a reason that says nothing about the credentials."""

    kind = "transient"


class LoginRejected(LoginFailed):
    """Teleport rejected the credentials, the account is locked, or automatic login is paused."""

    def __init__(self, message: str, kind: str = "auth") -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class SessionSnapshot:
    generation: int
    root: Path                  # resolved gen-<N> directory
    valid_until: datetime | None
    roles: tuple[str, ...]

    @property
    def home_dir(self) -> Path:
        return self.root / "home"

    @property
    def kubeconfig_path(self) -> Path:
        return self.root / "kubeconfig"

    def seconds_left(self) -> float:
        if self.valid_until is None:
            return float("inf")
        return (self.valid_until - datetime.now(timezone.utc)).total_seconds()

    def usable(self, margin: float = MIN_VALID_SECONDS) -> bool:
        return self.seconds_left() > margin


def live_home(cfg: Any) -> Path:
    """TELEPORT_HOME for long-running tsh processes: follows every switch."""
    return Path(cfg.teleport.sessions_dir) / CURRENT / "home"


def live_kubeconfig(cfg: Any) -> Path:
    return Path(cfg.teleport.sessions_dir) / CURRENT / "kubeconfig"


def _switch_current(sessions: Path, generation: int) -> None:
    tmp = sessions / f".{CURRENT}-{generation}.tmp"
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    os.symlink(f"{GEN_PREFIX}{generation}", tmp)
    os.replace(tmp, sessions / CURRENT)


def _generation_of(path: Path) -> int | None:
    name = path.name
    if not name.startswith(GEN_PREFIX):
        return None
    try:
        return int(name[len(GEN_PREFIX):])
    except ValueError:
        return None


def _default_totp(secret: str) -> str:
    import pyotp

    return pyotp.TOTP(secret, digits=6, interval=TOTP_INTERVAL_SECONDS).now()


Listener = Callable[[Any, "SessionSnapshot"], Awaitable[None]]


class TeleportSessionManager:
    def __init__(self) -> None:
        self._current: SessionSnapshot | None = None
        self._generation = 0
        self._lock: asyncio.Lock | None = None
        self._auth_failures = 0
        self._blocked: str | None = None
        self._last_error: str | None = None
        self._background: set[asyncio.Task] = set()
        self._listeners: list[Listener] = []
        # Seams for tests.
        self._sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
        self._totp: Callable[[str], str] = _default_totp

    # --- state -------------------------------------------------------------------

    def current(self) -> SessionSnapshot | None:
        return self._current

    @property
    def blocked(self) -> str | None:
        """Why automatic login is paused (a manual login clears it), or None."""
        return self._blocked

    def can_auto_login(self, cfg: Any) -> bool:
        """Credentials are configured (real non-empty strings) and auto_renew is on."""
        t = getattr(cfg, "teleport", None)
        filled = all(
            isinstance(v, str) and v
            for v in (getattr(t, name, None) for name in ("password", "totp_secret", "proxy", "user"))
        )
        return filled and getattr(t, "auto_renew", None) is True

    def add_listener(self, listener: Listener) -> None:
        """``listener(cfg, snapshot)`` runs in the background after every switch."""
        if listener not in self._listeners:
            self._listeners.append(listener)

    def status_info(self) -> dict[str, Any]:
        snap = self._current
        return {
            "generation": snap.generation if snap else None,
            "valid_until": snap.valid_until.isoformat() if snap and snap.valid_until else None,
            "seconds_left": int(snap.seconds_left()) if snap and snap.valid_until else None,
            "roles": list(snap.roles) if snap else [],
            "blocked": self._blocked,
            "auth_failures": self._auth_failures,
            "last_error": self._last_error,
        }

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    # --- public entry points -----------------------------------------------------

    async def ensure_session(self, cfg: Any, deadline: float) -> SessionSnapshot:
        """A usable snapshot, logging in on demand (shared by concurrent callers)."""
        snap = self._current
        if snap is not None and snap.usable():
            return snap
        self._check_auto_login_allowed(cfg)
        lock = self._get_lock()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TeleportAuthExpired("Teleport 会话不可用，时间预算已用完，稍后重试")
        try:
            await asyncio.wait_for(lock.acquire(), timeout=remaining)
        except asyncio.TimeoutError:
            raise TeleportAuthExpired("正在登录 Teleport，稍后重试") from None
        try:
            snap = self._current
            if snap is not None and snap.usable():
                return snap
            self._check_auto_login_allowed(cfg)
            return await self._renew_locked(cfg, reason="on_demand", deadline=deadline)
        finally:
            lock.release()

    async def renew(
        self, cfg: Any, *, reason: str, password: str | None = None, otp: str | None = None,
    ) -> SessionSnapshot:
        """Log into a new generation and switch to it.

        With ``password`` and ``otp`` (a manual login) the stored credentials are not
        used and a paused automatic login is resumed on success.
        """
        async with self._get_lock():
            return await self._renew_locked(cfg, reason=reason, password=password, otp=otp)

    # --- renewal -----------------------------------------------------------------

    def _check_auto_login_allowed(self, cfg: Any) -> None:
        if not self.can_auto_login(cfg):
            raise TeleportAuthExpired(
                "Teleport 会话不可用：没有配置自动登录（TELEPORT_PASSWORD / TELEPORT_TOTP_SECRET），"
                "请通过 /teleport-auth 登录"
            )
        if self._blocked:
            raise LoginRejected(
                f"Teleport 自动登录已暂停（{self._blocked}），请通过 /teleport-auth 人工登录", "blocked",
            )

    async def _renew_locked(
        self, cfg: Any, *, reason: str, password: str | None = None, otp: str | None = None,
        deadline: float | None = None,
    ) -> SessionSnapshot:
        manual = password is not None and otp is not None
        if not manual:
            self._check_auto_login_allowed(cfg)
        sessions = Path(cfg.teleport.sessions_dir)
        if self._generation == 0:
            self._remove_leftovers(sessions)
        sessions.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._generation += 1
        generation = self._generation
        root = sessions / f"{GEN_PREFIX}{generation}"
        (root / "home").mkdir(parents=True, mode=0o700)
        try:
            await self._login(cfg, root, manual=manual, password=password, otp=otp, deadline=deadline)
            await self._sublogins(cfg, root)
            status = await get_tsh_status(
                tsh_path=cfg.teleport.tsh_path, auth_home=str(root / "home"),
                timeout=STATUS_TIMEOUT_SECONDS,
            )
        except BaseException as e:
            shutil.rmtree(root, ignore_errors=True)
            self._last_error = f"{type(e).__name__}: {e}"[:300]
            raise
        if status is not None:
            valid_until, roles = status.valid_until, tuple(status.roles)
        else:
            logger.warning("teleport_status_unparsed_after_login", generation=generation)
            valid_until, roles = datetime.now(timezone.utc) + FALLBACK_VALIDITY, ()
        _switch_current(sessions, generation)
        snap = SessionSnapshot(generation=generation, root=root, valid_until=valid_until, roles=roles)
        self._current = snap
        self._last_error = None
        logger.info("teleport_session_renewed", generation=generation, reason=reason,
                    valid_until=valid_until.isoformat() if valid_until else None, roles=list(roles))
        self._spawn(self._remove_older_later(sessions, generation, float(cfg.teleport.session_retain_seconds)))
        for listener in list(self._listeners):
            self._spawn(self._notify(listener, cfg, snap))
        return snap

    async def _login(
        self, cfg: Any, root: Path, *, manual: bool, password: str | None, otp: str | None,
        deadline: float | None,
    ) -> None:
        t = cfg.teleport
        attempts = 1 if manual else 2
        for attempt in range(attempts):
            code = otp if manual else self._totp(t.totp_secret)
            timeout = float(t.login_timeout_seconds)
            if deadline is not None:
                timeout = min(timeout, deadline - time.monotonic())
                if timeout <= 0:
                    raise LoginFailed("Teleport 登录的时间预算已用完，稍后重试")
            result = await login_fresh(
                tsh_path=t.tsh_path, proxy=t.proxy, user=t.user,
                password=password if manual else t.password, otp=code or "",
                home=str(root / "home"), timeout_s=timeout,
            )
            if result.ok:
                if manual and self._blocked:
                    logger.info("teleport_auto_login_resumed")
                self._auth_failures = 0
                self._blocked = None
                return
            logger.warning("teleport_login_failed", kind=result.kind, manual=manual,
                           attempt=attempt + 1, detail=result.message[-300:])
            if result.kind == "transient":
                raise LoginFailed(f"Teleport 登录失败（网络或超时）：{result.message[-300:]}")
            if result.kind == "locked":
                if not manual:
                    self._blocked = "账号被锁定"
                raise LoginRejected(f"Teleport 账号被锁定：{result.message[-300:]}", "locked")
            if manual:   # a mistyped code: the person retries, automatic login is unaffected
                raise LoginRejected(f"密码或 OTP 错误：{result.message[-300:]}", "auth")
            self._auth_failures += 1
            if self._auth_failures >= max(1, int(t.max_auth_failures)):
                self._blocked = f"连续 {self._auth_failures} 次被拒绝"
                raise LoginRejected(
                    f"Teleport 拒绝了自动登录（连续 {self._auth_failures} 次），自动登录已暂停："
                    f"{result.message[-300:]}", "auth",
                )
            if attempt + 1 < attempts:
                # The code may have been used by another client in this window: wait for a new one.
                wait = TOTP_INTERVAL_SECONDS - (time.time() % TOTP_INTERVAL_SECONDS) + 1.5
                if deadline is not None and time.monotonic() + wait + 10 > deadline:
                    break
                await self._sleep(wait)
        raise LoginRejected("Teleport 拒绝了自动登录，稍后由后台重试", "auth")

    async def _sublogins(self, cfg: Any, root: Path) -> None:
        """Kube and AWS app logins inside the new generation; failures only warn."""
        from db_gateway.teleport_cli import run_tsh

        tools = cfg.teleport_tools
        steps: list[list[str]] = []
        if tools.kube.enabled:
            steps.append(["kube", "login", "--all"])
        if tools.lambda_logs.enabled:
            lc = tools.lambda_logs
            steps.append(["apps", "login", "--aws-role", lc.aws_role, lc.app])
        for args in steps:
            result = await run_tsh(
                args, cfg=cfg, home=root / "home", kubeconfig=root / "kubeconfig",
                deadline=time.monotonic() + SUBLOGIN_TIMEOUT_SECONDS,
            )
            if result.exit_code != 0:
                logger.warning("teleport_sublogin_failed", args=args, exit_code=result.exit_code,
                               timed_out=result.timed_out, detail=(result.stderr or result.stdout)[-300:])

    # --- housekeeping ------------------------------------------------------------

    def _spawn(self, coro: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _notify(self, listener: Listener, cfg: Any, snap: SessionSnapshot) -> None:
        try:
            await listener(cfg, snap)
        except Exception as e:  # noqa: BLE001 - a listener must not break renewals
            logger.warning("teleport_session_listener_failed", error=f"{type(e).__name__}: {e}"[:300])

    @staticmethod
    def _remove_leftovers(sessions: Path) -> None:
        """Generations left by a previous process (container restart)."""
        if not sessions.exists():
            return
        for entry in sessions.iterdir():
            if entry.is_symlink() or entry.is_file():
                entry.unlink(missing_ok=True)
            elif entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)

    async def _remove_older_later(self, sessions: Path, keep_from: int, delay: float) -> None:
        await asyncio.sleep(max(0.0, delay))
        for entry in sessions.glob(f"{GEN_PREFIX}*"):
            generation = _generation_of(entry)
            if generation is not None and generation < keep_from:
                shutil.rmtree(entry, ignore_errors=True)

    def _reset(self) -> None:
        """Drop all state but keep the listeners (static wiring done at import time)."""
        for task in list(self._background):
            task.cancel()
        listeners = self._listeners
        self.__init__()
        self._listeners = listeners


SESSION = TeleportSessionManager()


def _reset_state() -> None:
    """Fresh manager state (tests; each test runs on its own event loop)."""
    SESSION._reset()
