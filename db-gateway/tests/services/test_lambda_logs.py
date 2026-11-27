"""plugin_lambda_logs: CloudWatch logs of SaaS plugin Lambdas through `tsh proxy aws` + botocore."""
import asyncio
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from botocore.exceptions import ClientError, ProxyConnectionError

from db_gateway.lambda_logs import execute_plugin_lambda_logs, parse_time_ms
from db_gateway.postgres import TeleportAuthExpired
from db_gateway.teleport import TshStatus
from db_gateway.teleport_cli import TshTimeout
from db_gateway.tsh_daemons import TshDaemonError

EV = {"timestamp": 1795775460000, "logStreamName": "s", "eventId": "1", "message": "m",
      "ingestionTime": 1795775460100}


def page(events, token=None):
    return {"events": events, "searchedLogStreams": [], **({"nextToken": token} if token else {})}


def client_error(code, message=""):
    return ClientError({"Error": {"Code": code, "Message": message}}, "FilterLogEvents")


class FakeLogsClient:
    """Stands in for the botocore logs client: queued responses, recorded calls."""

    def __init__(self):
        self.queue = []
        self.calls = []

    def push(self, resp=None, *, error=None, sleep=0):
        self.queue.append((resp, error, sleep))

    def filter_log_events(self, **kwargs):
        self.calls.append(kwargs)
        resp, error, sleep = self.queue.pop(0) if self.queue else (page([]), None, 0)
        if sleep:
            time.sleep(sleep)
        if error is not None:
            raise error
        return resp


@pytest.fixture
def logs(monkeypatch):
    client = FakeLogsClient()
    client.get_calls = 0
    client.resets = 0
    client.get_error = None

    async def get_logs_client(cfg, deadline):
        client.get_calls += 1
        if client.get_error is not None:
            raise client.get_error
        return client

    def reset_proxy():
        client.resets += 1

    monkeypatch.setattr("db_gateway.lambda_logs.get_logs_client", get_logs_client)
    monkeypatch.setattr("db_gateway.lambda_logs.reset_proxy", reset_proxy)
    return client


@pytest.mark.parametrize("value,expected", [
    ("2026-11-27T18:31:00+08:00", 1795775460000),
    ("2026-11-27 18:31:00", 1795775460000),          # 无时区按北京时间
    ("2026-11-27T10:31:00Z", 1795775460000),
    (1795775460000, 1795775460000),
    ("1795775460000", 1795775460000),
])
def test_parse_time_ms(value, expected):
    assert parse_time_ms(value, default_timezone="Asia/Shanghai") == expected


@pytest.mark.parametrize("value", [1795775460, "1795775460", True, "昨天"])
def test_parse_time_ms_rejects(value):
    with pytest.raises(ValueError):
        parse_time_ms(value, default_timezone="Asia/Shanghai")


async def test_single_page_by_function_name(cfg, logs):
    logs.push(page([EV]))
    out = await execute_plugin_lambda_logs(cfg=cfg, function_name="9a442b2d0d9b43e56a190df6ca7dc207",
        start_time="2026-11-27 18:31:00", end_time="2026-11-27 18:32:00", filter_pattern='"ERROR"')
    assert out["success"] and out["complete"] is True and out["next_token"] is None
    assert out["log_group"] == "/aws/lambda/9a442b2d0d9b43e56a190df6ca7dc207"
    assert out["events"] == [{"timestamp": "2026-11-27T10:31:00.000Z", "log_stream": "s",
                              "event_id": "1", "message": "m"}]
    assert out["window"]["start_local"].startswith("2026-11-27T18:31:00")
    assert logs.calls == [{"logGroupName": "/aws/lambda/9a442b2d0d9b43e56a190df6ca7dc207",
                           "startTime": 1795775460000, "endTime": 1795775519999,
                           "filterPattern": '"ERROR"', "limit": 1000}]


