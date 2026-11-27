"""CloudWatch Logs access for plugin_lambda_logs through a long-running `tsh proxy aws`.

Measured on the server: one page costs ~0.75 s through a warm proxy + botocore,
against 1.4–1.9 s for a fresh `tsh aws` + aws CLI process per page.
"""
from __future__ import annotations

import asyncio
import re
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

from db_gateway.teleport_cli import tsh_login
from db_gateway.tsh_daemons import TshDaemon

_ENV_KEYS = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_CA_BUNDLE", "HTTPS_PROXY")
_EXPORT_RE = re.compile(r"^\s*export\s+(" + "|".join(_ENV_KEYS) + r")=(.*)$")

CONNECT_TIMEOUT_SECONDS = 10
READ_TIMEOUT_SECONDS = 30


def parse_proxy_env(lines: list[str]) -> dict[str, str] | None:
    """The four ``export`` lines `tsh proxy aws` prints once it listens; None until all seen."""
    env: dict[str, str] = {}
    for line in lines:
        m = _EXPORT_RE.match(line)
        if m:
            env[m.group(1)] = m.group(2).strip().strip("\"'")
    return env if all(k in env for k in _ENV_KEYS) else None


async def _apps_login(cfg: Any, deadline: float) -> None:
    lc = cfg.teleport_tools.lambda_logs
    await tsh_login(["apps", "login", "--aws-role", lc.aws_role, lc.app], cfg=cfg, deadline=deadline)


AWS_PROXY = TshDaemon(
    "aws_proxy",
    args=lambda cfg: ["proxy", "aws", "--app", cfg.teleport_tools.lambda_logs.app],
    parse=parse_proxy_env,
    before_start=_apps_login,
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
