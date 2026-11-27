"""Long-running tsh processes managed by db-gateway (aws proxy, db tunnel).

Each daemon runs ``tsh <args>`` against the shadow TELEPORT_HOME (see
teleport_cli), in its own process group. It is started lazily, reused while
the shadow certificate stays the same, and restarted after a renewal, after it
exits, or after a caller marked it stale. Its output is drained in the
background so the process never blocks on a full pipe.
"""
from __future__ import annotations

import asyncio
import collections
import os
import re
import signal
import time
import weakref
from typing import Any, Awaitable, Callable

from db_gateway.log import get_logger
from db_gateway.teleport_cli import (
    TshTimeout,
    _tsh_env,
    ensure_shadow_home,
    source_signature,
    strip_ansi,
)

logger = get_logger(module="tsh_daemons")

STOP_WAIT_SECONDS = 5.0
OUTPUT_TAIL_LINES = 50

# `tsh proxy aws` prints its (local, per-process) credentials as export lines.
# Never keep them in the output tail, logs or error messages.
_SECRET_EXPORT_RE = re.compile(
    r"^(\s*(?:export\s+)?[A-Za-z0-9_]*(?:SECRET|TOKEN|PASSWORD|ACCESS_KEY|KEY_ID)[A-Za-z0-9_]*=).*$",
    re.IGNORECASE,
)


def redact(line: str) -> str:
    return _SECRET_EXPORT_RE.sub(r"\1<redacted>", line)

_DAEMONS: "weakref.WeakSet[TshDaemon]" = weakref.WeakSet()


class TshDaemonError(Exception):
    """The daemon exited or did not become ready; the message carries its output tail."""


class TshDaemonTimeout(TshDaemonError):
    """The daemon did not become ready in time (says nothing about roles)."""


