from __future__ import annotations

import pytest

from signoz_mcp.structured_tools import STRUCTURED_TOOLS, build_structured_sz_args


def test_structured_tool_surface_contains_high_value_read_only_diagnostics():
    names = {tool["name"] for tool in STRUCTURED_TOOLS}

    assert {
        "signoz_get_field_keys",
        "signoz_get_field_values",
        "signoz_search_logs",
        "signoz_aggregate_logs",
        "signoz_search_traces",
        "signoz_aggregate_traces",
        "signoz_query_metrics",
    } <= names
    assert not any(
        name.startswith(("signoz_create_", "signoz_update_", "signoz_delete_"))
        for name in names
    )


def test_search_logs_builds_scoped_paginated_query():
    args = build_structured_sz_args(
        "signoz_search_logs",
        {
            "timeRange": "2h",
            "environment": "production",
            "deployment": "dify-api-oltp",
            "tenant": "tenant-123",
            "severity": "ERROR",
            "searchText": "sandbox",
            "limit": 20,
            "offset": 40,
        },
    )

    assert args[0] == "query"
    sql = args[1]
    assert "signoz_logs.distributed_logs_v2" in sql
    assert "resources_string['deployment.environment'] = 'production'" in sql
    assert "resources_string['k8s.deployment.name'] = 'dify-api-oltp'" in sql
    assert "attributes_string['tenant_id'] = 'tenant-123'" in sql
    assert "severity_text = 'ERROR'" in sql
    assert "body LIKE '%sandbox%'" in sql
    assert "LIMIT 20 OFFSET 40" in sql
    assert "AS timestamp," not in sql
    assert "AS timestamp_utc" in sql
    assert args[-2:] == ["--hours", "2"]


def test_aggregate_traces_builds_tenant_time_series_p95_query():
    args = build_structured_sz_args(
        "signoz_aggregate_traces",
        {
            "aggregation": "p95",
            "aggregateOn": "duration_nano",
            "operation": "/dify.saas.api.billing.BillingQuota/GetBalance",
            "groupBy": "attribute.dify.tenant_id",
            "requestType": "time_series",
            "stepInterval": 600,
            "timeRange": "4h",
            "limit": 100,
        },
    )

    sql = args[1]
    assert "quantile(0.95)(duration_nano) AS value" in sql
    assert "attributes_string['dify.tenant_id'] AS `dify.tenant_id`" in sql
    assert "toStartOfInterval" in sql
    assert "INTERVAL 600 SECOND" in sql
    assert "name = '/dify.saas.api.billing.BillingQuota/GetBalance'" in sql
    assert args[-2:] == ["--hours", "4"]


def test_field_discovery_queries_are_bounded_and_context_aware():
    keys_args = build_structured_sz_args(
        "signoz_get_field_keys",
        {"signal": "traces", "searchText": "tenant", "timeRange": "1h", "limit": 50},
    )
    values_args = build_structured_sz_args(
        "signoz_get_field_values",
        {
            "signal": "traces",
            "name": "dify.tenant_id",
            "fieldContext": "attribute",
            "timeRange": "1h",
            "limit": 20,
        },
    )

    assert "LIMIT 10000" in keys_args[1]
    assert "mapKeys(attributes_string)" in keys_args[1]
    assert "lower(name) LIKE '%tenant%'" in keys_args[1]
    assert "attributes_string['dify.tenant_id']" in values_args[1]
    assert "GROUP BY value" in values_args[1]


def test_aggregate_logs_counts_by_deployment():
    args = build_structured_sz_args(
        "signoz_aggregate_logs",
        {
            "aggregation": "count",
            "groupBy": "resource.k8s.deployment.name",
            "severity": "ERROR",
            "timeRange": "30m",
            "limit": 25,
        },
    )

    sql = args[1]
    assert "count() AS value" in sql
    assert "resources_string['k8s.deployment.name'] AS `k8s.deployment.name`" in sql
    assert "severity_text = 'ERROR'" in sql
    assert args[-2:] == ["--hours", "0.5"]


def test_search_traces_supports_service_operation_error_and_duration_filters():
    args = build_structured_sz_args(
        "signoz_search_traces",
        {
            "service": "billing",
            "operation": "GetBalance",
            "error": True,
            "minDuration": "500000000",
            "timeRange": "1h",
        },
    )

    sql = args[1]
    assert "serviceName = 'billing'" in sql
    assert "name = 'GetBalance'" in sql
    assert "has_error = true" in sql
    assert "duration_nano >= 500000000" in sql
    assert "AS timestamp," not in sql
    assert "AS timestamp_utc" in sql


def test_structured_group_by_rejects_sql_injection():
    with pytest.raises(ValueError, match="field"):
        build_structured_sz_args(
            "signoz_aggregate_traces",
            {"aggregation": "count", "groupBy": "name); DROP TABLE x; --"},
        )


