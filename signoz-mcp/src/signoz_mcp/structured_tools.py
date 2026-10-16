from __future__ import annotations

import re
from typing import Any


_TRACE_ID_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_TIME_RANGE_RE = re.compile(r"^(\d+(?:\.\d+)?)([mhd])$")
_FIELD_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


STRUCTURED_TOOLS: list[dict[str, Any]] = [
    {
        "name": "signoz_list_services",
        "description": (
            "List active SigNoz services and Kubernetes deployments. "
            "Use this before guessing a service or deployment name."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "timeRange": {
                    "type": "string",
                    "description": "Relative window such as 30m, 2h, or 1d. Default: 1h.",
                },
                "environment": {
                    "type": "string",
                    "description": "Optional deployment.environment value.",
                },
                "limit": {
                    "type": ["integer", "string"],
                    "description": "Maximum rows, from 1 to 1000. Default: 30.",
                },
            },
        },
    },
    {
        "name": "signoz_get_trace_details",
        "description": (
            "Get all spans for one trace ID in timestamp order, including parent/child IDs, "
            "service, operation, duration, status, HTTP fields, and attributes."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "traceId": {
                    "type": "string",
                    "description": "Exact 32-character hexadecimal trace ID.",
                },
                "timeRange": {
                    "type": "string",
                    "description": "Relative search window such as 6h or 1d. Default: 6h.",
                },
                "limit": {
                    "type": ["integer", "string"],
                    "description": "Maximum spans, from 1 to 1000. Default: 500.",
                },
            },
            "required": ["traceId"],
        },
    },
    {
        "name": "signoz_list_metrics",
        "description": (
            "Discover available SigNoz metric names and metadata before querying capacity "
            "or Kubernetes resource usage."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "searchText": {
                    "type": "string",
                    "description": "Optional case-insensitive metric-name substring.",
                },
                "timeRange": {
                    "type": "string",
                    "description": "Relative window such as 30m or 1h. Default: 1h.",
                },
                "limit": {
                    "type": ["integer", "string"],
                    "description": "Maximum metrics, from 1 to 1000. Default: 50.",
                },
            },
        },
    },
    {
        "name": "signoz_get_field_keys",
        "description": (
            "Discover intrinsic, attribute, and resource field names for logs, traces, "
            "or metrics from a bounded recent sample."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "signal": {"type": "string", "enum": ["logs", "traces", "metrics"]},
                "searchText": {"type": "string"},
                "timeRange": {"type": "string", "description": "Default: 1h."},
                "limit": {"type": ["integer", "string"], "description": "Default: 100."},
            },
            "required": ["signal"],
        },
    },
    {
        "name": "signoz_get_field_values",
        "description": "List the most frequent values for one safe log, trace, or metric field.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "signal": {"type": "string", "enum": ["logs", "traces", "metrics"]},
                "name": {"type": "string"},
                "fieldContext": {
                    "type": "string",
                    "enum": ["attribute", "resource", "intrinsic"],
                },
                "searchText": {"type": "string"},
                "timeRange": {"type": "string", "description": "Default: 1h."},
                "limit": {"type": ["integer", "string"], "description": "Default: 50."},
            },
            "required": ["signal", "name", "fieldContext"],
        },
    },
    {
        "name": "signoz_search_logs",
        "description": (
            "Search recent logs with scoped environment, deployment, tenant, severity, "
            "trace, and body filters."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "timeRange": {"type": "string", "description": "Default: 1h."},
                "environment": {"type": "string"},
                "deployment": {"type": "string"},
                "tenant": {"type": "string"},
                "severity": {"type": "string"},
                "traceId": {"type": "string"},
                "searchText": {"type": "string"},
                "limit": {"type": ["integer", "string"], "description": "Default: 100."},
                "offset": {"type": ["integer", "string"], "description": "Default: 0."},
            },
        },
    },
    {
        "name": "signoz_aggregate_logs",
        "description": "Aggregate recent logs safely, optionally grouped or bucketed over time.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "aggregation": {
                    "type": "string",
                    "enum": ["count", "count_distinct", "avg", "min", "max", "sum", "p50", "p75", "p90", "p95", "p99"],
                },
                "aggregateOn": {"type": "string"},
                "groupBy": {"type": "string", "description": "Comma-separated safe fields, up to 3."},
                "requestType": {"type": "string", "enum": ["scalar", "time_series"]},
                "stepInterval": {"type": ["integer", "string"], "description": "Seconds."},
                "timeRange": {"type": "string", "description": "Default: 1h."},
                "environment": {"type": "string"},
                "deployment": {"type": "string"},
                "tenant": {"type": "string"},
                "severity": {"type": "string"},
                "searchText": {"type": "string"},
                "limit": {"type": ["integer", "string"], "description": "Default: 100."},
            },
        },
    },
    {
        "name": "signoz_search_traces",
        "description": (
            "Search recent trace spans by service, operation, tenant, error status, duration, "
            "or exact trace ID."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "timeRange": {"type": "string", "description": "Default: 1h."},
                "service": {"type": "string"},
                "operation": {"type": "string"},
                "tenant": {"type": "string"},
                "environment": {"type": "string"},
                "deployment": {"type": "string"},
                "traceId": {"type": "string"},
                "error": {"type": "boolean"},
                "minDuration": {"type": ["integer", "string"], "description": "Nanoseconds."},
                "maxDuration": {"type": ["integer", "string"], "description": "Nanoseconds."},
                "limit": {"type": ["integer", "string"], "description": "Default: 100."},
                "offset": {"type": ["integer", "string"], "description": "Default: 0."},
            },
        },
    },
    {
        "name": "signoz_aggregate_traces",
        "description": (
            "Aggregate trace spans by latency, errors, services, operations, tenants, or "
            "other safe fields, optionally as a time series."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "aggregation": {
                    "type": "string",
                    "enum": ["count", "count_distinct", "avg", "min", "max", "sum", "p50", "p75", "p90", "p95", "p99"],
                },
                "aggregateOn": {"type": "string"},
                "groupBy": {"type": "string", "description": "Comma-separated safe fields, up to 3."},
                "requestType": {"type": "string", "enum": ["scalar", "time_series"]},
                "stepInterval": {"type": ["integer", "string"], "description": "Seconds."},
                "timeRange": {"type": "string", "description": "Default: 1h."},
                "service": {"type": "string"},
                "operation": {"type": "string"},
                "tenant": {"type": "string"},
                "environment": {"type": "string"},
                "deployment": {"type": "string"},
                "error": {"type": "boolean"},
                "minDuration": {"type": ["integer", "string"]},
                "maxDuration": {"type": ["integer", "string"]},
                "limit": {"type": ["integer", "string"], "description": "Default: 100."},
            },
        },
    },
    {
        "name": "signoz_query_metrics",
        "description": (
            "Query metric samples with safe aggregations and Kubernetes resource filters. "
            "Use signoz_list_metrics first when the metric name is unknown."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "metricName": {"type": "string"},
                "aggregation": {
                    "type": "string",
                    "enum": ["avg", "min", "max", "sum", "latest"],
                },
                "groupBy": {"type": "string", "description": "Comma-separated safe fields, up to 3."},
                "requestType": {"type": "string", "enum": ["scalar", "time_series"]},
                "stepInterval": {"type": ["integer", "string"], "description": "Seconds."},
                "timeRange": {"type": "string", "description": "Default: 1h."},
                "environment": {"type": "string"},
                "deployment": {"type": "string"},
                "pod": {"type": "string"},
                "limit": {"type": ["integer", "string"], "description": "Default: 100."},
            },
            "required": ["metricName"],
        },
    },
]