def _killpg(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


class TshDaemon:
    def __init__(
        self,
        name: str,
        *,
        args: Callable[[Any], list[str]],
        parse: Callable[[list[str]], dict[str, str] | None],
        before_start: Callable[[Any, float], Awaitable[None]] | None = None,
        start_timeout: float = 15.0,
    ) -> None:
        self.name = name
        self._args = args
        self._parse = parse
        self._before_start = before_start
        self._start_timeout = start_timeout
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task | None = None
        self._info: dict[str, str] | None = None
        self._sig: list[list[str]] | None = None
        self._stale = False
        self._lock: asyncio.Lock | None = None
        self.output: collections.deque[str] = collections.deque(maxlen=OUTPUT_TAIL_LINES)
        _DAEMONS.add(self)

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    def mark_stale(self) -> None:
        """Force a restart on the next ``ensure`` (e.g. the proxy rejected a request)."""
        self._stale = True

    async def ensure(self, cfg: Any, deadline: float) -> dict[str, str]:
        """Return the parsed ready output of a running daemon, starting it if needed."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            shadow = await ensure_shadow_home(cfg)
            sig = source_signature(shadow)
            if self._alive() and not self._stale and self._info is not None and self._sig == sig:
                return dict(self._info)
            await self._stop_locked()
            if time.monotonic() >= deadline:
                raise TshTimeout(f"{self.name} 启动前时间预算已用完，稍后重试")
            if self._before_start is not None:
                await self._before_start(cfg, deadline)
            self._info = await self._start(cfg, deadline)
            self._sig = sig
            self._stale = False
            logger.info("tsh_daemon_started", daemon=self.name, pid=self.pid)
            return dict(self._info)

    async def _start(self, cfg: Any, deadline: float) -> dict[str, str]:
        timeout = max(0.1, min(self._start_timeout, deadline - time.monotonic()))
        proc = await asyncio.create_subprocess_exec(
            cfg.teleport_tools.tsh_path, *self._args(cfg),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=_tsh_env(cfg),
            start_new_session=True,
        )
        self._proc = proc
        self.output.clear()
        lines: list[str] = []      # raw, for parse() only
        shown: list[str] = []      # redacted, for messages
        assert proc.stdout is not None

        async def wait_ready() -> dict[str, str] | None:
            while True:
                line = await self._readline(proc.stdout)
                if line is None:
                    return None
                lines.append(line)
                shown.append(redact(line))
                self.output.append(shown[-1])
                info = self._parse(lines)
                if info is not None:
                    return info

        timed_out = False
        try:
            info = await asyncio.wait_for(wait_ready(), timeout=timeout)
        except asyncio.TimeoutError:
            info, timed_out = None, True
        if info is not None and proc.returncode is not None:
            info = None   # matched, but the process is already gone: not a running daemon
        if info is None:
            tail = "\n".join(shown).strip()[-600:]
            rc = proc.returncode
            await self._stop_locked()
            if timed_out:
                raise TshDaemonTimeout(f"{self.name} 在 {timeout:.0f}s 内没有就绪：{tail}")
            raise TshDaemonError(f"{self.name} 启动失败（退出码 {rc}）：{tail}")
        self._reader = asyncio.create_task(self._drain(proc))
        return info

    @staticmethod
    async def _readline(stream: asyncio.StreamReader) -> str | None:
        try:
            raw = await stream.readline()
        except (ValueError, asyncio.LimitOverrunError):
            raw = await stream.read(65536)   # over-long line: take a chunk
        if not raw:
            return None
        return strip_ansi(raw.decode("utf-8", errors="replace")).rstrip("\r\n")

    async def _drain(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout is not None
        while True:
            line = await self._readline(proc.stdout)
            if line is None:
                break
            self.output.append(redact(line))
        rc = await proc.wait()
        logger.warning("tsh_daemon_exited", daemon=self.name, exit_code=rc,
                       tail=" | ".join(list(self.output)[-5:])[-400:])

    async def _stop_locked(self) -> None:
        proc, reader = self._proc, self._reader
        self._proc, self._reader, self._info = None, None, None
        if reader is not None:
            reader.cancel()
        if proc is None:
            return
        _killpg(proc.pid)   # also after a normal exit: orphans left in the group
        if proc.returncode is None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=STOP_WAIT_SECONDS)
            except asyncio.TimeoutError:
                logger.warning("tsh_daemon_stop_timeout", daemon=self.name, pid=proc.pid)

    async def stop(self) -> None:
        if self._lock is None:
            await self._stop_locked()
            return
        async with self._lock:
            await self._stop_locked()

    def _reset(self) -> None:
        """Synchronous kill + forget (tests: each test runs on its own event loop)."""
        if self._proc is not None:
            _killpg(self._proc.pid)
        if self._reader is not None:
            self._reader.cancel()
        self._proc = self._reader = self._info = self._sig = self._lock = None
        self._stale = False


async def stop_all() -> None:
    """Stop every daemon (aiohttp on_cleanup)."""
    for daemon in list(_DAEMONS):
        await daemon.stop()


def _reset_state() -> None:
    for daemon in list(_DAEMONS):
        daemon._reset()


# --- KnowledgeFS production tunnel -------------------------------------------------

# Anchored on tsh's success line: a bare "127.0.0.1:<port>" also appears in
# "ERROR: listen tcp 127.0.0.1:56064: bind: address already in use".
_TUNNEL_READY_RE = re.compile(r"Started authenticated tunnel\b.*\b127\.0\.0\.1:(\d+)")


def _parse_tunnel(lines: list[str]) -> dict[str, str] | None:
    for line in lines:
        m = _TUNNEL_READY_RE.search(line)
        if m:
            return {"port": m.group(1)}
    return None


def _knowledgefs_args(cfg: Any) -> list[str]:
    # Same user as the query/metadata connections (Teleport's Postgres engine takes the
    # user from the client's startup message, so a tunnel-only override would not apply).
    return [
        "proxy", "db", "--tunnel",
        "--port", str(cfg.postgres.port_knowledgefs),
        "--db-user", cfg.postgres.user,
        "--db-name", cfg.postgres.db_knowledgefs,
        cfg.teleport_tools.knowledgefs.teleport_db,
    ]


KNOWLEDGEFS_TUNNEL = TshDaemon("knowledgefs_tunnel", args=_knowledgefs_args, parse=_parse_tunnel)


async def ensure_knowledgefs_tunnel(cfg: Any, deadline: float) -> dict[str, str]:
    """Make sure the local tunnel to knowledge-fs-production-readonly is listening."""
    info = await KNOWLEDGEFS_TUNNEL.ensure(cfg, deadline)
    expected = str(cfg.postgres.port_knowledgefs)
    if info.get("port") != expected:
        await KNOWLEDGEFS_TUNNEL.stop()
        raise TshDaemonError(
            f"knowledgefs_tunnel 监听在 127.0.0.1:{info.get('port')}，不是配置的 {expected}"
        )
    return info