def test_query_metrics_joins_samples_and_metadata_with_resource_filters():
    args = build_structured_sz_args(
        "signoz_query_metrics",
        {
            "metricName": "k8s.pod.cpu.utilization",
            "aggregation": "avg",
            "groupBy": "resource.k8s.pod.name",
            "environment": "production",
            "deployment": "sandbox",
            "pod": "sandbox-abc123",
            "timeRange": "30m",
            "limit": 30,
        },
    )

    sql = args[1]
    assert "signoz_metrics.distributed_samples_v4 AS s" in sql
    assert "signoz_metrics.distributed_time_series_v4 AS t" in sql
    assert "s.fingerprint = t.fingerprint" in sql
    assert "avg(s.value) AS value" in sql
    assert "t.resource_attrs['k8s.pod.name'] AS `k8s.pod.name`" in sql
    assert "t.resource_attrs['deployment.environment'] = 'production'" in sql
    assert "t.resource_attrs['k8s.deployment.name'] = 'sandbox'" in sql
    assert "t.resource_attrs['k8s.pod.name'] = 'sandbox-abc123'" in sql
    assert args[-2:] == ["--hours", "0.5"]


def test_query_metrics_rejects_unsafe_group_field():
    with pytest.raises(ValueError, match="field"):
        build_structured_sz_args(
            "signoz_query_metrics",
            {"metricName": "cpu", "groupBy": "resource.x']; DROP TABLE y; --"},
        )


def test_search_pagination_is_bounded():
    args = build_structured_sz_args(
        "signoz_search_logs",
        {"timeRange": "1h", "limit": 999999, "offset": 999999999},
    )

    assert "LIMIT 1000 OFFSET 100000" in args[1]


# ---- absolute time, long windows, log fields ---------------------------------

from datetime import datetime, timezone  # noqa: E402

from signoz_mcp.settings import Settings  # noqa: E402
from signoz_mcp.structured_tools import build_structured_plan  # noqa: E402


NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
WINDOWED_TOOLS = {
    "signoz_list_services": {},
    "signoz_get_trace_details": {"traceId": "a" * 32},
    "signoz_list_metrics": {},
    "signoz_get_field_keys": {"signal": "logs"},
    "signoz_get_field_values": {"signal": "logs", "name": "severity_text", "fieldContext": "intrinsic"},
    "signoz_search_logs": {},
    "signoz_aggregate_logs": {},
    "signoz_search_traces": {},
    "signoz_aggregate_traces": {},
    "signoz_query_metrics": {"metricName": "cpu"},
}


def _settings(**overrides):
    values = {"SIGNOZ_MCP_API_KEY": "x" * 32, **overrides}
    return Settings(**values)


def test_every_windowed_tool_schema_has_start_and_end():
    tools = {tool["name"]: tool for tool in STRUCTURED_TOOLS}

    for name in WINDOWED_TOOLS:
        properties = tools[name]["inputSchema"]["properties"]
        assert "timeRange" in properties
        assert "ISO 8601" in properties["start"]["description"]
        assert "ISO 8601" in properties["end"]["description"]


@pytest.mark.parametrize("tool_name", sorted(WINDOWED_TOOLS))
def test_every_windowed_tool_passes_absolute_window_to_sz(tool_name):
    arguments = {
        **WINDOWED_TOOLS[tool_name],
        "start": "2026-10-01T00:00:00",
        "end": "2026-10-01T06:00:00Z",
        "timeRange": "1h",
    }

    plan = build_structured_plan(tool_name, arguments, settings=_settings(), now=NOW)

    index = plan.args.index("--start")
    assert plan.args[index:index + 4] == [
        "--start", "2026-10-01T00:00:00Z", "--end", "2026-10-01T06:00:00Z",
    ]
    assert "--hours" not in plan.args
    assert plan.window.echo()["timeRangeIgnored"] is True


def test_services_absolute_window_keeps_limit_and_env():
    args = build_structured_sz_args(
        "signoz_list_services",
        {"start": "2026-10-01T00:00:00Z", "end": "2026-10-01T02:00:00Z", "environment": "production"},
        settings=_settings(),
    )

    assert args == [
        "services", "--start", "2026-10-01T00:00:00Z", "--end", "2026-10-01T02:00:00Z",
        "--limit", "30", "--env", "production",
    ]


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"start": "2026-10-02T00:00:00Z", "end": "2026-10-01T00:00:00Z"}, "earlier"),
        ({"start": "2026-08-01T00:00:00Z", "end": "2026-10-01T00:00:00Z"}, "maximum"),
        ({"start": "last tuesday"}, "ISO 8601"),
    ],
)
def test_absolute_window_is_validated(arguments, message):
    with pytest.raises(ValueError, match=message):
        build_structured_sz_args("signoz_aggregate_logs", arguments, settings=_settings())


def test_max_window_is_configurable():
    arguments = {"start": "2026-08-01T00:00:00Z", "end": "2026-10-01T00:00:00Z"}

    plan = build_structured_plan(
        "signoz_aggregate_logs", arguments, settings=_settings(SIGNOZ_MCP_MAX_WINDOW_DAYS=90)
    )

    assert plan.window.hours == 61 * 24