_SIGNALS = {
    "logs": {
        "table": "signoz_logs.distributed_logs_v2",
        "attribute_map": "attributes_string",
        "resource_map": "resources_string",
        "time_filter": "timestamp >= {start_ns} AND timestamp <= {end_ns}",
        "intrinsic": {
            "timestamp": "timestamp",
            "id": "id",
            "trace_id": "trace_id",
            "span_id": "span_id",
            "severity_text": "severity_text",
            "body": "body",
        },
    },
    "traces": {
        "table": "signoz_traces.distributed_signoz_index_v3",
        "attribute_map": "attributes_string",
        "resource_map": "resources_string",
        "time_filter": (
            "timestamp >= fromUnixTimestamp64Nano({start_ns}) "
            "AND timestamp <= fromUnixTimestamp64Nano({end_ns})"
        ),
        "intrinsic": {
            "timestamp": "timestamp",
            "trace_id": "trace_id",
            "span_id": "span_id",
            "parent_span_id": "parent_span_id",
            "service.name": "serviceName",
            "serviceName": "serviceName",
            "name": "name",
            "operation": "name",
            "duration_nano": "duration_nano",
            "has_error": "has_error",
            "status_code_string": "status_code_string",
            "response_status_code": "response_status_code",
        },
    },
    "metrics": {
        "table": "signoz_metrics.distributed_time_series_v4",
        "attribute_map": "attrs",
        "resource_map": "resource_attrs",
        "time_filter": (
            "unix_milli >= intDiv({start_ns}, 1000000) "
            "AND unix_milli <= intDiv({end_ns}, 1000000)"
        ),
        "intrinsic": {
            "metric_name": "metric_name",
            "description": "description",
            "unit": "unit",
            "type": "type",
            "temporality": "temporality",
        },
    },
}


