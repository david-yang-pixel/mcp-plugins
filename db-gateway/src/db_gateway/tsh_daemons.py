"""Long-running tsh processes managed by db-gateway (DB tunnels, AWS proxy).

Each daemon runs ``tsh <args>`` in its own process group with
``TELEPORT_HOME=<sessions_dir>/current/home`` (teleport_session.py). It is started
lazily once a session exists, restarted when it exits or a caller marks it stale,
and its output is drained in the background so it never blocks on a full pipe.

DB tunnels are not touched on renewal: a running `tsh proxy db --tunnel` reads
the new certificate through the switched ``current`` symlink (measured on the
server). The AWS proxy is rotated instead: a new process (new local port and
credentials) starts first, callers move to it, and the old one is killed a few
seconds later so requests already on it can finish.
"""
from __future__ import annotations

import asyncio
import collections
import os
import re
import signal
import time
import weakref
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from db_gateway.log import get_logger
from db_gateway.teleport_cli import TshTimeout, _tsh_env, strip_ansi
from db_gateway.teleport_session import SESSION, SessionSnapshot

logger = get_logger(module="tsh_daemons")

STOP_WAIT_SECONDS = 5.0
OUTPUT_TAIL_LINES = 50
# A rotated-out process keeps serving requests that started on it for this long.
ROTATE_KILL_DELAY_SECONDS = 10.0
ROTATE_BUDGET_SECONDS = 40.0

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


@dataclass
class _Running:
    proc: asyncio.subprocess.Process
    reader: asyncio.Task
    info: dict[str, str]
    output: "collections.deque[str]"


