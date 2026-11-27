"""plugin_lambda_logs: CloudWatch logs of SaaS plugin Lambdas through tsh aws."""
import asyncio
import json
import shutil
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from db_gateway.lambda_logs import execute_plugin_lambda_logs, parse_time_ms

EV = {"timestamp": 1795775460000, "logStreamName": "s", "eventId": "1", "message": "m",
      "ingestionTime": 1795775460100}


def page(events, token=None):
    return json.dumps({"events": events, "searchedLogStreams": [],
                       **({"nextToken": token} if token else {})})


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


async def test_single_page_by_function_name(cfg, fake_tsh):
    fake_tsh.push(stdout=page([EV]))
    out = await execute_plugin_lambda_logs(cfg=cfg, function_name="9a442b2d0d9b43e56a190df6ca7dc207",
        start_time="2026-11-27 18:31:00", end_time="2026-11-27 18:32:00", filter_pattern='"ERROR"')
    assert out["success"] and out["complete"] is True and out["next_token"] is None
    assert out["log_group"] == "/aws/lambda/9a442b2d0d9b43e56a190df6ca7dc207"
    assert out["events"] == [{"timestamp": "2026-11-27T10:31:00.000Z", "log_stream": "s",
                              "event_id": "1", "message": "m"}]
    assert out["window"]["start_local"].startswith("2026-11-27T18:31:00")
    argv = fake_tsh.calls[0]["argv"]
    assert argv[:5] == ["aws", "--app", "aws-support-logs", "logs", "filter-log-events"]
    assert argv[argv.index("--start-time") + 1] == "1795775460000"
    assert argv[argv.index("--end-time") + 1] == "1795775519999"
    assert '--filter-pattern="ERROR"' in argv
    assert argv[argv.index("--limit") + 1] == "1000"
    assert "--no-paginate" in argv and "--no-cli-pager" not in argv


async def test_dash_filter_and_token_use_equals_form(cfg, fake_tsh):
    fake_tsh.push(stdout=page([EV]))
    await execute_plugin_lambda_logs(cfg=cfg, function_name="f", start_time=1795775460000,
                                     end_time=1795775520000, filter_pattern="-DEBUG", next_token="T0")
    argv = fake_tsh.calls[0]["argv"]
    assert "--filter-pattern=-DEBUG" in argv and "--next-token=T0" in argv


async def test_long_message_is_not_truncated(cfg, fake_tsh):
    fake_tsh.push(stdout=page([{**EV, "message": "x" * 300_000}]))
    out = await execute_plugin_lambda_logs(cfg=cfg, function_name="f" * 32, since_minutes=10)
    assert len(out["events"][0]["message"]) == 300_000


async def test_limit_stops_with_token(cfg, fake_tsh):
    fake_tsh.push(stdout=page([EV, EV], token="T1"))
    out = await execute_plugin_lambda_logs(cfg=cfg, function_name="f" * 32, since_minutes=10, limit=2)
    assert (out["event_count"], out["next_token"], out["complete"]) == (2, "T1", False)
    assert len(fake_tsh.calls) == 1


async def test_empty_pages_with_token_keep_paging(cfg, fake_tsh):
    fake_tsh.push(stdout=page([], token="T1")); fake_tsh.push(stdout=page([], token="T2"))
    fake_tsh.push(stdout=page([EV]))
    out = await execute_plugin_lambda_logs(cfg=cfg, function_name="f" * 32, since_minutes=10)
    assert (out["event_count"], out["complete"]) == (1, True)
    assert "--next-token=T1" in fake_tsh.calls[1]["argv"]


async def test_time_budget_returns_partial_with_token(cfg, fake_tsh):
    cfg.teleport_tools.time_budget_seconds = 2
    fake_tsh.push(stdout=page([EV], token="T1"), sleep=1.5); fake_tsh.push(stdout=page([EV]))
    out = await execute_plugin_lambda_logs(cfg=cfg, function_name="f" * 32, since_minutes=10)
    assert out["success"] and (out["event_count"], out["next_token"], out["complete"]) == (1, "T1", False)


