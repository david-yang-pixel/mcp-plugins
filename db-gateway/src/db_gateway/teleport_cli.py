"""tsh subprocess layer for plugin_lambda_logs, kube_read and the session manager.

Every call runs the tsh bundled in the image (``teleport.tsh_path``; the
host-mounted 18.0.0 fails `tsh aws` signature verification against the 18.11
proxy) against db-gateway's own session (teleport_session.py): by default the
live ``current/home`` + ``current/kubeconfig``, or the pinned gen-<N> paths a
caller passes in.
"""
from __future__ import annotations

import asyncio
import os
import re
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from db_gateway.log import get_logger
from db_gateway.postgres import TeleportAuthExpired

logger = get_logger(module="teleport_cli")

LOGIN_TIMEOUT_SECONDS = 20.0
STDERR_KEEP_BYTES = 64 * 1024
KILL_GRACE_SECONDS = 2.0

# Cleared for tsh: a host/container proxy would MITM Teleport. NO_PROXY is not set
# to "*" because `tsh aws` points the aws child at its local proxy via HTTPS_PROXY.
_PROXY_VARS = ("http_proxy", "https_proxy", "all_proxy", "no_proxy")
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

_login_locks: dict[tuple[str, ...], asyncio.Lock] = {}
_last_login_ok: dict[tuple[str, ...], float] = {}


def _reset_state() -> None:
    """Drop module-level locks and caches (tests; each test has its own loop)."""
    _login_locks.clear()
    _last_login_ok.clear()


@dataclass
class TshResult:
    exit_code: int | None      # None when killed at the deadline or never started
    stdout: str                # utf-8 (errors="replace"), verbatim
    stderr: str                # utf-8 (errors="replace"), ANSI colours removed
    timed_out: bool
    truncated: bool            # stdout exceeded max_output_bytes
    elapsed_ms: int


class TeleportNotAuthorized(Exception):
    """tsh login failed although the Teleport session itself is valid (role/app missing)."""


class TshTimeout(Exception):
    """The call's time budget ran out before tsh could finish (e.g. during login)."""


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


# --- tsh subprocess layer -------------------------------------------------------


class _Sink:
    """Keeps the first ``limit`` bytes of a stream but drains all of it."""

    def __init__(self, limit: int | None) -> None:
        self.limit = limit
        self.data = bytearray()
        self.truncated = False

    async def drain(self, stream: asyncio.StreamReader) -> None:
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                return
            if self.limit is None:
                self.data += chunk
                continue
            room = self.limit - len(self.data)
            if room > 0:
                self.data += chunk[:room]
            if len(chunk) > max(room, 0):
                self.truncated = True


def _tsh_env(cfg: Any, home: Path | None = None, kubeconfig: Path | None = None) -> dict[str, str]:
    from db_gateway.teleport_session import live_home, live_kubeconfig

    env = {k: v for k, v in os.environ.items() if k.lower() not in _PROXY_VARS}
    env["TELEPORT_HOME"] = str(home if home is not None else live_home(cfg))
    env["KUBECONFIG"] = str(kubeconfig if kubeconfig is not None else live_kubeconfig(cfg))
    env["TELEPORT_TOOLS_VERSION"] = "off"   # no client-tools auto update / re-exec
    return env


