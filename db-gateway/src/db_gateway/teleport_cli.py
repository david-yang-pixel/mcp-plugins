"""Read-only tsh layer for plugin_lambda_logs and kube_read.

Two facts measured on the server (spec §9.2) shape this module:

* tsh rewrites the whole key ring (user cert, key, CAs, SSH cert) on every
  ``tsh apps login`` / ``tsh aws`` / ``tsh kubectl``. Running it against the
  shared ``teleport.auth_home`` would make ``pg_pool.keys_signature`` flush the
  connection pools and could race tsh-auth-watchdog's renewal. The tools
  therefore run tsh against a container-local copy (the *shadow* home) and only
  ever read the shared directory.
* The host-mounted tsh 18.0.0 fails ``tsh aws`` signature verification against
  the 18.11 proxy, so the tools use the tsh bundled in the image
  (``teleport_tools.tsh_path``).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from db_gateway.log import get_logger
from db_gateway.postgres import TeleportAuthExpired
from db_gateway.teleport import get_tsh_status

logger = get_logger(module="teleport_cli")

SHADOW_SIGNATURE_FILE = ".source-signature.json"
SHADOW_IGNORE_SUFFIXES = ("-app", "-kube", "-db")

LOGIN_TIMEOUT_SECONDS = 20.0
STDERR_KEEP_BYTES = 64 * 1024
KILL_GRACE_SECONDS = 2.0
STATUS_TIMEOUT_SECONDS = 5.0

# Cleared for tsh: a host/container proxy would MITM Teleport. NO_PROXY is not set
# to "*" because `tsh aws` points the aws child at its local proxy via HTTPS_PROXY.
_PROXY_VARS = ("http_proxy", "https_proxy", "all_proxy", "no_proxy")
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

_shadow_lock: asyncio.Lock | None = None
_login_locks: dict[tuple[str, ...], asyncio.Lock] = {}
_last_login_ok: dict[tuple[str, ...], float] = {}


def _reset_state() -> None:
    """Drop module-level locks and caches (tests; each test has its own loop)."""
    global _shadow_lock
    _shadow_lock = None
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


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def source_signature(auth_home: str) -> list[list[str]] | None:
    """``[[relative path, sha256], ...]`` of the user TLS certs ``keys/*/*.crt``.

    tsh rewrites these files with identical content, so the signature only
    changes when tsh-auth-watchdog renews the session. None while not logged in
    (no ``current-profile`` or no cert), e.g. during the renewal window.
    """
    root = Path(auth_home)
    if not (root / "current-profile").is_file():
        return None
    entries: list[list[str]] = []
    try:
        for cert in sorted((root / "keys").glob("*/*.crt")):
            if cert.is_file():
                digest = hashlib.sha256(cert.read_bytes()).hexdigest()
                entries.append([str(cert.relative_to(root)), digest])
    except OSError:
        return None
    return entries or None


