"""Run difyctl as a child process: exec arguments, JSON on stdin, allow-listed environment."""
from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path

# Proxy settings, including the CA file an intercepting proxy needs; never credentials of this server.
PROXY_VARIABLES = ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "no_proxy", "all_proxy",
                   "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE")
TOKEN = re.compile(r"dfoa_[A-Za-z0-9._~+/=-]+")
MAX_CAPTURE_BYTES = 32 * 1024 * 1024
INTERRUPT_GRACE_SECONDS = 5


def redact(text: str) -> str:
    """Remove Dify OAuth access tokens from anything that leaves the server."""
    return TOKEN.sub("dfoa_[REDACTED]", text)


def redact_value(value):
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [redact_value(v) for v in value]
    if isinstance(value, dict):
        return {redact(str(k)): redact_value(v) for k, v in value.items()}
    return value


@dataclass
class Run:
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    truncated: bool = False
    duration_ms: int = 0
    notices: list[str] = field(default_factory=list)
    error: dict | None = None

    def json(self):
        text = self.stdout.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except ValueError:
            return None

    def events(self) -> list:
        """Parse the one-JSON-object-per-line output of --stream."""
        events = []
        for line in self.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except ValueError:
                events.append({"unparsed": line})
        return events


def split_stderr(stderr: str) -> tuple[list[str], dict | None]:
    """Separate notice lines from the trailing JSON error envelope difyctl prints."""
    lines = stderr.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("{"):
            try:
                envelope = json.loads("\n".join(lines[index:]))
            except ValueError:
                continue
            if isinstance(envelope, dict):
                error = envelope.get("error") if isinstance(envelope.get("error"), dict) else envelope
                return [l for l in lines[:index] if l.strip()], error
    return [l for l in lines if l.strip()], None


class Runner:
    def __init__(self, binary: str, home: Path, environ=None):
        self.binary = binary
        self.home = home
        self.environ = os.environ if environ is None else environ

    def environment(self, config_dir: Path) -> dict[str, str]:
        # DIFY_TOKEN and DIFY_WORKSPACE_ID are deliberately never set: they disable logout and use workspace.
        env = {"PATH": self.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": str(self.home),
               "DIFY_CONFIG_DIR": str(config_dir), "DIFY_OUTPUT": "json"}
        for key in PROXY_VARIABLES:
            if self.environ.get(key):
                env[key] = self.environ[key]
        return env

    async def run(self, args: list[str], *, config_dir: Path, cwd: Path | None = None,
                  stdin: bytes | None = None, timeout: float = 90) -> Run:
        if any(not isinstance(a, str) or "\0" in a for a in args):
            raise ValueError("Invalid difyctl argument")
        started = time.monotonic()
        process = await asyncio.create_subprocess_exec(
            self.binary, *args, cwd=str(cwd or self.home), env=self.environment(config_dir),
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
        truncated = False

        async def capture(stream) -> bytes:
            nonlocal truncated
            chunks, size = [], 0
            while True:
                chunk = await stream.read(65536)
                if not chunk:
                    return b"".join(chunks)
                if size < MAX_CAPTURE_BYTES:
                    chunks.append(chunk[:MAX_CAPTURE_BYTES - size])
                    size += len(chunks[-1])
                if size >= MAX_CAPTURE_BYTES and len(chunk):
                    truncated = True

        async def feed():
            if stdin is not None:
                try:
                    process.stdin.write(stdin)
                    await process.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    process.stdin.close()

        readers = asyncio.gather(capture(process.stdout), capture(process.stderr), feed(), process.wait())
        timed_out = False
        try:
            out, err, _, _ = await asyncio.wait_for(asyncio.shield(readers), timeout)
        except asyncio.TimeoutError:
            timed_out = True
            # SIGINT lets difyctl abort the request and print what it has (e.g. status incomplete and run_id).
            self._signal(process, signal.SIGINT)
            try:
                out, err, _, _ = await asyncio.wait_for(asyncio.shield(readers), INTERRUPT_GRACE_SECONDS)
            except asyncio.TimeoutError:
                self._signal(process, signal.SIGKILL)
                out, err, _, _ = await readers
        except asyncio.CancelledError:
            self._signal(process, signal.SIGKILL)
            raise
        run = Run(process.returncode, redact(out.decode("utf-8", "replace")), redact(err.decode("utf-8", "replace")),
                  timed_out=timed_out, truncated=truncated, duration_ms=int((time.monotonic() - started) * 1000))
        run.notices, run.error = split_stderr(run.stderr)
        return run

    @staticmethod
    def _signal(process, signum):
        # Signal the whole process group so helpers cannot keep the output pipes open.
        try:
            os.killpg(process.pid, signum)
        except (ProcessLookupError, PermissionError):
            try:
                process.send_signal(signum)
            except ProcessLookupError:
                pass
