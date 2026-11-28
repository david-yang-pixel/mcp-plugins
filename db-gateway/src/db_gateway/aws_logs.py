"""CloudWatch Logs access for plugin_lambda_logs through a long-running `tsh proxy aws`.

Measured on the server: one page costs ~0.75 s through a warm proxy + botocore,
against 1.4–1.9 s for a fresh `tsh aws` + aws CLI process per page.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import time
from pathlib import Path
from typing import Any

import botocore.session
from botocore.config import Config
from botocore.exceptions import (
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    HTTPClientError,
    ProxyConnectionError,
    ReadTimeoutError,
    SSLError,
)

from db_gateway.log import get_logger
from db_gateway.teleport_cli import tsh_login
from db_gateway.tsh_daemons import TshDaemon

logger = get_logger(module="aws_logs")

_ENV_KEYS = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_CA_BUNDLE", "HTTPS_PROXY")
_EXPORT_RE = re.compile(r"^\s*export\s+(" + "|".join(_ENV_KEYS) + r")=(.*)$")

CONNECT_TIMEOUT_SECONDS = 10
READ_TIMEOUT_SECONDS = 30

# Private copies of each proxy's local CA, outside every TELEPORT_HOME. The proxy
# writes `<app>-localca.pem` into the key ring once, at start, and the client
# reads it on every new TLS connection; but other tsh processes rewrite the key
# ring (the first `tsh kubectl` of a generation, `tsh apps login`) and can drop
# the file when they overlap with a proxy start. Copies older than this go.
CA_COPY_DIR = Path("/tmp/db-gateway/aws-proxy-ca")
CA_COPY_KEEP_SECONDS = 600.0


def _private_ca_copy(path: str) -> str:
    """Copy the proxy's CA where no tsh process writes; the reported path if unreadable."""
    try:
        data = Path(path).read_bytes()
    except OSError as e:
        logger.warning("aws_proxy_ca_copy_failed", error=f"{type(e).__name__}: {e}"[:300])
        return path
    CA_COPY_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = CA_COPY_DIR / f"ca-{hashlib.sha256(data).hexdigest()[:16]}.pem"
    if target.exists():
        os.utime(target)
    else:
        tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, target)
    cutoff = time.time() - CA_COPY_KEEP_SECONDS
    for old in CA_COPY_DIR.glob("ca-*.pem"):
        try:
            if old != target and old.stat().st_mtime < cutoff:
                old.unlink()
        except OSError:
            pass
    return str(target)


def parse_proxy_env(lines: list[str]) -> dict[str, str] | None:
    """The four ``export`` lines `tsh proxy aws` prints once it listens; None until all seen."""
    env: dict[str, str] = {}
    for line in lines:
        m = _EXPORT_RE.match(line)
        if m:
            env[m.group(1)] = m.group(2).strip().strip("\"'")
    if not all(k in env for k in _ENV_KEYS):
        return None
    # The path is under TELEPORT_HOME=<sessions>/current/...: resolve it to this
    # proxy's generation, then keep a copy no key-ring rewrite can remove.
    env["AWS_CA_BUNDLE"] = _private_ca_copy(os.path.realpath(env["AWS_CA_BUNDLE"]))
    return env


async def _apps_login(cfg: Any, deadline: float) -> None:
    lc = cfg.teleport_tools.lambda_logs
    await tsh_login(["apps", "login", "--aws-role", lc.aws_role, lc.app], cfg=cfg, deadline=deadline)


# Rotated on every renewal (tsh_daemons.rotate_running): a new proxy with fresh
# credentials starts before the old one is stopped.
AWS_PROXY = TshDaemon(
    "aws_proxy",
    args=lambda cfg: ["proxy", "aws", "--app", cfg.teleport_tools.lambda_logs.app],
    parse=parse_proxy_env,
    before_start=_apps_login,
    rotate_on_renew=True,
)

_session: Any = None
_client: Any = None
_client_key: tuple[str, ...] | None = None


def _reset_state() -> None:
    global _session, _client, _client_key
    _session = _client = _client_key = None


def reset_proxy() -> None:
    """Restart the proxy and rebuild the client on next use (the proxy misbehaved)."""
    global _client, _client_key
    AWS_PROXY.mark_stale()
    _client = _client_key = None


async def get_logs_client(cfg: Any, deadline: float) -> Any:
    """A botocore CloudWatch Logs client wired to the running proxy (started if needed)."""
    global _session, _client, _client_key
    env = await AWS_PROXY.ensure(cfg, deadline)
    region = cfg.teleport_tools.lambda_logs.region
    key = (env["HTTPS_PROXY"], env["AWS_ACCESS_KEY_ID"], env["AWS_CA_BUNDLE"], region)
    if _client is None or _client_key != key:
        if _session is None:
            _session = botocore.session.get_session()
        _client = await asyncio.to_thread(
            _session.create_client,
            "logs",
            region_name=region,
            aws_access_key_id=env["AWS_ACCESS_KEY_ID"],
            aws_secret_access_key=env["AWS_SECRET_ACCESS_KEY"],
            verify=env["AWS_CA_BUNDLE"],
            config=Config(
                proxies={"https": env["HTTPS_PROXY"]},
                connect_timeout=CONNECT_TIMEOUT_SECONDS,
                read_timeout=READ_TIMEOUT_SECONDS,
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )
        _client_key = key
    return _client


def classify_page_error(exc: BaseException) -> tuple[str, str]:
    """("proxy" | "timeout" | "aws", message) for an exception from filter_log_events."""
    if isinstance(exc, ClientError):
        err = exc.response.get("Error") or {}
        code, message = str(err.get("Code") or ""), str(err.get("Message") or "")
        if code == "403":
            # The local proxy (not AWS) rejected the request: signature/cert no longer valid.
            return "proxy", f"本地 AWS 代理拒绝了请求（403）{message}".strip()
        return "aws", f"{code}: {message}".strip(": ")
    if isinstance(exc, ReadTimeoutError):
        return "timeout", str(exc)
    if isinstance(exc, (EndpointConnectionError, ProxyConnectionError, SSLError,
                        ConnectionClosedError, ConnectTimeoutError, HTTPClientError)):
        return "proxy", f"连接本地 AWS 代理失败：{exc}"
    return "aws", f"{type(exc).__name__}: {exc}"