async def test_dash_filter_streams_and_token_are_passed_as_is(cfg, logs):
    logs.push(page([EV]))
    await execute_plugin_lambda_logs(cfg=cfg, function_name="f", start_time=1795775460000,
                                     end_time=1795775520000, filter_pattern="-DEBUG", next_token="T0",
                                     log_stream_names=["2026/11/27/[$LATEST]abc"])
    call = logs.calls[0]
    assert (call["filterPattern"], call["nextToken"], call["logStreamNames"]) == (
        "-DEBUG", "T0", ["2026/11/27/[$LATEST]abc"])


async def test_long_message_is_not_truncated(cfg, logs):
    logs.push(page([{**EV, "message": "x" * 300_000}]))
    out = await execute_plugin_lambda_logs(cfg=cfg, function_name="f" * 32, since_minutes=10)
    assert len(out["events"][0]["message"]) == 300_000


async def test_limit_stops_with_token(cfg, logs):
    logs.push(page([EV, EV], token="T1"))
    out = await execute_plugin_lambda_logs(cfg=cfg, function_name="f" * 32, since_minutes=10, limit=2)
    assert (out["event_count"], out["next_token"], out["complete"]) == (2, "T1", False)
    assert len(logs.calls) == 1


async def test_empty_pages_with_token_keep_paging(cfg, logs):
    logs.push(page([], token="T1")); logs.push(page([], token="T2")); logs.push(page([EV]))
    out = await execute_plugin_lambda_logs(cfg=cfg, function_name="f" * 32, since_minutes=10)
    assert (out["event_count"], out["complete"]) == (1, True)
    assert [c.get("nextToken") for c in logs.calls] == [None, "T1", "T2"]


async def test_time_budget_returns_partial_with_token(cfg, logs):
    cfg.teleport_tools.time_budget_seconds = 2
    logs.push(page([EV], token="T1"), sleep=1.5); logs.push(page([EV]))
    out = await execute_plugin_lambda_logs(cfg=cfg, function_name="f" * 32, since_minutes=10)
    assert out["success"] and (out["event_count"], out["next_token"], out["complete"]) == (1, "T1", False)


async def test_first_page_timeout_is_logs_timeout(cfg, logs):
    cfg.teleport_tools.time_budget_seconds = 1
    logs.push(page([EV]), sleep=2.5)
    t0 = time.monotonic()
    out = await execute_plugin_lambda_logs(cfg=cfg, function_name="f" * 32, since_minutes=10)
    assert (out["success"], out["code"]) == (False, "LOGS_TIMEOUT") and time.monotonic() - t0 < 2.2


async def test_later_page_timeout_returns_partial(cfg, logs):
    cfg.teleport_tools.time_budget_seconds = 2
    logs.push(page([EV], token="T1")); logs.push(page([EV]), sleep=2.5)
    out = await execute_plugin_lambda_logs(cfg=cfg, function_name="f" * 32, since_minutes=10)
    assert out["success"] and (out["event_count"], out["next_token"], out["complete"]) == (1, "T1", False)


async def test_client_error_is_aws_error_with_code(cfg, logs):
    logs.push(error=client_error("ResourceNotFoundException", "The specified log group does not exist."))
    out = await execute_plugin_lambda_logs(cfg=cfg, function_name="f" * 32, since_minutes=10)
    assert out["code"] == "AWS_ERROR"
    assert out["error"].startswith("ResourceNotFoundException: The specified log group")
    assert len(logs.calls) == 1 and logs.resets == 0


async def test_proxy_403_restarts_proxy_and_retries_once(cfg, logs):
    logs.push(error=client_error("403")); logs.push(page([EV]))
    out = await execute_plugin_lambda_logs(cfg=cfg, function_name="f" * 32, since_minutes=10)
    assert out["success"] and out["event_count"] == 1
    assert (logs.resets, logs.get_calls, len(logs.calls)) == (1, 2, 2)


