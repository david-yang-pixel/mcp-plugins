"""plugin_lambda_logs: CloudWatch logs of SaaS plugin Lambdas.

Same query as the manual procedure in the "Teleport 查询 SaaS 插件 Lambda 指南"
(``tsh aws --app aws-support-logs logs filter-log-events --region us-east-1
--log-group-name /aws/lambda/<function_name> ...``), sent with botocore through a
long-running ``tsh proxy aws`` (aws_logs.py). Nothing is truncated; a large range
is fetched over several calls with ``next_token``.
"""
from __future__ import annotations

import asyncio
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from db_gateway.core import _audit_path, _db_name, _metadata_kwargs, _prepare_database, _query_limiter
from db_gateway.log import get_logger
from db_gateway.pg_pool import run_metadata_query
from db_gateway.postgres import TeleportAuthExpired, audit_log
from db_gateway.aws_logs import classify_page_error, get_logs_client, reset_proxy
from db_gateway.teleport_cli import (
    TeleportNotAuthorized,
    TshTimeout,
    acquire_before,
    classify_failure,
)
from db_gateway.tsh_daemons import TshDaemonError, TshDaemonTimeout

logger = get_logger(module="lambda_logs")

TOOL_NAME = "plugin_lambda_logs"
FUNCTION_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MIN_TIMESTAMP_MS = 10**11          # smaller numbers are almost certainly seconds
CLOUDWATCH_MAX_PAGE = 10000
RESOLVE_QUERY_TIMEOUT_SECONDS = 10
RESOLVE_TOTAL_TIMEOUT_SECONDS = 15
PAGING_BUDGET_FRACTION = 0.6       # stop paging after this share of the budget
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

RESOLVE_SQL = """
SELECT i.plugin_unique_identifier, i.runtime_type, r.function_name, r.function_url
FROM public.plugin_installations AS i
LEFT JOIN public.serverless_runtimes AS r
  ON r.plugin_unique_identifier = i.plugin_unique_identifier
WHERE i.tenant_id = %s::uuid AND i.plugin_id = %s
"""

INSTALLATION_WARNING = (
    "安装表反映当前状态；故障后升级或重装过，请按故障时的版本用 db_gateway_query 查 serverless_runtimes。"
)
EMPTY_WARNING = (
    "没有匹配的日志：先去掉 filter_pattern、把窗口放宽到故障前后各 5 分钟，再核对时区和该工作空间"
    "实际安装的 checksum；同一函数被多个工作空间共用，关联客户请求需要时间或 RequestId。"
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


class _InvalidArgument(ValueError):
    pass


def parse_time_ms(value: Any, *, default_timezone: str) -> int:
    """Millisecond epoch from an ISO 8601 string or a millisecond timestamp.

    Strings without a zone are read in ``default_timezone``. Raises ValueError.
    """
    if isinstance(value, bool):
        raise ValueError("时间不能是布尔值")
    if isinstance(value, (int, float)):
        ms = int(value)
    elif isinstance(value, str) and value.strip().isdigit():
        ms = int(value.strip())
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if text[-1] in "Zz":
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            raise ValueError(f"无法解析时间 {value!r}：用 ISO 8601 或毫秒时间戳") from None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo(default_timezone))
        return (dt - _EPOCH) // timedelta(milliseconds=1)
    else:
        raise ValueError(f"无法解析时间 {value!r}：用 ISO 8601 或毫秒时间戳")
    if ms < MIN_TIMESTAMP_MS:
        raise ValueError(f"时间戳 {value!r} 看起来是秒级时间戳，请改用毫秒")
    return ms


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise _InvalidArgument(f"{name} 必须是正整数")
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and value.strip().isdigit():
        number = int(value.strip())
    else:
        raise _InvalidArgument(f"{name} 必须是正整数")
    if number < 1:
        raise _InvalidArgument(f"{name} 必须是正整数")
    return number


def _iso(ms: int, tz: timezone | ZoneInfo = timezone.utc) -> str:
    dt = (_EPOCH + timedelta(milliseconds=ms)).astimezone(tz)
    text = dt.isoformat(timespec="milliseconds")
    return text.replace("+00:00", "Z") if tz is timezone.utc else text


def _event(raw: dict[str, Any]) -> dict[str, Any]:
    ts = raw.get("timestamp")
    return {
        "timestamp": _iso(int(ts)) if isinstance(ts, (int, float)) else ts,
        "log_stream": raw.get("logStreamName"),
        "event_id": raw.get("eventId"),
        "message": raw.get("message"),
    }


