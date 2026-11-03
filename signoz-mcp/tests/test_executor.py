from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone

import pytest

from signoz_mcp.executor import execute_plan, merge_chunk_rows
from signoz_mcp.settings import Settings
from signoz_mcp.structured_tools import ChunkSpec, build_structured_plan


NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
THIRTY_DAYS = {"start": "2026-10-01T00:00:00Z", "end": "2026-10-31T00:00:00Z"}


def _settings(**overrides):
    values = {
        "SIGNOZ_MCP_API_KEY": "x" * 32,
        "SIGNOZ_MCP_TIMEOUT_SECONDS": 60,
        "SIGNOZ_MCP_CHUNK_CONCURRENCY": 3,
        **overrides,
    }
    return Settings(**values)


def _start_of(args):
    return args[args.index("--start") + 1]


async def _never(args, settings):
    raise AssertionError("unexpected runner call")


@pytest.mark.asyncio
async def test_thirty_day_count_is_split_into_daily_queries_and_summed():
    settings = _settings()
    plan = build_structured_plan(
        "signoz_aggregate_logs",
        {**THIRTY_DAYS, "deployment": "dify-api-oltp", "aggregation": "count"},
        settings=settings,
    )
    calls = []

    async def run_chunk(args, _settings):
        calls.append(args)
        return {"success": True, "data": [{"value": "10"}]}

    payload = await execute_plan(plan, settings, run_single=_never, run_chunk=run_chunk, now=NOW)

    assert payload["success"] is True
    assert payload["data"] == [{"value": 300}]
    assert payload["chunking"]["chunks"] == 30
    assert payload["chunking"]["completed"] == 30
    assert payload["window"]["start"] == "2026-10-01T00:00:00Z"
    assert len(calls) == 30
    assert all(args[0] == "query" and "--hours" not in args for args in calls)
    starts = sorted(_start_of(args) for args in calls)
    assert starts[0] == "2026-10-01T00:00:00Z" and starts[-1] == "2026-10-30T00:00:00Z"
    last = next(args for args in calls if _start_of(args) == starts[-1])
    assert "timestamp <= {end_ns}" in last[1]
    assert all("timestamp < {end_ns}" in args[1] for args in calls if args is not last)


@pytest.mark.asyncio
async def test_short_window_runs_original_single_query():
    settings = _settings()
    plan = build_structured_plan("signoz_aggregate_logs", {"timeRange": "2h"}, settings=settings)
    seen = []

    async def run_single(args, _settings):
        seen.append(args)
        return {"success": True, "data": [{"value": 5}]}

    payload = await execute_plan(plan, settings, run_single=run_single, run_chunk=_never, now=NOW)

    assert seen == [plan.args]
    assert seen[0][-2:] == ["--hours", "2"]
    assert payload["data"] == [{"value": 5}]
    assert payload["window"]["mode"] == "relative"
    assert "chunking" not in payload


@pytest.mark.asyncio
async def test_relative_long_window_is_chunked_from_now():
    settings = _settings()
    plan = build_structured_plan("signoz_list_services", {"timeRange": "3d"}, settings=settings)
    calls = []

    async def run_chunk(args, _settings):
        calls.append(args)
        return {
            "success": True,
            "data": [
                {"deployment": "api", "service": "api", "environment": "prod", "log_count": 2},
                {"deployment": "web", "service": "web", "environment": "prod", "log_count": 1},
            ],
        }

    payload = await execute_plan(plan, settings, run_single=_never, run_chunk=run_chunk, now=NOW)

    assert payload["success"] is True
    assert len(calls) == 4  # 12:00 -> midnight, 2 full days, midnight -> 12:00
    assert payload["data"][0] == {"deployment": "api", "service": "api", "environment": "prod", "log_count": 8}
    assert payload["window"]["end"] == "2026-11-02T12:00:00Z"