async def test_connection_error_retries_once_then_aws_error(cfg, logs):
    for _ in range(2):
        logs.push(error=ProxyConnectionError(proxy_url="http://127.0.0.1:1", error="refused"))
    out = await execute_plugin_lambda_logs(cfg=cfg, function_name="f" * 32, since_minutes=10)
    assert out["code"] == "AWS_ERROR" and "代理" in out["error"]
    assert (logs.resets, len(logs.calls)) == (1, 2)


async def test_later_page_aws_error_returns_partial(cfg, logs):
    logs.push(page([EV], token="T1")); logs.push(error=client_error("ThrottlingException", "Rate exceeded"))
    out = await execute_plugin_lambda_logs(cfg=cfg, function_name="f" * 32, since_minutes=10)
    assert out["success"] and out["next_token"] == "T1" and out["complete"] is False
    assert any("ThrottlingException" in w for w in out["warnings"])


@pytest.mark.parametrize("error,code", [
    (TeleportAuthExpired("gone"), "TELEPORT_EXPIRED"),
    (TshTimeout("login timeout"), "TSH_TIMEOUT"),
])
async def test_session_errors_from_proxy(cfg, logs, error, code):
    logs.get_error = error
    out = await execute_plugin_lambda_logs(cfg=cfg, function_name="f", since_minutes=5)
    assert out["code"] == code and logs.calls == []


async def test_daemon_error_without_session_is_expired(cfg, logs):
    logs.get_error = TshDaemonError("aws_proxy 启动失败（退出码 1）：ERROR: not logged in")
    out = await execute_plugin_lambda_logs(cfg=cfg, function_name="f", since_minutes=5)
    assert out["code"] == "TELEPORT_EXPIRED"


async def test_daemon_error_with_session_is_not_authorized(cfg, logs, monkeypatch):
    status = TshStatus(user="u", cluster="c", roles=["production-read-all"],
                       valid_until=datetime.now(timezone.utc), remaining=timedelta(hours=1))
    monkeypatch.setattr("db_gateway.teleport_cli.get_tsh_status", AsyncMock(return_value=status))
    logs.get_error = TshDaemonError('aws_proxy 启动失败（退出码 1）：ERROR: app "x" not found')
    out = await execute_plugin_lambda_logs(cfg=cfg, function_name="f", since_minutes=5)
    assert out["code"] == "NOT_AUTHORIZED" and "production-read-all" in out["error"]


async def test_queued_call_past_deadline_is_tsh_timeout(cfg, logs):
    cfg.teleport_tools.max_concurrent = 1
    cfg.teleport_tools.time_budget_seconds = 1
    logs.push(page([EV]), sleep=2.5)
    first, second = await asyncio.gather(
        execute_plugin_lambda_logs(cfg=cfg, function_name="f", since_minutes=5),
        execute_plugin_lambda_logs(cfg=cfg, function_name="g", since_minutes=5),
    )
    assert first["code"] == "LOGS_TIMEOUT" and second["code"] == "TSH_TIMEOUT"
    assert len(logs.calls) == 1


async def test_resolve_by_tenant_and_plugin(cfg, logs, monkeypatch):
    q = AsyncMock(return_value=[["langgenius/openai:1.2.1@abc", "serverless", "fn--2",
                                 "https://x.lambda-url.us-east-1.on.aws/"]])
    monkeypatch.setattr("db_gateway.lambda_logs.run_metadata_query", q)
    logs.push(page([EV]))
    out = await execute_plugin_lambda_logs(cfg=cfg, tenant_id="6f1d2c3e-0000-4000-8000-000000000001",
                                           plugin_id="langgenius/openai", since_minutes=10)
    assert out["function_name"] == "fn--2" and logs.calls[0]["logGroupName"] == "/aws/lambda/fn--2"
    assert out["plugin_unique_identifier"] == "langgenius/openai:1.2.1@abc"
    assert q.call_args.kwargs["params"] == ("6f1d2c3e-0000-4000-8000-000000000001", "langgenius/openai")
    assert any("安装表反映当前状态" in w for w in out["warnings"])