async def test_first_page_timeout_is_logs_timeout(cfg, fake_tsh):
    cfg.teleport_tools.time_budget_seconds = 1
    fake_tsh.push(sleep=30)
    t0 = time.monotonic()
    out = await execute_plugin_lambda_logs(cfg=cfg, function_name="f" * 32, since_minutes=10)
    assert (out["success"], out["code"]) == (False, "LOGS_TIMEOUT") and time.monotonic() - t0 < 4


async def test_aws_error_is_reported_without_login(cfg, fake_tsh):
    fake_tsh.push(code=1, stderr="An error occurred (ResourceNotFoundException) when calling "
                  "the FilterLogEvents operation: The specified log group does not exist.\n"
                  "ERROR: exit status 255\n")
    out = await execute_plugin_lambda_logs(cfg=cfg, function_name="f" * 32, since_minutes=10)
    assert out["code"] == "AWS_ERROR" and "ResourceNotFoundException" in out["error"]
    assert len(fake_tsh.calls) == 1


async def test_aws_usage_error_does_not_login(cfg, fake_tsh):
    fake_tsh.push(code=1, stderr="aws: error: argument --filter-pattern: expected one argument\n")
    out = await execute_plugin_lambda_logs(cfg=cfg, function_name="f" * 32, since_minutes=10)
    assert out["code"] == "AWS_ERROR" and len(fake_tsh.calls) == 1


async def test_missing_app_cert_logs_in_and_retries(cfg, fake_tsh):
    fake_tsh.push(code=1, stderr="ERROR: some tsh error"); fake_tsh.push(); fake_tsh.push(stdout=page([EV]))
    out = await execute_plugin_lambda_logs(cfg=cfg, function_name="f" * 32, since_minutes=10)
    assert out["success"]
    assert fake_tsh.calls[1]["argv"] == ["apps", "login", "--aws-role", "SupportLambdaLogsReadOnly",
                                         "aws-support-logs"]


async def test_no_teleport_session_is_expired_without_running_tsh(cfg, fake_tsh):
    shutil.rmtree(Path(cfg.teleport.auth_home) / "keys")
    out = await execute_plugin_lambda_logs(cfg=cfg, function_name="f", since_minutes=5)
    assert out["code"] == "TELEPORT_EXPIRED" and fake_tsh.calls == []


async def test_resolve_by_tenant_and_plugin(cfg, fake_tsh, monkeypatch):
    q = AsyncMock(return_value=[["langgenius/openai:1.2.1@abc", "serverless", "fn--2",
                                 "https://x.lambda-url.us-east-1.on.aws/"]])
    monkeypatch.setattr("db_gateway.lambda_logs.run_metadata_query", q)
    fake_tsh.push(stdout=page([EV]))
    out = await execute_plugin_lambda_logs(cfg=cfg, tenant_id="6f1d2c3e-0000-4000-8000-000000000001",
                                           plugin_id="langgenius/openai", since_minutes=10)
    assert out["function_name"] == "fn--2"
    assert out["plugin_unique_identifier"] == "langgenius/openai:1.2.1@abc"
    assert q.call_args.kwargs["params"] == ("6f1d2c3e-0000-4000-8000-000000000001", "langgenius/openai")
    assert any("安装表反映当前状态" in w for w in out["warnings"])


@pytest.mark.parametrize("rows,needle", [
    ([], "没有安装"),
    ([["p:1@c", "local", None, None]], "没有运行映射"),
    ([["p:1@c", "serverless", "fn", "https://example.com/"]], "不是 Lambda"),
])
async def test_resolve_failures(cfg, fake_tsh, monkeypatch, rows, needle):
    monkeypatch.setattr("db_gateway.lambda_logs.run_metadata_query", AsyncMock(return_value=rows))
    out = await execute_plugin_lambda_logs(cfg=cfg, tenant_id="6f1d2c3e-0000-4000-8000-000000000001",
                                           plugin_id="a/b", since_minutes=10)
    assert out["code"] == "UNKNOWN_FUNCTION" and needle in out["error"] and fake_tsh.calls == []