class TshDaemon:
    def __init__(
        self,
        name: str,
        *,
        args: Callable[[Any], list[str]],
        parse: Callable[[list[str]], dict[str, str] | None],
        before_start: Callable[[Any, float], Awaitable[None]] | None = None,
        start_timeout: float = 15.0,
        rotate_on_renew: bool = False,
    ) -> None:
        self.name = name
        self._args = args
        self._parse = parse
        self._before_start = before_start
        self._start_timeout = start_timeout
        self.rotate_on_renew = rotate_on_renew
        self._running: _Running | None = None
        self._stale = False
        self._lock: asyncio.Lock | None = None
        self._retiring: set[asyncio.Task] = set()
        # Successful starts so far: a change means connections made through the
        # previous process (e.g. a DB tunnel) are gone.
        self.starts = 0
        self.output: collections.deque[str] = collections.deque(maxlen=OUTPUT_TAIL_LINES)
        _DAEMONS.add(self)

    @property
    def pid(self) -> int | None:
        return self._running.proc.pid if self._running is not None else None

    def _alive(self) -> bool:
        return self._running is not None and self._running.proc.returncode is None

    def mark_stale(self) -> None:
        """Force a restart on the next ``ensure`` (e.g. the proxy rejected a request)."""
        self._stale = True

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def ensure(self, cfg: Any, deadline: float) -> dict[str, str]:
        """Return the parsed ready output of a running daemon, starting it if needed."""
        async with self._get_lock():
            if self._alive() and not self._stale and self._running is not None:
                return dict(self._running.info)
            await SESSION.ensure_session(cfg, deadline)   # TeleportAuthExpired without a login
            await self._stop_locked()
            if time.monotonic() >= deadline:
                raise TshTimeout(f"{self.name} 启动前时间预算已用完，稍后重试")
            if self._before_start is not None:
                await self._before_start(cfg, deadline)
            self._install(await self._spawn(cfg, deadline))
            self._stale = False
            logger.info("tsh_daemon_started", daemon=self.name, pid=self.pid)
            return dict(self._running.info)

    async def rotate(self, cfg: Any, deadline: float) -> None:
        """Replace a running process with a fresh one; the old one is killed later.

        Does nothing when the daemon is not running (it stays lazy). If the new
        process fails to start, the running one is kept and the error is raised.
        """
        async with self._get_lock():
            if not self._alive():
                return
            if self._before_start is not None:
                await self._before_start(cfg, deadline)
            new = await self._spawn(cfg, deadline)
            old = self._running
            self._install(new)
            logger.info("tsh_daemon_rotated", daemon=self.name, pid=self.pid,
                        old_pid=old.proc.pid if old else None)
            if old is not None:
                task = asyncio.ensure_future(self._retire_later(old, ROTATE_KILL_DELAY_SECONDS))
                self._retiring.add(task)
                task.add_done_callback(self._retiring.discard)

    def _install(self, running: _Running) -> None:
        self._running = running
        self.output = running.output
        self.starts += 1

    async def _spawn(self, cfg: Any, deadline: float) -> _Running:
        timeout = max(0.1, min(self._start_timeout, deadline - time.monotonic()))
        proc = await asyncio.create_subprocess_exec(
            cfg.teleport.tsh_path, *self._args(cfg),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=_tsh_env(cfg),
            start_new_session=True,
        )
        output: collections.deque[str] = collections.deque(maxlen=OUTPUT_TAIL_LINES)
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
                output.append(shown[-1])
                info = self._parse(lines)
                if info is not None:
                    return info

        timed_out = False
        try:
            info = await asyncio.wait_for(wait_ready(), timeout=timeout)
        except asyncio.TimeoutError:
            info, timed_out = None, True
        except BaseException:
            await self._kill(proc)
            raise
        if info is not None and proc.returncode is not None:
            info = None   # matched, but the process is already gone: not a running daemon
        if info is None:
            tail = "\n".join(shown).strip()[-600:]
            rc = proc.returncode
            await self._kill(proc)
            if timed_out:
                raise TshDaemonTimeout(f"{self.name} 在 {timeout:.0f}s 内没有就绪：{tail}")
            raise TshDaemonError(f"{self.name} 启动失败（退出码 {rc}）：{tail}")
        reader = asyncio.create_task(self._drain(proc, output))
        return _Running(proc=proc, reader=reader, info=info, output=output)

    @staticmethod
    async def _readline(stream: asyncio.StreamReader) -> str | None:
        try:
            raw = await stream.readline()
        except (ValueError, asyncio.LimitOverrunError):
            raw = await stream.read(65536)   # over-long line: take a chunk
        if not raw:
            return None
        return strip_ansi(raw.decode("utf-8", errors="replace")).rstrip("\r\n")

    async def _drain(self, proc: asyncio.subprocess.Process, output: "collections.deque[str]") -> None:
        assert proc.stdout is not None
        while True:
            line = await self._readline(proc.stdout)
            if line is None:
                break
            output.append(redact(line))
        rc = await proc.wait()
        logger.warning("tsh_daemon_exited", daemon=self.name, pid=proc.pid, exit_code=rc,
                       tail=" | ".join(list(output)[-5:])[-400:])

    async def _kill(self, proc: asyncio.subprocess.Process) -> None:
        _killpg(proc.pid)   # also after a normal exit: orphans left in the group
        if proc.returncode is None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=STOP_WAIT_SECONDS)
            except asyncio.TimeoutError:
                logger.warning("tsh_daemon_stop_timeout", daemon=self.name, pid=proc.pid)

    async def _retire_later(self, old: _Running, delay: float) -> None:
        await asyncio.sleep(delay)
        old.reader.cancel()
        await self._kill(old.proc)

    async def _stop_locked(self) -> None:
        running, self._running = self._running, None
        if running is None:
            return
        running.reader.cancel()
        await self._kill(running.proc)

    async def stop(self) -> None:
        for task in list(self._retiring):
            task.cancel()
        async with self._get_lock():
            await self._stop_locked()

    def _reset(self) -> None:
        """Synchronous kill + forget (tests: each test runs on its own event loop)."""
        for task in list(self._retiring):
            task.cancel()
        if self._running is not None:
            _killpg(self._running.proc.pid)
            self._running.reader.cancel()
        self._running = self._lock = None
        self._retiring = set()
        self._stale = False


async def stop_all() -> None:
    """Stop every daemon (aiohttp on_cleanup)."""
    for daemon in list(_DAEMONS):
        await daemon.stop()


