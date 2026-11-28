"""`tsh login` through pexpect (password + OTP prompts).

Used by teleport_session.py for db-gateway's own session, always into a fresh
gen-<N> home (also for manual logins submitted through /teleport-auth).

Gotchas kept in one place:
1. HTTP(S)_PROXY must be cleared: a MITM proxy makes Teleport answer "invalid
   credentials".
2. An existing profile skips the password prompt; a fresh, empty TELEPORT_HOME
   avoids that.
3. TELEPORT_TOOLS_VERSION=off keeps tsh from re-executing another client version.

The prompts match tsh 18.11 ("Enter password for Teleport user <u>:" and
"Enter an OTP code from a device:", checked on the server).
"""
from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass

from db_gateway.log import get_logger

logger = get_logger(module="integrations.teleport_login")

_PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy")
_TAIL_CHARS = 600

_LOCKED_RE = re.compile(r"\blocked\b|too many (failed )?(login )?attempts", re.I)
_AUTH_RE = re.compile(
    r"invalid credentials|access denied|invalid (totp|otp|token|password)|"
    r"bad (password|credentials)|authentication failed|incorrect",
    re.I,
)
_TRANSIENT_RE = re.compile(
    r"connection refused|no such host|i/o timeout|timed? ?out|dial tcp|connection reset|"
    r"network is unreachable|tls handshake|\bEOF\b",
    re.I,
)


@dataclass(frozen=True)
class LoginResult:
    ok: bool
    # "ok" | "auth" (password/OTP rejected) | "locked" (account locked) |
    # "transient" (network, timeout, nothing submitted: does not count against the account)
    kind: str
    message: str


def classify_login_output(text: str) -> str:
    """Why a submitted login failed. Unknown failures count as rejections (conservative)."""
    if _LOCKED_RE.search(text):
        return "locked"
    if _AUTH_RE.search(text):
        return "auth"
    if _TRANSIENT_RE.search(text):
        return "transient"
    return "auth"


def _login_env(home: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _PROXY_VARS}
    env.update(NO_PROXY="*", no_proxy="*", TELEPORT_HOME=home, TELEPORT_TOOLS_VERSION="off")
    return env


# Terminal control sequences tsh writes to the pty (OSC colour queries, CSI codes).
_ANSI_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-Z\\-_]")


def _tail(text: str | None, secrets: tuple[str, ...] = ()) -> str:
    """Readable end of tsh output: control sequences removed, secrets redacted before cutting."""
    return _redact(_ANSI_RE.sub("", text or ""), secrets).strip()[-_TAIL_CHARS:]


def _redact(text: str, secrets: tuple[str, ...]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "<redacted>")
    return text


def _pexpect_login(
    *, tsh_path: str, proxy: str, user: str, password: str, otp: str, home: str,
    timeout_s: float, ttl_minutes: int | None,
) -> LoginResult:
    import pexpect

    args = ["login", f"--proxy={proxy}", f"--user={user}"]
    if ttl_minutes:
        args.append(f"--ttl={ttl_minutes}")
    secrets = (password, otp)
    try:
        child = pexpect.spawn(tsh_path, args, env=_login_env(home), timeout=timeout_s, encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        return LoginResult(False, "transient", f"cannot start tsh: {e}")
    try:
        idx = child.expect([r"[Ee]nter password.*:", r"[Pp]assword.*:", pexpect.EOF, pexpect.TIMEOUT])
        if idx in (2, 3):
            # Nothing was submitted: this never counts against the account.
            return LoginResult(False, "transient", f"no password prompt: {_tail(child.before, secrets)}")
        child.sendline(password)
        idx = child.expect([
            r"[Ee]nter.*OTP.*:", r"[Oo]ne.*time.*password.*:", r"[Tt]wo-?factor.*:",
            r"[Aa]uthentication.*code.*:", pexpect.EOF, pexpect.TIMEOUT,
        ])
        if idx == 5:
            return LoginResult(False, "transient", "timed out waiting for the OTP prompt")
        if idx == 4:
            out = child.before or ""
            return LoginResult(False, classify_login_output(out), _tail(out, secrets))
        child.sendline(otp)
        idx = child.expect([pexpect.EOF, pexpect.TIMEOUT])
        out = child.before or ""
        if idx == 1:
            return LoginResult(False, "transient", "timed out after the OTP was sent")
        child.close()
        if child.exitstatus == 0:
            return LoginResult(True, "ok", "tsh login succeeded")
        return LoginResult(False, classify_login_output(out), _tail(out, secrets))
    except Exception as e:  # noqa: BLE001
        return LoginResult(False, "transient", f"pexpect error: {e}")
    finally:
        if child.isalive():
            child.close(force=True)


async def login_fresh(
    *, tsh_path: str, proxy: str, user: str, password: str, otp: str, home: str,
    timeout_s: float = 60, ttl_minutes: int | None = None,
) -> LoginResult:
    """`tsh login` into ``home`` (expected empty, so the password prompt always appears)."""
    logger.info("tsh_login_start", proxy=proxy, user=user, home=home)
    result = await asyncio.to_thread(
        _pexpect_login, tsh_path=tsh_path, proxy=proxy, user=user, password=password,
        otp=otp, home=home, timeout_s=timeout_s, ttl_minutes=ttl_minutes,
    )
    return LoginResult(result.ok, result.kind, _redact(result.message, (password, otp)))