def _time_range_hours(value: Any, default: str) -> str:
    raw = default if value in (None, "") else str(value).strip().lower()
    match = _TIME_RANGE_RE.fullmatch(raw)
    if not match:
        raise ValueError("timeRange must look like 30m, 2h, or 1d")
    amount = float(match.group(1))
    unit = match.group(2)
    hours = amount / 60 if unit == "m" else amount * 24 if unit == "d" else amount
    hours = max(0.01, min(hours, 168.0))
    return str(int(hours)) if hours.is_integer() else str(hours)


def _bounded_int(value: Any, default: int, *, minimum: int = 1, maximum: int = 1000) -> int:
    if value in (None, ""):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("limit must be an integer") from exc
    return max(minimum, min(parsed, maximum))


def _sql_literal(value: str) -> str:
    if len(value) > 4096:
        raise ValueError("query value must not exceed 4096 characters")
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _signal_config(value: Any) -> tuple[str, dict[str, Any]]:
    signal = str(value or "").strip().lower()
    config = _SIGNALS.get(signal)
    if config is None:
        raise ValueError("signal must be logs, traces, or metrics")
    return signal, config


def _safe_field_name(value: Any) -> str:
    field = str(value or "").strip()
    if not field or not _FIELD_RE.fullmatch(field):
        raise ValueError("field must contain only letters, numbers, dots, underscores, or hyphens")
    return field


def _field_expression(
    signal: str,
    field: Any,
    *,
    context: str | None = None,
    numeric: bool = False,
) -> tuple[str, str]:
    name = _safe_field_name(field)
    config = _SIGNALS[signal]

    if context is None:
        if name.startswith("attribute."):
            context, name = "attribute", name.removeprefix("attribute.")
        elif name.startswith("resource."):
            context, name = "resource", name.removeprefix("resource.")
        else:
            context = "intrinsic"
    if context not in {"attribute", "resource", "intrinsic"}:
        raise ValueError("fieldContext must be attribute, resource, or intrinsic")
    name = _safe_field_name(name)

    if context == "attribute":
        map_name = "attributes_number" if numeric and signal != "metrics" else config["attribute_map"]
        return f"{map_name}['{_sql_literal(name)}']", name
    if context == "resource":
        return f"{config['resource_map']}['{_sql_literal(name)}']", name

    expression = config["intrinsic"].get(name)
    if expression is None:
        raise ValueError(f"unsupported intrinsic field for {signal}: {name}")
    return expression, name


def _non_negative_int(
    value: Any,
    field_name: str,
    default: int | None = None,
    *,
    maximum: int | None = None,
) -> int | None:
    if value in (None, ""):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an integer") from exc
    if parsed < 0:
        raise ValueError(f"{field_name} must not be negative")
    if maximum is not None:
        parsed = min(parsed, maximum)
    return parsed