def _read_shadow_signature(shadow: Path) -> list[list[str]] | None:
    try:
        return json.loads((shadow / SHADOW_SIGNATURE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _ignore(_dir: str, names: list[str]) -> set[str]:
    return {n for n in names if n == "bin" or n.endswith(SHADOW_IGNORE_SUFFIXES)}


def _sync_shadow(source: str, shadow: Path, sig: list[list[str]]) -> bool:
    """Copy ``source`` into ``shadow`` atomically; False if the source moved under us."""
    tmp = shadow.with_name(f"{shadow.name}.tmp-{os.getpid()}")
    old = shadow.with_name(f"{shadow.name}.old-{os.getpid()}")
    for _attempt in range(2):
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            shutil.copytree(source, tmp, ignore=_ignore, symlinks=True)
        except (OSError, shutil.Error):
            continue
        if source_signature(source) != sig:
            continue  # tsh-auth-watchdog renewed while we copied
        (tmp / SHADOW_SIGNATURE_FILE).write_text(json.dumps(sig), encoding="utf-8")
        shadow.parent.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(old, ignore_errors=True)
        if shadow.exists():
            shadow.rename(old)
        tmp.rename(shadow)
        shutil.rmtree(old, ignore_errors=True)
        return True
    shutil.rmtree(tmp, ignore_errors=True)
    return False


async def ensure_shadow_home(cfg: Any) -> str:
    """Return the shadow TELEPORT_HOME, re-syncing it after a renewal.

    Raises TeleportAuthExpired when neither the shared home nor an earlier copy
    is usable.
    """
    global _shadow_lock
    if _shadow_lock is None:
        _shadow_lock = asyncio.Lock()
    source = cfg.teleport.auth_home
    shadow = Path(cfg.teleport_tools.shadow_home)
    async with _shadow_lock:
        sig = source_signature(source)
        current = _read_shadow_signature(shadow)
        if sig is not None and sig == current:
            return str(shadow)
        if sig is not None and await asyncio.to_thread(_sync_shadow, source, shadow, sig):
            logger.info("teleport_shadow_synced", shadow=str(shadow))
            return str(shadow)
        if current is not None:
            # Renewal window (or a copy that raced it): the old certificate is
            # still within its validity, keep using it.
            return str(shadow)
        raise TeleportAuthExpired("Teleport 会话不可用（可能正在续期），稍后重试")


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


def _tsh_env(cfg: Any) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k.lower() not in _PROXY_VARS}
    env["TELEPORT_HOME"] = cfg.teleport_tools.shadow_home
    env["KUBECONFIG"] = cfg.teleport_tools.kube.kubeconfig_path
    env["TELEPORT_TOOLS_VERSION"] = "off"   # no client-tools auto update / re-exec
    return env


def _killpg(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


async def run_tsh(
    args: list[str], *, cfg: Any, deadline: float, max_output_bytes: int | None = None,
) -> TshResult:
    """Run ``tsh <args>`` (no shell) until it exits or ``deadline`` (monotonic) passes.

    The process runs in its own session; at the deadline the whole process group
    is killed (``tsh aws`` has an ``aws`` child that would otherwise keep the
    pipes open) and whatever was read so far is returned.
    """
    started = time.monotonic()
    remaining = deadline - started
    if remaining <= 0:
        return TshResult(None, "", "", True, False, 0)
    kubeconfig_dir = os.path.dirname(cfg.teleport_tools.kube.kubeconfig_path)
    if kubeconfig_dir:
        os.makedirs(kubeconfig_dir, exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
        cfg.teleport_tools.tsh_path, *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_tsh_env(cfg),
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


async def _login_failure(cfg: Any, result: TshResult) -> Exception:
    """TeleportAuthExpired when the shared session is gone, else TeleportNotAuthorized."""
    try:
        status = await asyncio.wait_for(
            get_tsh_status(
                proxy=cfg.teleport.proxy, user=cfg.teleport.user,
                auth_home=cfg.teleport.auth_home,
            ),
            timeout=STATUS_TIMEOUT_SECONDS,
        )
    except Exception:
        status = None
    detail = (result.stderr or result.stdout).strip()[:600]
    if not detail:
        detail = "登录超时" if result.timed_out else f"exit {result.exit_code}"
    if status is None:
        return TeleportAuthExpired(f"Teleport 会话已过期或正在续期：{detail}")
    return TeleportNotAuthorized(f"{detail}；当前 Roles: {', '.join(status.roles)}")


async def tsh_login(
    login_args: list[str], *, cfg: Any, deadline: float, not_before: float | None = None,
) -> None:
    """Run one ``tsh <login_args>``; concurrent callers share a single login.

    Skipped when the same login already succeeded at or after ``not_before``.
    """
    key = tuple(login_args)
    lock = _login_locks.setdefault(key, asyncio.Lock())
    async with lock:
        last = _last_login_ok.get(key)
        if not_before is not None and last is not None and last >= not_before:
            return
        result = await run_tsh(
            login_args, cfg=cfg,
            deadline=min(deadline, time.monotonic() + LOGIN_TIMEOUT_SECONDS),
        )
        if result.exit_code == 0:
            _last_login_ok[key] = time.monotonic()
            logger.info("tsh_login_ok", args=login_args)
            return
    logger.warning("tsh_login_failed", args=login_args, exit_code=result.exit_code,
                   timed_out=result.timed_out)
    raise await _login_failure(cfg, result)


async def run_tsh_with_login(
    args: list[str], *, cfg: Any, deadline: float, login_args: list[str],
    needs_login: Callable[[TshResult], bool], max_output_bytes: int | None = None,
) -> TshResult:
    """``run_tsh``; on a credentials failure log in once and retry within ``deadline``."""
    started = time.monotonic()
    result = await run_tsh(args, cfg=cfg, deadline=deadline, max_output_bytes=max_output_bytes)
    if not needs_login(result):
        return result
    await tsh_login(login_args, cfg=cfg, deadline=deadline, not_before=started)
    return await run_tsh(args, cfg=cfg, deadline=deadline, max_output_bytes=max_output_bytes)