def _killpg(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


async def acquire_before(semaphore: asyncio.Semaphore, deadline: float) -> bool:
    """Acquire ``semaphore`` unless ``deadline`` (monotonic) passes first."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return False
    try:
        await asyncio.wait_for(semaphore.acquire(), timeout=remaining)
    except asyncio.TimeoutError:
        return False
    return True


async def run_tsh(
    args: list[str], *, cfg: Any, deadline: float, max_output_bytes: int | None = None,
    home: Path | None = None, kubeconfig: Path | None = None,
) -> TshResult:
    """Run ``tsh <args>`` (no shell) until it exits or ``deadline`` (monotonic) passes.

    ``home`` / ``kubeconfig`` pin a session generation; by default the live
    ``current`` paths are used. The process runs in its own session; at the
    deadline the whole process group is killed (``tsh aws`` has an ``aws`` child
    that would otherwise keep the pipes open) and whatever was read so far is
    returned.
    """
    started = time.monotonic()
    remaining = deadline - started
    if remaining <= 0:
        return TshResult(None, "", "", True, False, 0)
    proc = await asyncio.create_subprocess_exec(
        cfg.teleport.tsh_path, *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_tsh_env(cfg, home, kubeconfig),
        start_new_session=True,
    )
    out, err = _Sink(max_output_bytes), _Sink(STDERR_KEEP_BYTES)
    assert proc.stdout is not None and proc.stderr is not None
    readers = [
        asyncio.create_task(out.drain(proc.stdout)),
        asyncio.create_task(err.drain(proc.stderr)),
    ]
    timed_out = False
    try:
        await asyncio.wait_for(proc.wait(), timeout=remaining)
    except asyncio.TimeoutError:
        timed_out = True
        _killpg(proc.pid)
        await proc.wait()
    except BaseException:
        _killpg(proc.pid)
        for task in readers:
            task.cancel()
        raise
    _killpg(proc.pid)  # orphans left in the group after a normal exit
    _done, pending = await asyncio.wait(readers, timeout=KILL_GRACE_SECONDS)
    for task in pending:
        task.cancel()
    return TshResult(
        exit_code=None if timed_out else proc.returncode,
        stdout=bytes(out.data).decode("utf-8", errors="replace"),
        stderr=strip_ansi(bytes(err.data).decode("utf-8", errors="replace")),
        timed_out=timed_out,
        truncated=out.truncated,
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )


async def classify_failure(cfg: Any, detail: str) -> Exception:
    """TeleportAuthExpired without a usable session, else TeleportNotAuthorized (roles)."""
    from db_gateway.teleport_session import SESSION

    snap = SESSION.current()
    if snap is None or not snap.usable(margin=0):
        return TeleportAuthExpired(f"Teleport 会话已过期或正在续期：{detail}")
    return TeleportNotAuthorized(f"{detail}；当前 Roles: {', '.join(snap.roles)}")


async def _login_failure(cfg: Any, result: TshResult) -> Exception:
    detail = (result.stderr or result.stdout).strip()[:600] or f"exit {result.exit_code}"
    return await classify_failure(cfg, detail)


async def tsh_login(
    login_args: list[str], *, cfg: Any, deadline: float, not_before: float | None = None,
    home: Path | None = None, kubeconfig: Path | None = None,
) -> None:
    """Run one ``tsh <login_args>``; concurrent callers share a single login.

    Skipped when the same login (same home) already succeeded at or after ``not_before``.
    """
    key = (*login_args, str(home))
    lock = _login_locks.setdefault(key, asyncio.Lock())
    async with lock:
        last = _last_login_ok.get(key)
        if not_before is not None and last is not None and last >= not_before:
            return
        result = await run_tsh(
            login_args, cfg=cfg, home=home, kubeconfig=kubeconfig,
            deadline=min(deadline, time.monotonic() + LOGIN_TIMEOUT_SECONDS),
        )
        if result.exit_code == 0:
            _last_login_ok[key] = time.monotonic()
            logger.info("tsh_login_ok", args=login_args)
            return
    logger.warning("tsh_login_failed", args=login_args, exit_code=result.exit_code,
                   timed_out=result.timed_out)
    if result.timed_out:
        # Out of time says nothing about roles; do not report NOT_AUTHORIZED.
        raise TshTimeout("tsh 登录没有在时间预算内完成，稍后重试")
    raise await _login_failure(cfg, result)


async def run_tsh_with_login(
    args: list[str], *, cfg: Any, deadline: float, login_args: list[str],
    needs_login: Callable[[TshResult], bool], max_output_bytes: int | None = None,
    home: Path | None = None, kubeconfig: Path | None = None,
) -> TshResult:
    """``run_tsh``; on a credentials failure log in once and retry within ``deadline``."""
    started = time.monotonic()
    paths = {"home": home, "kubeconfig": kubeconfig}
    result = await run_tsh(args, cfg=cfg, deadline=deadline, max_output_bytes=max_output_bytes, **paths)
    if not needs_login(result):
        return result
    await tsh_login(login_args, cfg=cfg, deadline=deadline, not_before=started, **paths)
    return await run_tsh(args, cfg=cfg, deadline=deadline, max_output_bytes=max_output_bytes, **paths)
