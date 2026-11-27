"""kube_read: read-only kubectl through ``tsh kubectl``.

The kubectl arguments are passed through after a light check that keeps to
read-only verbs and rejects flags that switch identity/cluster, stream forever
or read/write local files (mistake prevention; what can be read is decided by
the Teleport role). ``tsh kubectl`` exits 0 even when kubectl fails, so errors
are detected from stderr.
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Any

import yaml

from db_gateway.core import _audit_path
from db_gateway.log import get_logger
from db_gateway.postgres import TeleportAuthExpired, audit_log
from db_gateway.teleport_cli import (
    TeleportNotAuthorized,
    TshResult,
    TshTimeout,
    acquire_before,
    ensure_shadow_home,
    run_tsh_with_login,
    tsh_login,
)

logger = get_logger(module="kube_read")

TOOL_NAME = "kube_read"
LOGIN_ARGS = ["kube", "login", "--all"]
MAX_ARGS = 64
MAX_ARG_LENGTH = 4096
READ_ONLY_VERBS = frozenset({
    "get", "describe", "logs", "top", "explain", "api-resources", "api-versions",
    "version", "auth",
})

_READS_FILE = "读本地文件"
_SWITCHES_TARGET = "换身份或目标"
_STREAMS = "持续输出"
_WRITES_FILE = "写本地文件"
_BLOCKED_LONG = {
    "--filename": _READS_FILE, "--kustomize": _READS_FILE,
    "--kubeconfig": _SWITCHES_TARGET, "--context": _SWITCHES_TARGET,
    "--cluster": _SWITCHES_TARGET, "--user": _SWITCHES_TARGET, "--as": _SWITCHES_TARGET,
    "--as-group": _SWITCHES_TARGET, "--as-uid": _SWITCHES_TARGET, "--token": _SWITCHES_TARGET,
    "--server": _SWITCHES_TARGET, "--certificate-authority": _SWITCHES_TARGET,
    "--client-certificate": _SWITCHES_TARGET, "--client-key": _SWITCHES_TARGET,
    "--insecure-skip-tls-verify": _SWITCHES_TARGET, "--tls-server-name": _SWITCHES_TARGET,
    "--username": _SWITCHES_TARGET, "--password": _SWITCHES_TARGET,
    "--follow": _STREAMS, "--watch": _STREAMS, "--watch-only": _STREAMS,
    "--cache-dir": _WRITES_FILE, "--profile": _WRITES_FILE, "--profile-output": _WRITES_FILE,
}
# Short flags: stop at one that takes a value (the rest is its value).
_SHORT_TAKES_VALUE = frozenset("nolcL")
_BLOCKED_SHORT = {"f": _READS_FILE, "k": _READS_FILE, "w": _STREAMS, "s": _SWITCHES_TARGET}

KUBECTL_ERROR_RE = re.compile(
    r"^(error:|Error from server|Error in configuration|Unable to connect to the server)", re.M
)
# Credential problems that a fresh `tsh kube login --all` fixes. Calibrate against
# real tsh output (Task 0 could not reproduce one: tsh re-issued certs by itself).
KUBE_LOGIN_MARKERS: tuple[re.Pattern[str], ...] = (
    re.compile(r"getting credentials"),
    re.compile(r"tsh kube login"),
    re.compile(r"exec plugin"),
    re.compile(r"context was not found"),
    re.compile(r'context "[^"]+" does not exist'),
)

_semaphore: asyncio.Semaphore | None = None
_semaphore_size: int | None = None


def _reset_state() -> None:
    global _semaphore, _semaphore_size
    _semaphore = None
    _semaphore_size = None


def _limiter(size: int) -> asyncio.Semaphore:
    global _semaphore, _semaphore_size
    if _semaphore is None or _semaphore_size != size:
        _semaphore = asyncio.Semaphore(size)
        _semaphore_size = size
    return _semaphore


def validate_kubectl_args(args: Any) -> list[str]:
    """Return ``args`` if it is a read-only kubectl invocation; else raise ValueError."""
    if not isinstance(args, list) or not args:
        raise ValueError("args 必须是非空的字符串数组，例如 [\"get\", \"pods\", \"-n\", \"dify-plugin\"]")
    if len(args) > MAX_ARGS:
        raise ValueError(f"args 最多 {MAX_ARGS} 项")
    for arg in args:
        if not isinstance(arg, str):
            raise ValueError(f"args 只能包含字符串：{arg!r}")
        if len(arg) > MAX_ARG_LENGTH or "\x00" in arg:
            raise ValueError(f"参数过长或含 NUL 字符：{arg[:80]!r}")
    verb = args[0]
    if verb not in READ_ONLY_VERBS:
        raise ValueError(
            f"第一个参数必须是只读子命令（{', '.join(sorted(READ_ONLY_VERBS))}），收到 {verb!r}"
        )
    if verb == "auth" and (len(args) < 2 or args[1] != "can-i"):
        raise ValueError("auth 只允许 auth can-i")
    for arg in args[1:]:
        if arg == "--":
            break
        if arg.startswith("--"):
            name = arg.split("=", 1)[0]
            if name in _BLOCKED_LONG:
                raise ValueError(f"不允许参数 {arg}（{_BLOCKED_LONG[name]}）")
        elif arg.startswith("-") and len(arg) >= 2:
            for ch in arg[1:]:
                if ch in _SHORT_TAKES_VALUE:
                    break
                if ch in _BLOCKED_SHORT:
                    raise ValueError(f"不允许参数 {arg}（-{ch}：{_BLOCKED_SHORT[ch]}）")
    return args


def resolve_context(kubeconfig_path: str, *, proxy: str, cluster_name: str) -> str | None:
    """The kubeconfig context `tsh kube login` created for ``cluster_name``, if exactly one."""
    try:
        with open(kubeconfig_path, encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
        names = [c.get("name") for c in config.get("contexts") or [] if isinstance(c, dict)]
    except (OSError, yaml.YAMLError, AttributeError):
        return None
    host = proxy.split(":")[0]
    exact, prefix = f"{host}-{cluster_name}", f"{host}-{cluster_name}-"
    matches = [n for n in names if isinstance(n, str) and (n == exact or n.startswith(prefix))]
    return matches[0] if len(matches) == 1 else None


def kubectl_failed(result: TshResult) -> bool:
    return result.exit_code not in (0, None) or bool(KUBECTL_ERROR_RE.search(result.stderr))


def _needs_login(result: TshResult) -> bool:
    return any(p.search(result.stderr) for p in KUBE_LOGIN_MARKERS)


async def execute_kube_read(*, cfg: Any, cluster: Any, args: Any) -> dict[str, Any]:
    started = time.monotonic()
    tools = cfg.teleport_tools
    kube = tools.kube
    deadline = started + float(tools.time_budget_seconds)

    def elapsed() -> int:
        return int((time.monotonic() - started) * 1000)

    def fail(code: str, error: str, *, record: bool = True) -> dict[str, Any]:
        logger.warning("kube_read_failed", code=code, cluster=cluster)
        if record:
            audit_log(log_path=_audit_path(cfg), database="", sql="", status=code,
                      elapsed_ms=elapsed(), error=error, tool=TOOL_NAME, cluster=cluster,
                      args=args)
        return {"success": False, "code": code, "error": error, "elapsed_ms": elapsed(),
                "cluster": cluster}

    clusters = dict(kube.clusters)
    if not isinstance(cluster, str) or cluster not in clusters:
        aliases = ", ".join(f"{k}={v}" for k, v in clusters.items())
        return fail("INVALID_ARGUMENT", f"未知集群 {cluster!r}，可用：{aliases}", record=False)
    try:
        args = validate_kubectl_args(args)
    except ValueError as e:
        return fail("INVALID_ARGUMENT", str(e), record=False)
    cluster_name = clusters[cluster]

    slot = _limiter(max(1, int(tools.max_concurrent)))
    if not await acquire_before(slot, deadline):
        return fail("TSH_TIMEOUT", "等待并发槽位超过了时间预算（网关繁忙），稍后重试")
    try:
        try:
            await ensure_shadow_home(cfg)
            context = resolve_context(kube.kubeconfig_path, proxy=cfg.teleport.proxy,
                                      cluster_name=cluster_name)
            if context is None:
                await tsh_login(LOGIN_ARGS, cfg=cfg, deadline=deadline, not_before=started)
                context = resolve_context(kube.kubeconfig_path, proxy=cfg.teleport.proxy,
                                          cluster_name=cluster_name)
                if context is None:
                    return fail("NOT_AUTHORIZED", f"Teleport 里看不到集群 {cluster_name}")
            # --context right after the verb: never behind a user `--`, never before
            # the verb (tsh 18.0 parses it as its own flag there).
            argv = ["kubectl", args[0], "--context", context, *args[1:]]
            result = await run_tsh_with_login(
                argv, cfg=cfg, deadline=deadline, login_args=LOGIN_ARGS,
                needs_login=_needs_login, max_output_bytes=kube.max_output_bytes,
            )
        except TeleportAuthExpired as e:
            return fail("TELEPORT_EXPIRED", str(e))
        except TshTimeout as e:
            return fail("TSH_TIMEOUT", str(e))
        except TeleportNotAuthorized as e:
            return fail("NOT_AUTHORIZED", str(e))
    finally:
        slot.release()

    failed = kubectl_failed(result)
    if result.timed_out and not failed and not result.stdout:
        # Nothing ran or nothing came back before the deadline: not a success.
        return fail("TSH_TIMEOUT", "到时间预算 kubectl 还没有任何输出，稍后重试，或用 --tail、--since、-l 缩小范围")
    warnings: list[str] = []
    if result.truncated:
        warnings.append(
            f"输出超过 {kube.max_output_bytes} 字节，只保留开头；用 --tail、--since、--since-time、-l 缩小范围"
        )
    if result.timed_out and not failed:
        warnings.append("到时间预算被中止，输出不完整；用 --tail、--since、-l 缩小范围")
    payload: dict[str, Any] = {
        "success": not failed,
        "code": "KUBECTL_ERROR" if failed else "",
        "error": (result.stderr.strip()[:2000] or f"kubectl 退出码 {result.exit_code}") if failed else None,
        "cluster": cluster,
        "context": context,
        "output": result.stdout,
        "stderr": result.stderr,
        "exit_code": result.exit_code,
        "truncated": result.truncated,
        "timed_out": result.timed_out,
        "warnings": warnings,
        "elapsed_ms": elapsed(),
    }
    audit_log(log_path=_audit_path(cfg), database="", sql="",
              status=payload["code"] or "OK", elapsed_ms=payload["elapsed_ms"],
              error=payload["error"], tool=TOOL_NAME, cluster=cluster, args=args,
              exit_code=result.exit_code, output_bytes=len(result.stdout.encode("utf-8")),
              truncated=result.truncated, timed_out=result.timed_out, kubectl_error=failed)
    logger.info("kube_read", cluster=cluster, verb=args[0], exit_code=result.exit_code,
                kubectl_error=failed, timed_out=result.timed_out)
    return payload