async def test_bad_tenant_id_is_rejected_without_query(cfg, fake_tsh, monkeypatch):
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
])
async def test_invalid_arguments(cfg, fake_tsh, kwargs):
    out = await execute_plugin_lambda_logs(cfg=cfg, **kwargs)
    assert out["code"] == "INVALID_ARGUMENT" and fake_tsh.calls == []


async def test_limit_above_max_is_clamped_with_warning(cfg, fake_tsh):
    fake_tsh.push(stdout=page([EV]))
    out = await execute_plugin_lambda_logs(cfg=cfg, function_name="f", since_minutes=5, limit=99999)
    assert fake_tsh.calls[0]["argv"][fake_tsh.calls[0]["argv"].index("--limit") + 1] == "10000"
    assert out["warnings"]


async def test_audit_has_no_log_content(cfg, fake_tsh, tmp_path):
    fake_tsh.push(stdout=page([{**EV, "message": "SECRET-PAYLOAD"}]))
    await execute_plugin_lambda_logs(cfg=cfg, function_name="f", since_minutes=5)
    text = (Path(cfg.db_gateway.audit_log_dir) / "db_gateway_audit.log").read_text()
    assert '"tool": "plugin_lambda_logs"' in text and "SECRET-PAYLOAD" not in text


# --- final review fixes -----------------------------------------------------------


async def test_queued_call_past_deadline_is_tsh_timeout(cfg, fake_tsh):
    cfg.teleport_tools.max_concurrent = 1
    cfg.teleport_tools.time_budget_seconds = 1
    fake_tsh.push(sleep=30)
    first, second = await asyncio.gather(
        execute_plugin_lambda_logs(cfg=cfg, function_name="f", since_minutes=5),
        execute_plugin_lambda_logs(cfg=cfg, function_name="g", since_minutes=5),
    )
    assert first["code"] == "LOGS_TIMEOUT" and second["code"] == "TSH_TIMEOUT"
    assert len(fake_tsh.calls) == 1


async def test_login_timeout_is_tsh_timeout(cfg, fake_tsh):
    cfg.teleport_tools.time_budget_seconds = 2
    fake_tsh.push(code=1, stderr="ERROR: some tsh error"); fake_tsh.push(sleep=30)
    out = await execute_plugin_lambda_logs(cfg=cfg, function_name="f", since_minutes=5)
    assert out["code"] == "TSH_TIMEOUT"


async def test_since_minutes_with_next_token_is_rejected(cfg, fake_tsh):
    out = await execute_plugin_lambda_logs(cfg=cfg, function_name="f", since_minutes=5, next_token="T1")
    assert out["code"] == "INVALID_ARGUMENT" and "start_ms" in out["error"] and fake_tsh.calls == []


async def test_window_has_absolute_ms_and_continue_hint(cfg, fake_tsh):
    fake_tsh.push(stdout=page([EV], token="T1"))
    out = await execute_plugin_lambda_logs(cfg=cfg, function_name="f", since_minutes=10, limit=1)
    w = out["window"]
    assert w["end_ms"] - w["start_ms"] == 600_000 and out["next_token"] == "T1"
    argv = fake_tsh.calls[0]["argv"]
    assert argv[argv.index("--start-time") + 1] == str(w["start_ms"])
    assert any("start_ms" in x for x in out["warnings"])


async def test_json_after_login_banner_is_parsed(cfg, fake_tsh):
    fake_tsh.push(stdout='Logged into AWS app "aws-support-logs".\n\n' + page([EV]))
    out = await execute_plugin_lambda_logs(cfg=cfg, function_name="f", since_minutes=5)
    assert out["success"] and out["event_count"] == 1