def _validate_trace_id(value: Any) -> str:
    trace_id = str(value or "")
    if not _TRACE_ID_RE.fullmatch(trace_id):
        raise ValueError("traceId must be exactly 32 hexadecimal characters")
    return trace_id.lower()


def _log_filters(arguments: dict[str, Any]) -> list[str]:
    where = [_SIGNALS["logs"]["time_filter"]]
    exact_filters = (
        ("environment", "resources_string['deployment.environment']"),
        ("deployment", "resources_string['k8s.deployment.name']"),
        ("tenant", "attributes_string['tenant_id']"),
        ("severity", "severity_text"),
    )
    for argument_name, expression in exact_filters:
        value = arguments.get(argument_name)
        if value not in (None, ""):
            where.append(f"{expression} = '{_sql_literal(str(value))}'")
    if arguments.get("traceId") not in (None, ""):
        where.append(f"trace_id = '{_validate_trace_id(arguments['traceId'])}'")
    search_text = arguments.get("searchText")
    if search_text not in (None, ""):
        where.append(f"body LIKE '%{_sql_literal(str(search_text))}%'")
    return where


def _trace_filters(arguments: dict[str, Any]) -> list[str]:
    where = [_SIGNALS["traces"]["time_filter"]]
    exact_filters = (
        ("service", "serviceName"),
        ("operation", "name"),
        ("tenant", "attributes_string['dify.tenant_id']"),
        ("environment", "resources_string['deployment.environment']"),
        ("deployment", "resources_string['k8s.deployment.name']"),
    )
    for argument_name, expression in exact_filters:
        value = arguments.get(argument_name)
        if value not in (None, ""):
            where.append(f"{expression} = '{_sql_literal(str(value))}'")
    if arguments.get("traceId") not in (None, ""):
        where.append(f"trace_id = '{_validate_trace_id(arguments['traceId'])}'")
    if "error" in arguments and arguments["error"] is not None:
        if not isinstance(arguments["error"], bool):
            raise ValueError("error must be a boolean")
        where.append(f"has_error = {'true' if arguments['error'] else 'false'}")
    minimum = _non_negative_int(arguments.get("minDuration"), "minDuration")
    maximum = _non_negative_int(arguments.get("maxDuration"), "maxDuration")
    if minimum is not None:
        where.append(f"duration_nano >= {minimum}")
    if maximum is not None:
        where.append(f"duration_nano <= {maximum}")
    return where


def _group_expressions(signal: str, value: Any) -> list[tuple[str, str]]:
    if value in (None, ""):
        return []
    fields = [part.strip() for part in str(value).split(",") if part.strip()]
    if not fields or len(fields) > 3:
        raise ValueError("groupBy field list must contain between 1 and 3 fields")
    return [_field_expression(signal, field) for field in fields]


def _aggregation_expression(signal: str, arguments: dict[str, Any]) -> str:
    aggregation = str(arguments.get("aggregation") or "count").lower()
    allowed = {"count", "count_distinct", "avg", "min", "max", "sum", "p50", "p75", "p90", "p95", "p99"}
    if aggregation not in allowed:
        raise ValueError(f"unsupported aggregation: {aggregation}")
    if aggregation == "count":
        return "count()"

    aggregate_on = arguments.get("aggregateOn")
    if aggregate_on in (None, ""):
        raise ValueError(f"aggregateOn field is required for {aggregation}")
    expression, _ = _field_expression(
        signal,
        aggregate_on,
        numeric=aggregation != "count_distinct",
    )
    if aggregation == "count_distinct":
        return f"uniqExact({expression})"
    if aggregation.startswith("p"):
        quantile = int(aggregation[1:]) / 100
        return f"quantile({quantile})({expression})"
    return f"{aggregation}({expression})"