async def rotate_running(cfg: Any, deadline: float) -> None:
    """After a renewal: rotate the running daemons that need it (AWS proxy)."""
    for daemon in list(_DAEMONS):
        if not (daemon.rotate_on_renew and daemon._alive()):
            continue
        try:
            await daemon.rotate(cfg, deadline)
        except Exception as e:  # noqa: BLE001 - background work must not raise
            logger.warning("tsh_daemon_rotate_failed", daemon=daemon.name,
                           error=redact(f"{type(e).__name__}: {e}")[:300])


async def _on_session_renewed(cfg: Any, _snapshot: SessionSnapshot) -> None:
    await rotate_running(cfg, time.monotonic() + ROTATE_BUDGET_SECONDS)


SESSION.add_listener(_on_session_renewed)


def _reset_state() -> None:
    for daemon in list(_DAEMONS):
        daemon._reset()


# --- self-managed DB tunnels ---------------------------------------------------------

# Anchored on tsh's success line: a bare "127.0.0.1:<port>" also appears in
# "ERROR: listen tcp 127.0.0.1:56064: bind: address already in use".
_TUNNEL_READY_RE = re.compile(r"Started authenticated tunnel\b.*\b127\.0\.0\.1:(\d+)")


def _parse_tunnel(lines: list[str]) -> dict[str, str] | None:
    for line in lines:
        m = _TUNNEL_READY_RE.search(line)
        if m:
            return {"port": m.group(1)}
    return None


@dataclass(frozen=True)
class TunnelSpec:
    key: str
    teleport_db: str
    db_name: str
    port: int


def tunnel_spec(cfg: Any, database_key: str) -> TunnelSpec | None:
    """The tunnel db-gateway runs for ``database_key``, or None (a host tunnel / static port)."""
    pg = cfg.postgres
    if database_key == "knowledgefs":
        kfs = getattr(getattr(cfg, "teleport_tools", None), "knowledgefs", None)
        teleport_db = getattr(kfs, "teleport_db", "")
        if getattr(kfs, "enabled", False) is not True or not (isinstance(teleport_db, str) and teleport_db):
            return None
        return TunnelSpec("knowledgefs", teleport_db, pg.db_knowledgefs, int(pg.port_knowledgefs))
    if database_key in ("prod", "billing", "plugin"):
        teleport_db = getattr(pg, f"teleport_db_{database_key}", "")
        if not (isinstance(teleport_db, str) and teleport_db):
            return None
        return TunnelSpec(
            database_key, teleport_db, getattr(pg, f"db_{database_key}"),
            int(getattr(pg, f"tunnel_port_{database_key}")),
        )
    return None


def _tunnel_args(cfg: Any, database_key: str) -> list[str]:
    spec = tunnel_spec(cfg, database_key)
    if spec is None:
        raise TshDaemonError(f"{database_key} 没有配置由 db-gateway 管理的隧道")
    # Same user as the query/metadata connections (Teleport's Postgres engine takes the
    # user from the client's startup message, so a tunnel-only override would not apply).
    return [
        "proxy", "db", "--tunnel", "--port", str(spec.port),
        "--db-user", cfg.postgres.user, "--db-name", spec.db_name, spec.teleport_db,
    ]


_TUNNELS: dict[str, TshDaemon] = {}


def tunnel_daemon(database_key: str) -> TshDaemon:
    daemon = _TUNNELS.get(database_key)
    if daemon is None:
        daemon = TshDaemon(
            f"{database_key}_tunnel",
            args=lambda cfg, key=database_key: _tunnel_args(cfg, key),
            parse=_parse_tunnel,
        )
        _TUNNELS[database_key] = daemon
    return daemon


async def ensure_tunnel(cfg: Any, database_key: str, deadline: float) -> int:
    """Make sure db-gateway's tunnel for ``database_key`` listens; returns its port."""
    spec = tunnel_spec(cfg, database_key)
    if spec is None:
        raise TshDaemonError(f"{database_key} 没有配置由 db-gateway 管理的隧道")
    daemon = tunnel_daemon(database_key)
    info = await daemon.ensure(cfg, deadline)
    if info.get("port") != str(spec.port):
        await daemon.stop()
        raise TshDaemonError(
            f"{daemon.name} 监听在 127.0.0.1:{info.get('port')}，不是配置的 {spec.port}"
        )
    return spec.port


KNOWLEDGEFS_TUNNEL = tunnel_daemon("knowledgefs")