async def _fetch_page(cfg: Any, params: dict[str, Any], deadline: float) -> tuple[dict | None, str, bool]:
    """One FilterLogEvents call: (response, error message, timed_out).

    A proxy failure (local 403, connection/SSL error) restarts the proxy and the
    page is retried once within the same deadline.
    """
    for attempt in range(2):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None, "", True
        client = await get_logs_client(cfg, deadline)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None, "", True
        try:
            data = await asyncio.wait_for(
                asyncio.to_thread(client.filter_log_events, **params), timeout=remaining,
            )
        except asyncio.TimeoutError:
            return None, "", True
        except Exception as e:  # botocore errors, classified below
            kind, message = classify_page_error(e)
            if kind == "timeout":
                return None, "", True
            if kind == "proxy" and attempt == 0:
                logger.warning("plugin_lambda_logs_proxy_retry", error=message[:300])
                reset_proxy()
                continue
            return None, message, False
        return (data if isinstance(data, dict) else None), "", False
    raise AssertionError("unreachable")


async def execute_plugin_lambda_logs(
    *,
    cfg: Any,
    function_name: Any = None,
    tenant_id: Any = None,
    plugin_id: Any = None,
    start_time: Any = None,
    end_time: Any = None,
    since_minutes: Any = None,
    filter_pattern: Any = None,
    log_stream_names: Any = None,
    limit: Any = None,
    next_token: Any = None,
) -> dict[str, Any]:
    started = time.monotonic()
    tools = cfg.teleport_tools
    lc = tools.lambda_logs
    budget = float(tools.time_budget_seconds)
    deadline = started + budget
    audit: dict[str, Any] = {"has_next_token": bool(next_token)}

    def elapsed() -> int:
        return int((time.monotonic() - started) * 1000)

    def fail(code: str, error: str, *, record: bool = True, **extra: Any) -> dict[str, Any]:
        logger.warning("plugin_lambda_logs_failed", code=code, **{
            k: audit.get(k) for k in ("function_name", "start_ms", "end_ms")
        })
        if record:
            audit_log(log_path=_audit_path(cfg), database="", sql="", status=code,
                      elapsed_ms=elapsed(), error=error, tool=TOOL_NAME, **audit)
        return {"success": False, "code": code, "error": error, "elapsed_ms": elapsed(),
                **({"function_name": audit["function_name"]} if audit.get("function_name") else {}),
                **extra}

    # --- arguments -------------------------------------------------------------
    warnings: list[str] = []
    try:
        has_fn = function_name not in (None, "")
        has_ws = tenant_id not in (None, "") or plugin_id not in (None, "")
        if has_fn == has_ws:
            raise _InvalidArgument("function_name 与 tenant_id + plugin_id 必须恰好给一种")
        if has_fn:
            if not isinstance(function_name, str) or not FUNCTION_NAME_RE.match(function_name):
                raise _InvalidArgument("function_name 只能包含字母、数字、- 和 _，最长 64 个字符")
        else:
            if tenant_id in (None, "") or plugin_id in (None, ""):
                raise _InvalidArgument("按工作空间定位时 tenant_id 和 plugin_id 都要给")
            try:
                tenant_id = str(uuid.UUID(str(tenant_id)))
            except ValueError:
                raise _InvalidArgument(f"tenant_id 不是 UUID：{tenant_id!r}") from None
            if (not isinstance(plugin_id, str) or plugin_id.count("/") != 1
                    or not all(part.strip() for part in plugin_id.split("/"))):
                raise _InvalidArgument(f"plugin_id 应为 publisher/name，例如 langgenius/openai：{plugin_id!r}")

        has_since = since_minutes is not None
        has_range = start_time is not None or end_time is not None
        if has_since == has_range:
            raise _InvalidArgument("since_minutes 与 start_time + end_time 必须恰好给一种")
        if has_since:
            minutes = _positive_int(since_minutes, "since_minutes")
            end_ms = int(time.time() * 1000)
            start_ms = end_ms - minutes * 60_000
        else:
            if start_time is None or end_time is None:
                raise _InvalidArgument("start_time 和 end_time 要一起给")
            try:
                start_ms = parse_time_ms(start_time, default_timezone=lc.default_timezone)
                end_ms = parse_time_ms(end_time, default_timezone=lc.default_timezone)
            except ValueError as e:
                raise _InvalidArgument(str(e)) from None
            if end_ms <= start_ms:
                raise _InvalidArgument("end_time 必须晚于 start_time")

        page_limit = lc.default_limit if limit is None else _positive_int(limit, "limit")
        if page_limit > lc.max_limit:
            warnings.append(f"limit {page_limit} 超过上限，已按 {lc.max_limit} 处理")
            page_limit = lc.max_limit

        if filter_pattern in ("", None):
            filter_pattern = None
        elif not isinstance(filter_pattern, str):
            raise _InvalidArgument("filter_pattern 必须是字符串")
        if log_stream_names in (None, []):
            log_stream_names = None
        elif (not isinstance(log_stream_names, list)
              or not all(isinstance(n, str) and n for n in log_stream_names)):
            raise _InvalidArgument("log_stream_names 必须是非空字符串数组")
        if next_token in ("", None):
            next_token = None
        elif not isinstance(next_token, str):
            raise _InvalidArgument("next_token 必须是字符串")
        elif has_since:
            # since_minutes is relative to now: a token replayed against a shifted
            # window could skip events.
            raise _InvalidArgument(
                "带 next_token 续页时不要用 since_minutes：把上次返回的 window.start_ms、"
                "window.end_ms 作为 start_time、end_time，再带上 next_token"
            )
    except _InvalidArgument as e:
        return fail("INVALID_ARGUMENT", str(e), record=False)

    audit.update(start_ms=start_ms, end_ms=end_ms, filter_pattern=filter_pattern, limit=page_limit)

    slot = _limiter(max(1, int(tools.max_concurrent)))
    if not await acquire_before(slot, deadline):
        return fail("TSH_TIMEOUT", "等待并发槽位超过了时间预算（网关繁忙），稍后重试")
    try:
        # --- locate the function -------------------------------------------------
        plugin_unique_identifier = None
        if not has_fn:
            audit.update(tenant_id=tenant_id, plugin_id=plugin_id)
            # Same port db_gateway_query uses: the host tunnel, or db-gateway's own
            # plugin tunnel once teleport_db_plugin is set.
            db_port, prepare_error = await _prepare_database(cfg, "plugin")
            if prepare_error:
                return fail(*prepare_error)
            kwargs = _metadata_kwargs(cfg, db_name=_db_name(cfg, "plugin"), db_port=db_port)
            max_queries = max(1, int(getattr(cfg.postgres, "max_concurrent_queries", 3) or 3))

            async def resolve() -> list[list[Any]]:
                query_limiter = await _query_limiter("plugin", db_port, max_queries)
                async with query_limiter:   # shared with db_gateway_query: wait inside the bound
                    return await run_metadata_query(
                        **kwargs, sql=RESOLVE_SQL, params=(tenant_id, plugin_id),
                        timeout=RESOLVE_QUERY_TIMEOUT_SECONDS,
                    )

            try:
                rows = await asyncio.wait_for(
                    resolve(),
                    timeout=max(0.1, min(RESOLVE_TOTAL_TIMEOUT_SECONDS, deadline - time.monotonic())),
                )
            except TeleportAuthExpired as e:
                return fail("TELEPORT_EXPIRED", str(e))
            except asyncio.TimeoutError:
                return fail("QUERY_ERROR", "查询插件安装记录超时（含排队等待）")
            except Exception as e:
                return fail("QUERY_ERROR", f"查询插件安装记录失败：{e}")
            if not rows:
                return fail("UNKNOWN_FUNCTION", f"该工作空间没有安装 {plugin_id}")
            plugin_unique_identifier, runtime_type, resolved, function_url = (list(rows[0]) + [None] * 4)[:4]
            if not resolved:
                return fail("UNKNOWN_FUNCTION",
                            f"有安装记录（runtime_type={runtime_type}）但没有运行映射")
            url_re = rf"^https://[^/]+\.lambda-url\.{re.escape(lc.region)}\.on\.aws(/|$)"
            if not re.match(url_re, str(function_url or "")):
                return fail("UNKNOWN_FUNCTION",
                            f"运行后端不是 Lambda {lc.region}（function_url={function_url}）")
            function_name = str(resolved)
            warnings.append(INSTALLATION_WARNING)
        log_group = f"/aws/lambda/{function_name}"
        audit.update(function_name=function_name, log_group=log_group)

        # --- fetch pages ---------------------------------------------------------
        try:
            events: list[dict[str, Any]] = []
            token = next_token
            pages = 0
            complete = False
            while True:
                if time.monotonic() >= deadline:
                    if pages == 0:
                        return fail("TSH_TIMEOUT", "开始查询前时间预算就用完了（排队或登录耗时），稍后重试")
                    warnings.append(f"第 {pages + 1} 页之前时间预算用完，已返回之前取到的事件；用 next_token 继续")
                    break
                params: dict[str, Any] = {
                    "logGroupName": log_group,
                    "startTime": start_ms,
                    "endTime": end_ms - 1,
                }
                if filter_pattern is not None:
                    params["filterPattern"] = filter_pattern
                if log_stream_names:
                    params["logStreamNames"] = list(log_stream_names)
                params["limit"] = min(page_limit - len(events), CLOUDWATCH_MAX_PAGE)
                if token:
                    params["nextToken"] = token
                data, error, timed_out = await _fetch_page(cfg, params, deadline)
                if data is None:
                    if timed_out:
                        reason, code = "时间预算用完", "LOGS_TIMEOUT"
                        error = "第一页就超过了时间预算：请缩小时间窗口或加 filter_pattern"
                    else:
                        reason, code = f"AWS 报错：{error[:300]}", "AWS_ERROR"
                        error = error[:800] or "CloudWatch 返回了无法解析的结果"
                    if pages == 0:
                        return fail(code, error)
                    warnings.append(f"第 {pages + 1} 页中断（{reason}），已返回之前取到的事件；用 next_token 继续")
                    break
                pages += 1
                events.extend(data.get("events") or [])
                token = data.get("nextToken") or None
                if token is None:
                    complete = True
                    break
                if len(events) >= page_limit:
                    warnings.append("已达到 limit，用 next_token 继续")
                    break
                if time.monotonic() - started >= budget * PAGING_BUDGET_FRACTION:
                    warnings.append("时间预算用完，用 next_token 继续")
                    break
        except TshDaemonTimeout as e:
            return fail("TSH_TIMEOUT", f"AWS 代理没有在时间预算内就绪，稍后重试：{str(e)[:400]}")
        except TshDaemonError as e:
            classified = await classify_failure(cfg, str(e)[:600])
            if isinstance(classified, TeleportAuthExpired):
                return fail("TELEPORT_EXPIRED", str(classified))
            return fail("NOT_AUTHORIZED", f"{classified}；需要 Teleport 角色 support-lambda-logs")
        except TeleportAuthExpired as e:
            return fail("TELEPORT_EXPIRED", str(e))
        except TshTimeout as e:
            return fail("TSH_TIMEOUT", str(e))
        except TeleportNotAuthorized as e:
            return fail("NOT_AUTHORIZED", f"{e}；需要 Teleport 角色 support-lambda-logs")
    finally:
        slot.release()

    if complete and not events:
        warnings.append(EMPTY_WARNING)
    if not complete:
        warnings.append(
            f"续页：start_time={start_ms}、end_time={end_ms}（即 window.start_ms / window.end_ms），"
            "其他参数不变，再带上 next_token"
        )
    tz = ZoneInfo(lc.default_timezone)
    payload: dict[str, Any] = {
        "success": True,
        "code": "",
        "error": None,
        "function_name": function_name,
        "log_group": log_group,
        "region": lc.region,
        "window": {
            "start_ms": start_ms,
            "end_ms": end_ms,
            "start_utc": _iso(start_ms),
            "end_utc": _iso(end_ms),
            "start_local": _iso(start_ms, tz),
            "end_local": _iso(end_ms, tz),
            "timezone": lc.default_timezone,
        },
        "events": [_event(e) for e in events],
        "event_count": len(events),
        "next_token": None if complete else token,
        "complete": complete,
        "warnings": warnings,
    }
    if plugin_unique_identifier is not None:
        payload["plugin_unique_identifier"] = plugin_unique_identifier
    payload["elapsed_ms"] = elapsed()
    audit_log(log_path=_audit_path(cfg), database="", sql="", status="OK",
              row_count=len(events), elapsed_ms=payload["elapsed_ms"], tool=TOOL_NAME,
              complete=complete, **audit)
    logger.info("plugin_lambda_logs", function_name=function_name, events=len(events),
                complete=complete, pages=pages)
    return payload