def _aggregate_sql(signal: str, arguments: dict[str, Any], where: list[str]) -> str:
    config = _SIGNALS[signal]
    aggregate = _aggregation_expression(signal, arguments)
    groups = _group_expressions(signal, arguments.get("groupBy"))
    select_parts: list[str] = []
    group_aliases: list[str] = []

    request_type = str(arguments.get("requestType") or "scalar").lower()
    if request_type not in {"scalar", "time_series"}:
        raise ValueError("requestType must be scalar or time_series")
    if request_type == "time_series":
        step = _bounded_int(arguments.get("stepInterval"), 300, minimum=1, maximum=86400)
        source_time = (
            "fromUnixTimestamp64Nano(toInt64(timestamp))"
            if signal == "logs"
            else "timestamp"
        )
        select_parts.append(
            f"toStartOfInterval({source_time}, INTERVAL {step} SECOND) AS bucket"
        )
        group_aliases.append("bucket")

    for expression, alias in groups:
        select_parts.append(f"{expression} AS `{alias}`")
        group_aliases.append(f"`{alias}`")
    select_parts.append(f"{aggregate} AS value")

    limit = _bounded_int(arguments.get("limit"), 100)
    group_clause = f" GROUP BY {', '.join(group_aliases)}" if group_aliases else ""
    order_clause = " ORDER BY bucket ASC" if request_type == "time_series" else " ORDER BY value DESC"
    return (
        f"SELECT {', '.join(select_parts)} FROM {config['table']} "
        f"WHERE {' AND '.join(where)}{group_clause}{order_clause} LIMIT {limit}"
    )


def _metric_field_expression(field: Any) -> tuple[str, str]:
    expression, alias = _field_expression("metrics", field)
    if expression.startswith(("attrs[", "resource_attrs[")):
        expression = f"t.{expression}"
    else:
        expression = f"t.{expression}"
    return expression, alias


def _metric_query_sql(arguments: dict[str, Any]) -> str:
    metric_name = str(arguments.get("metricName") or "").strip()
    if not metric_name:
        raise ValueError("metricName is required")
    metric_literal = _sql_literal(metric_name)
    aggregation = str(arguments.get("aggregation") or "avg").lower()
    metric_aggregations = {
        "avg": "avg(s.value)",
        "min": "min(s.value)",
        "max": "max(s.value)",
        "sum": "sum(s.value)",
        "latest": "argMax(s.value, s.unix_milli)",
    }
    aggregate = metric_aggregations.get(aggregation)
    if aggregate is None:
        raise ValueError(f"unsupported metric aggregation: {aggregation}")

    groups: list[tuple[str, str]] = []
    raw_group_by = arguments.get("groupBy")
    if raw_group_by not in (None, ""):
        fields = [part.strip() for part in str(raw_group_by).split(",") if part.strip()]
        if not fields or len(fields) > 3:
            raise ValueError("groupBy field list must contain between 1 and 3 fields")
        groups = [_metric_field_expression(field) for field in fields]

    select_parts: list[str] = []
    group_aliases: list[str] = []
    request_type = str(arguments.get("requestType") or "scalar").lower()
    if request_type not in {"scalar", "time_series"}:
        raise ValueError("requestType must be scalar or time_series")
    if request_type == "time_series":
        step = _bounded_int(arguments.get("stepInterval"), 300, minimum=1, maximum=86400)
        select_parts.append(
            "toStartOfInterval(fromUnixTimestamp64Milli(s.unix_milli), "
            f"INTERVAL {step} SECOND) AS bucket"
        )
        group_aliases.append("bucket")
    for expression, alias in groups:
        select_parts.append(f"{expression} AS `{alias}`")
        group_aliases.append(f"`{alias}`")
    select_parts.append(f"{aggregate} AS value")

    where = [
        "s.unix_milli >= intDiv({start_ns}, 1000000)",
        "s.unix_milli <= intDiv({end_ns}, 1000000)",
        f"s.metric_name = '{metric_literal}'",
        f"t.metric_name = '{metric_literal}'",
    ]
    resource_filters = (
        ("environment", "deployment.environment"),
        ("deployment", "k8s.deployment.name"),
        ("pod", "k8s.pod.name"),
    )
    for argument_name, resource_name in resource_filters:
        value = arguments.get(argument_name)
        if value not in (None, ""):
            where.append(
                f"t.resource_attrs['{resource_name}'] = '{_sql_literal(str(value))}'"
            )

    group_clause = f" GROUP BY {', '.join(group_aliases)}" if group_aliases else ""
    order_clause = " ORDER BY bucket ASC" if request_type == "time_series" else " ORDER BY value DESC"
    limit = _bounded_int(arguments.get("limit"), 100)
    return (
        f"SELECT {', '.join(select_parts)} "
        "FROM signoz_metrics.distributed_samples_v4 AS s "
        "ANY INNER JOIN signoz_metrics.distributed_time_series_v4 AS t "
        "ON s.metric_name = t.metric_name AND s.fingerprint = t.fingerprint "
        f"WHERE {' AND '.join(where)}{group_clause}{order_clause} LIMIT {limit}"
    )