@pytest.mark.parametrize("rows,needle", [
    ([], "没有安装"),
    ([["p:1@c", "local", None, None]], "没有运行映射"),
    ([["p:1@c", "serverless", "fn", "https://example.com/"]], "不是 Lambda"),
])
async def test_resolve_failures(cfg, logs, monkeypatch, rows, needle):
    monkeypatch.setattr("db_gateway.lambda_logs.run_metadata_query", AsyncMock(return_value=rows))
    out = await execute_plugin_lambda_logs(cfg=cfg, tenant_id="6f1d2c3e-0000-4000-8000-000000000001",
                                           plugin_id="a/b", since_minutes=10)
    assert out["code"] == "UNKNOWN_FUNCTION" and needle in out["error"] and logs.calls == []


async def test_bad_tenant_id_is_rejected_without_query(cfg, logs, monkeypatch):
    q = AsyncMock(); monkeypatch.setattr("db_gateway.lambda_logs.run_metadata_query", q)
    out = await execute_plugin_lambda_logs(cfg=cfg, tenant_id="not-a-uuid", plugin_id="a/b", since_minutes=5)
    assert out["code"] == "INVALID_ARGUMENT" and q.await_count == 0


@pytest.mark.parametrize("kwargs", [
    {},                                                           # 没给定位
    {"function_name": "f", "tenant_id": "x", "plugin_id": "a/b"}, # 两种都给
    {"function_name": "../etc"},                                  # 非法函数名
    {"function_name": "f"},                                       # 没给窗口
    {"function_name": "f", "since_minutes": 5, "start_time": 1, "end_time": 2},
    {"function_name": "f", "start_time": 1795775520000, "end_time": 1795775460000},
    {"function_name": "f", "start_time": 1795775460, "end_time": 1795775520},   # 秒级
    {"function_name": "f", "since_minutes": 0},
    {"function_name": "f", "since_minutes": True},
    {"function_name": "f", "since_minutes": 5, "limit": 0},
    {"function_name": "f", "start_time": "昨天", "end_time": "今天"},
    {"function_name": "f", "since_minutes": 5, "next_token": "T1"},
])
async def test_invalid_arguments(cfg, logs, kwargs):
    out = await execute_plugin_lambda_logs(cfg=cfg, **kwargs)
    assert out["code"] == "INVALID_ARGUMENT" and logs.get_calls == 0


async def test_since_minutes_with_next_token_message_points_to_window(cfg, logs):
    out = await execute_plugin_lambda_logs(cfg=cfg, function_name="f", since_minutes=5, next_token="T1")
    assert "start_ms" in out["error"]


async def test_window_has_absolute_ms_and_continue_hint(cfg, logs):
    logs.push(page([EV], token="T1"))
    out = await execute_plugin_lambda_logs(cfg=cfg, function_name="f", since_minutes=10, limit=1)
    w = out["window"]
    assert w["end_ms"] - w["start_ms"] == 600_000 and out["next_token"] == "T1"
    assert logs.calls[0]["startTime"] == w["start_ms"] and logs.calls[0]["endTime"] == w["end_ms"] - 1
    assert any("start_ms" in x for x in out["warnings"])


async def test_limit_above_max_is_clamped_with_warning(cfg, logs):
    logs.push(page([EV]))
    out = await execute_plugin_lambda_logs(cfg=cfg, function_name="f", since_minutes=5, limit=99999)
    assert logs.calls[0]["limit"] == 10000 and out["warnings"]


async def test_audit_has_no_log_content(cfg, logs):
    logs.push(page([{**EV, "message": "SECRET-PAYLOAD"}]))
    await execute_plugin_lambda_logs(cfg=cfg, function_name="f", since_minutes=5)
    text = (Path(cfg.db_gateway.audit_log_dir) / "db_gateway_audit.log").read_text()
    assert '"tool": "plugin_lambda_logs"' in text and "SECRET-PAYLOAD" not in text