def test_merge_avg_time_series_and_latest():
    avg_spec = ChunkSpec(
        build_sql=lambda _: "",
        key_columns=("bucket",),
        merge_ops={"__sum": "sum", "__count": "sum"},
        finalize="avg",
        sort_column="bucket",
        sort_desc=False,
        limit=10,
        row_limit=100,
    )
    merged = merge_chunk_rows(
        [
            [{"bucket": "2026-10-01 23:55:00", "__sum": 10, "__count": 2}],
            [
                {"bucket": "2026-10-01 23:55:00", "__sum": "20", "__count": "2"},
                {"bucket": "2026-10-02 00:00:00", "__sum": 9, "__count": 3},
            ],
        ],
        avg_spec,
    )
    assert merged == [
        {"bucket": "2026-10-01 23:55:00", "value": 7.5},
        {"bucket": "2026-10-02 00:00:00", "value": 3.0},
    ]

    latest_spec = ChunkSpec(
        build_sql=lambda _: "",
        key_columns=("pod",),
        merge_ops={},
        finalize="latest",
        sort_column="value",
        sort_desc=True,
        limit=10,
        row_limit=100,
    )
    merged = merge_chunk_rows(
        [[{"pod": "a", "value": 1, "__ts": 100}], [{"pod": "a", "value": 2, "__ts": 200}]],
        latest_spec,
    )
    assert merged == [{"pod": "a", "value": 2}]


@pytest.mark.asyncio
async def test_grouped_merge_sorts_and_applies_user_limit_and_flags_row_cap():
    settings = _settings(SIGNOZ_MCP_CHUNK_ROW_LIMIT=2)
    plan = build_structured_plan(
        "signoz_aggregate_logs",
        {**THIRTY_DAYS, "groupBy": "resource.k8s.deployment.name", "limit": 1},
        settings=settings,
    )

    async def run_chunk(args, _settings):
        return {
            "success": True,
            "data": [{"k8s.deployment.name": "a", "value": 1}, {"k8s.deployment.name": "b", "value": 2}],
        }

    payload = await execute_plan(plan, settings, run_single=_never, run_chunk=run_chunk, now=NOW)

    assert payload["data"] == [{"k8s.deployment.name": "b", "value": 60}]
    assert payload["chunking"]["approximate"] is True


@pytest.mark.asyncio
async def test_chunk_timeout_returns_partial_result_and_smaller_windows():
    settings = _settings(SIGNOZ_MCP_CHUNK_CONCURRENCY=1)
    plan = build_structured_plan(
        "signoz_aggregate_logs", {**THIRTY_DAYS, "aggregation": "count"}, settings=settings
    )
    calls = []

    async def run_chunk(args, _settings):
        calls.append(args)
        if _start_of(args) == "2026-10-03T00:00:00Z":
            return {"success": False, "code": "SZ_TIMEOUT", "error": "sz timed out after 60s"}
        return {"success": True, "data": [{"value": 4}]}

    payload = await execute_plan(plan, settings, run_single=_never, run_chunk=run_chunk, now=NOW)

    assert payload["success"] is False
    assert payload["code"] == "SZ_TIMEOUT"
    assert "2026-10-03T00:00:00Z" in payload["error"]
    assert len(calls) == 3  # stops scheduling after the first failure
    assert payload["partial"]["data"] == [{"value": 8}]
    assert payload["partial"]["completedWindows"] == [
        {"start": "2026-10-01T00:00:00Z", "end": "2026-10-03T00:00:00Z"}
    ]
    assert payload["partial"]["remainingWindows"] == [
        {"start": "2026-10-03T00:00:00Z", "end": "2026-10-31T00:00:00Z"}
    ]
    suggestion = payload["suggestion"]
    assert suggestion["maxHours"] == 12
    assert suggestion["windows"][0] == {"start": "2026-10-03T00:00:00Z", "end": "2026-10-03T12:00:00Z"}
    assert "12h" in suggestion["message"]


@pytest.mark.asyncio
async def test_total_budget_timeout_cancels_remaining_chunks():
    settings = _settings(SIGNOZ_MCP_TOTAL_TIMEOUT_SECONDS=1, SIGNOZ_MCP_CHUNK_CONCURRENCY=2)
    plan = build_structured_plan(
        "signoz_aggregate_logs", {**THIRTY_DAYS, "aggregation": "count"}, settings=settings
    )
    cancelled = 0

    async def run_chunk(args, _settings):
        nonlocal cancelled
        if _start_of(args) in {"2026-10-01T00:00:00Z", "2026-10-02T00:00:00Z"}:
            return {"success": True, "data": [{"value": 1}]}
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled += 1
            raise
        return {"success": True, "data": [{"value": 1}]}

    payload = await execute_plan(plan, settings, run_single=_never, run_chunk=run_chunk, now=NOW)

    assert payload["success"] is False
    assert payload["code"] == "SZ_TOTAL_TIMEOUT"
    assert payload["partial"]["data"] == [{"value": 2}]
    assert payload["chunking"]["completed"] == 2
    assert cancelled == 2
    assert payload["suggestion"]["windows"][0]["start"] == "2026-10-03T00:00:00Z"
    assert re.search(r"at most \d+h", payload["suggestion"]["message"])