def test_logs_time_filter_uses_ts_bucket_pruning_by_default_and_can_be_disabled():
    pruned = build_structured_sz_args("signoz_search_logs", {}, settings=_settings())[1]
    plain = build_structured_sz_args(
        "signoz_search_logs", {}, settings=_settings(SIGNOZ_MCP_TS_BUCKET_PRUNING="false")
    )[1]

    assert "ts_bucket_start >= intDiv({start_ns}, 1000000000) - 1800" in pruned
    assert "ts_bucket_start" not in plain
    assert "timestamp >= {start_ns} AND timestamp <= {end_ns}" in plain


def test_aggregate_chunk_sql_uses_exclusive_end_and_large_limit():
    plan = build_structured_plan(
        "signoz_aggregate_logs",
        {"aggregation": "count", "groupBy": "resource.k8s.deployment.name", "limit": 5},
        settings=_settings(SIGNOZ_MCP_CHUNK_ROW_LIMIT=777),
    )

    inner = plan.chunk.build_sql(True)
    last = plan.chunk.build_sql(False)
    assert "timestamp < {end_ns}" in inner
    assert "timestamp <= {end_ns}" in last
    assert inner.endswith("LIMIT 777")
    assert "LIMIT 5" in plan.args[1]
    assert plan.chunk.key_columns == ("k8s.deployment.name",)
    assert plan.chunk.merge_ops == {"value": "sum"}


def test_avg_chunk_sql_splits_sum_and_count():
    plan = build_structured_plan(
        "signoz_aggregate_traces",
        {"aggregation": "avg", "aggregateOn": "duration_nano", "requestType": "time_series"},
        settings=_settings(),
    )

    sql = plan.chunk.build_sql(True)
    assert "sum(duration_nano) AS __sum" in sql
    assert "count() AS __count" in sql
    assert "timestamp < fromUnixTimestamp64Nano({end_ns})" in sql
    assert plan.chunk.finalize == "avg"
    assert plan.chunk.key_columns == ("bucket",)
    assert "avg(duration_nano) AS value" in plan.args[1]


@pytest.mark.parametrize("aggregation", ["p95", "count_distinct"])
def test_non_mergeable_aggregations_are_not_chunked(aggregation):
    plan = build_structured_plan(
        "signoz_aggregate_logs",
        {"aggregation": aggregation, "aggregateOn": "attribute.latency"},
        settings=_settings(),
    )

    assert plan.chunk is None
    assert aggregation in plan.chunk_unavailable_reason


def test_metric_latest_chunk_sql_tracks_sample_time():
    plan = build_structured_plan(
        "signoz_query_metrics",
        {"metricName": "cpu", "aggregation": "latest", "groupBy": "resource.k8s.pod.name"},
        settings=_settings(),
    )

    sql = plan.chunk.build_sql(True)
    assert "max(s.unix_milli) AS __ts" in sql
    assert "s.unix_milli < intDiv({end_ns}, 1000000)" in sql
    assert plan.chunk.finalize == "latest"
    assert plan.chunk.key_columns == ("k8s.pod.name",)


def test_search_logs_fields_select_only_requested_columns():
    args = build_structured_sz_args(
        "signoz_search_logs",
        {
            "fields": ["timestamp_utc", "body", "attribute.exception.type", "resource.k8s.pod.name"],
            "limit": 1,
        },
        settings=_settings(),
    )

    select = args[1].split(" FROM ")[0]
    assert "AS timestamp_utc" in select
    assert "body" in select
    assert "attributes_string['exception.type'] AS `attribute.exception.type`" in select
    assert "resources_string['k8s.pod.name'] AS `resource.k8s.pod.name`" in select
    assert "resources_string," not in select
    assert "trace_id" not in select


def test_search_logs_fields_accepts_comma_string_and_rejects_unknown():
    args = build_structured_sz_args("signoz_search_logs", {"fields": "id, body"}, settings=_settings())
    assert args[1].startswith("SELECT id, body FROM")

    with pytest.raises(ValueError, match="unsupported log field"):
        build_structured_sz_args("signoz_search_logs", {"fields": ["passwords"]}, settings=_settings())
    with pytest.raises(ValueError, match="field"):
        build_structured_sz_args(
            "signoz_search_logs", {"fields": ["attribute.x`) FROM y --"]}, settings=_settings()
        )


def test_search_logs_compact_defaults_on_and_can_be_disabled():
    compact = build_structured_plan("signoz_search_logs", {}, settings=_settings())
    raw = build_structured_plan("signoz_search_logs", {"compact": False}, settings=_settings())

    assert compact.postprocess is not None
    assert raw.postprocess is None
    with pytest.raises(ValueError, match="compact"):
        build_structured_plan("signoz_search_logs", {"compact": "maybe"}, settings=_settings())
