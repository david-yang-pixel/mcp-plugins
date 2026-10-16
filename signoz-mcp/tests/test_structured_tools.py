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