def build_structured_sz_args(tool_name: str, arguments: Any) -> list[str]:
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be an object")

    if tool_name == "signoz_list_services":
        hours = _time_range_hours(arguments.get("timeRange"), "1h")
        limit = _bounded_int(arguments.get("limit"), 30)
        args = ["services", "--hours", hours, "--limit", str(limit)]
        environment = arguments.get("environment")
        if environment:
            args.extend(["--env", str(environment)])
        return args

    if tool_name == "signoz_get_trace_details":
        trace_id = str(arguments.get("traceId") or "")
        if not _TRACE_ID_RE.fullmatch(trace_id):
            raise ValueError("traceId must be exactly 32 hexadecimal characters")
        hours = _time_range_hours(arguments.get("timeRange"), "6h")
        limit = _bounded_int(arguments.get("limit"), 500)
        sql = (
            "SELECT toString(timestamp) AS timestamp_utc, trace_id, span_id, parent_span_id, "
            "serviceName AS service, name AS operation, kind_string AS span_kind, "
            "duration_nano, has_error, status_code_string, status_message, "
            "http_method, http_url, response_status_code, attributes_string, resources_string "
            "FROM signoz_traces.distributed_signoz_index_v3 "
            "WHERE timestamp >= fromUnixTimestamp64Nano({start_ns}) "
            "AND timestamp <= fromUnixTimestamp64Nano({end_ns}) "
            f"AND trace_id = '{trace_id.lower()}' "
            f"ORDER BY timestamp ASC LIMIT {limit}"
        )
        return ["query", sql, "--hours", hours]

    if tool_name == "signoz_list_metrics":
        hours = _time_range_hours(arguments.get("timeRange"), "1h")
        limit = _bounded_int(arguments.get("limit"), 50)
        where = [
            "unix_milli >= intDiv({start_ns}, 1000000)",
            "unix_milli <= intDiv({end_ns}, 1000000)",
        ]
        search_text = arguments.get("searchText")
        if search_text:
            where.append(
                "lower(metric_name) LIKE '%"
                + _sql_literal(str(search_text).lower())
                + "%'"
            )
        sql = (
            "SELECT metric_name, any(description) AS description, any(type) AS type, "
            "any(unit) AS unit, any(temporality) AS temporality, "
            "any(is_monotonic) AS is_monotonic "
            "FROM signoz_metrics.distributed_time_series_v4 "
            f"WHERE {' AND '.join(where)} "
            "GROUP BY metric_name ORDER BY metric_name "
            f"LIMIT {limit}"
        )
        return ["query", sql, "--hours", hours]

    if tool_name == "signoz_get_field_keys":
        signal, config = _signal_config(arguments.get("signal"))
        hours = _time_range_hours(arguments.get("timeRange"), "1h")
        limit = _bounded_int(arguments.get("limit"), 100)
        attribute_map = config["attribute_map"]
        resource_map = config["resource_map"]
        time_filter = config["time_filter"]
        intrinsic_names = ", ".join(
            f"'{_sql_literal(name)}'" for name in config["intrinsic"]
        )
        search_clause = ""
        if arguments.get("searchText") not in (None, ""):
            search_clause = (
                " WHERE lower(name) LIKE '%"
                + _sql_literal(str(arguments["searchText"]).lower())
                + "%'"
            )
        sql = (
            "SELECT name, context FROM ("
            f"SELECT arrayJoin(mapKeys({attribute_map})) AS name, 'attribute' AS context "
            f"FROM (SELECT {attribute_map} FROM {config['table']} WHERE {time_filter} LIMIT 10000) "
            "UNION ALL "
            f"SELECT arrayJoin(mapKeys({resource_map})) AS name, 'resource' AS context "
            f"FROM (SELECT {resource_map} FROM {config['table']} WHERE {time_filter} LIMIT 10000) "
            "UNION ALL "
            f"SELECT arrayJoin([{intrinsic_names}]) AS name, 'intrinsic' AS context"
            f"){search_clause} GROUP BY name, context ORDER BY context, name LIMIT {limit}"
        )
        return ["query", sql, "--hours", hours]

    if tool_name == "signoz_get_field_values":
        signal, config = _signal_config(arguments.get("signal"))
        hours = _time_range_hours(arguments.get("timeRange"), "1h")
        limit = _bounded_int(arguments.get("limit"), 50)
        expression, _ = _field_expression(
            signal,
            arguments.get("name"),
            context=str(arguments.get("fieldContext") or "").lower(),
        )
        where = [config["time_filter"], f"toString({expression}) != ''"]
        if arguments.get("searchText") not in (None, ""):
            where.append(
                f"lower(toString({expression})) LIKE '%"
                f"{_sql_literal(str(arguments['searchText']).lower())}%'"
            )
        sql = (
            f"SELECT toString({expression}) AS value, count() AS count "
            f"FROM {config['table']} WHERE {' AND '.join(where)} "
            f"GROUP BY value ORDER BY count DESC LIMIT {limit}"
        )
        return ["query", sql, "--hours", hours]

    if tool_name == "signoz_search_logs":
        hours = _time_range_hours(arguments.get("timeRange"), "1h")
        limit = _bounded_int(arguments.get("limit"), 100)
        offset = _non_negative_int(arguments.get("offset"), "offset", 0, maximum=100000)
        sql = (
            "SELECT fromUnixTimestamp64Nano(toInt64(timestamp)) AS timestamp_utc, id, trace_id, "
            "span_id, severity_text, body, attributes_string, resources_string "
            "FROM signoz_logs.distributed_logs_v2 "
            f"WHERE {' AND '.join(_log_filters(arguments))} "
            f"ORDER BY timestamp DESC LIMIT {limit} OFFSET {offset}"
        )
        return ["query", sql, "--hours", hours]

    if tool_name == "signoz_aggregate_logs":
        hours = _time_range_hours(arguments.get("timeRange"), "1h")
        sql = _aggregate_sql("logs", arguments, _log_filters(arguments))
        return ["query", sql, "--hours", hours]

    if tool_name == "signoz_search_traces":
        hours = _time_range_hours(arguments.get("timeRange"), "1h")
        limit = _bounded_int(arguments.get("limit"), 100)
        offset = _non_negative_int(arguments.get("offset"), "offset", 0, maximum=100000)
        sql = (
            "SELECT toString(timestamp) AS timestamp_utc, trace_id, span_id, parent_span_id, "
            "serviceName AS service, name AS operation, duration_nano, has_error, "
            "status_code_string, response_status_code, attributes_string, resources_string "
            "FROM signoz_traces.distributed_signoz_index_v3 "
            f"WHERE {' AND '.join(_trace_filters(arguments))} "
            f"ORDER BY timestamp DESC LIMIT {limit} OFFSET {offset}"
        )
        return ["query", sql, "--hours", hours]

    if tool_name == "signoz_aggregate_traces":
        hours = _time_range_hours(arguments.get("timeRange"), "1h")
        sql = _aggregate_sql("traces", arguments, _trace_filters(arguments))
        return ["query", sql, "--hours", hours]

    if tool_name == "signoz_query_metrics":
        hours = _time_range_hours(arguments.get("timeRange"), "1h")
        return ["query", _metric_query_sql(arguments), "--hours", hours]

    raise ValueError(f"unknown structured tool: {tool_name}")