@pytest.mark.asyncio
async def test_non_timeout_chunk_failure_has_no_suggestion():
    settings = _settings()
    plan = build_structured_plan("signoz_aggregate_logs", THIRTY_DAYS, settings=settings)

    async def run_chunk(args, _settings):
        return {"success": False, "code": "SZ_NONZERO", "error": "SigNoz query error: bad"}

    payload = await execute_plan(plan, settings, run_single=_never, run_chunk=run_chunk, now=NOW)

    assert payload["code"] == "SZ_NONZERO"
    assert "bad" in payload["error"]
    assert "suggestion" not in payload


@pytest.mark.asyncio
async def test_single_query_timeout_includes_window_and_suggestion():
    settings = _settings()
    plan = build_structured_plan(
        "signoz_search_logs",
        {"start": "2026-10-01T00:00:00Z", "end": "2026-10-08T00:00:00Z"},
        settings=settings,
    )

    async def run_single(args, _settings):
        return {"success": False, "code": "SZ_TIMEOUT", "error": "sz timed out after 60s"}

    payload = await execute_plan(plan, settings, run_single=run_single, run_chunk=_never, now=NOW)

    assert payload["code"] == "SZ_TIMEOUT"
    assert payload["window"]["hours"] == 168
    assert payload["suggestion"]["maxHours"] == 72
    assert payload["suggestion"]["windows"][0] == {
        "start": "2026-10-01T00:00:00Z",
        "end": "2026-10-04T00:00:00Z",
    }


@pytest.mark.asyncio
async def test_percentile_over_long_window_runs_single_query_and_says_why():
    settings = _settings()
    plan = build_structured_plan(
        "signoz_aggregate_traces",
        {**THIRTY_DAYS, "aggregation": "p95", "aggregateOn": "duration_nano"},
        settings=settings,
    )

    async def run_single(args, _settings):
        return {"success": True, "data": [{"value": 1}]}

    payload = await execute_plan(plan, settings, run_single=run_single, run_chunk=_never, now=NOW)

    assert payload["chunking"]["applied"] is False
    assert "p95" in payload["chunking"]["reason"]


@pytest.mark.asyncio
async def test_search_logs_payload_is_compacted():
    settings = _settings()
    plan = build_structured_plan("signoz_search_logs", {"limit": 1}, settings=settings)
    traceback = "Traceback (most recent call last):\n" + "x" * 500 + "\nValueError: boom"

    async def run_single(args, _settings):
        return {
            "success": True,
            "data": [
                {
                    "body": traceback,
                    "attributes_string": {"exception.stacktrace": traceback},
                    "resources_string": {"host.ip": "10.0.0.1", "host.mac": "aa", "service.name": "api"},
                }
            ],
        }

    payload = await execute_plan(plan, settings, run_single=run_single, run_chunk=_never, now=NOW)

    row = payload["data"][0]
    assert row["resources_string"] == {"service.name": "api"}
    assert row["attributes_string"]["exception.stacktrace"] == "[duplicate of body]"
    assert payload["compaction"]["deduplicated"] == 1


@pytest.mark.asyncio
async def test_chunk_runner_exception_is_reported_not_treated_as_budget_timeout():
    settings = _settings(SIGNOZ_MCP_CHUNK_CONCURRENCY=1)
    plan = build_structured_plan("signoz_aggregate_logs", THIRTY_DAYS, settings=settings)

    async def run_chunk(args, _settings):
        raise RuntimeError("spawn exploded")

    payload = await execute_plan(plan, settings, run_single=_never, run_chunk=run_chunk, now=NOW)

    assert payload["code"] == "SZ_FAILED"
    assert "spawn exploded" in payload["error"]


def test_deployed_budgets_return_before_a_60s_proxy_timeout():
    """The legacy route's proxy may cut responses at 60s; partial results must beat it."""
    import json
    from pathlib import Path

    env = json.loads((Path(__file__).resolve().parents[1] / "deploy" / "runtime.json").read_text())[
        "environment"
    ]
    settings = Settings(SIGNOZ_MCP_API_KEY="x" * 32, **env)
    kill_grace = 5  # sz_runner._kill_process waits up to 5s after cancelling sz
    assert settings.total_timeout_seconds + kill_grace < 60
    assert settings.timeout_seconds + kill_grace < 60
    assert settings.timeout_seconds <= settings.total_timeout_seconds
