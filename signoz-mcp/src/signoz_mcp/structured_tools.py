from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from typing import Any, Callable

from .log_compact import compact_log_rows
from .settings import Settings, get_settings
from .timewindow import TimeWindow, resolve_window


_TRACE_ID_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_FIELD_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

_START_PROPERTY = {
    "type": "string",
    "description": (
        "Absolute window start, ISO 8601 such as 2026-10-01T08:00:00Z; no offset means UTC. "
        "start/end take precedence over timeRange."
    ),
}
_END_PROPERTY = {
    "type": "string",
    "description": (
        "Absolute window end, ISO 8601; no offset means UTC. Default: now. "
        "With only end, the window is timeRange before end."
    ),
}
_CHUNK_NOTE = (
    " Long windows are split into sub-window queries and merged "
    "(count, sum, min, max, avg; percentiles and count_distinct run as one query)."
)


_BASE_TOOLS: list[dict[str, Any]] = [
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
            "trace, and body filters. Results are compacted by default: low-value host/process "
            "resource keys are dropped, repeated text (e.g. a traceback in both body and "
            "exception.stacktrace) is kept once, and long text is truncated with a marker."
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
                "fields": {
                    "type": ["array", "string"],
                    "items": {"type": "string"},
                    "description": (
                        "Columns to return (array or comma-separated): timestamp_utc, id, trace_id, "
                        "span_id, severity_text, body, attributes_string, resources_string, "
                        "attribute.<key>, resource.<key>. Default: all of the plain columns plus "
                        "both maps."
                    ),
                },
                "compact": {
                    "type": "boolean",
                    "description": (
                        "Default: true. Drop denylisted resource/attribute keys (host.ip, host.mac, ...) "
                        "and empty values, de-duplicate repeated long text, truncate long text. "
                        "false returns raw rows."
                    ),
                },
                "maxFieldChars": {
                    "type": ["integer", "string"],
                    "description": "Per-string truncation length when compact, from 200 to 100000. Default: 4000.",
                },
                "limit": {"type": ["integer", "string"], "description": "Default: 100."},
                "offset": {"type": ["integer", "string"], "description": "Default: 0."},
            },
        },
    },
    {
        "name": "signoz_aggregate_logs",
        "description": (
            "Aggregate recent logs safely, optionally grouped or bucketed over time." + _CHUNK_NOTE
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
            "other safe fields, optionally as a time series." + _CHUNK_NOTE
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
            " Long windows are split into sub-window queries and merged."
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


def _with_absolute_time(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Insert start/end right after timeRange in every tool that has a window."""
    result = copy.deepcopy(tools)
    for tool in result:
        properties = tool["inputSchema"]["properties"]
        if "timeRange" not in properties:
            continue
        rebuilt: dict[str, Any] = {}
        for name, schema in properties.items():
            rebuilt[name] = schema
            if name == "timeRange":
                rebuilt["start"] = dict(_START_PROPERTY)
                rebuilt["end"] = dict(_END_PROPERTY)
        tool["inputSchema"]["properties"] = rebuilt
    return result


STRUCTURED_TOOLS: list[dict[str, Any]] = _with_absolute_time(_BASE_TOOLS)


_SIGNALS = {
    "logs": {
        "table": "signoz_logs.distributed_logs_v2",
        "attribute_map": "attributes_string",
        "resource_map": "resources_string",
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
        "intrinsic": {
            "metric_name": "metric_name",
            "description": "description",
            "unit": "unit",
            "type": "type",
            "temporality": "temporality",
        },
    },
}

# ts_bucket_start is the leading primary-key column of SigNoz logs v2 and traces v3
# (30-minute buckets, seconds). SigNoz's own query builder adds the same predicate.
_TS_BUCKET_FILTER = (
    "ts_bucket_start >= intDiv({start_ns}, 1000000000) - 1800 "
    "AND ts_bucket_start <= intDiv({end_ns}, 1000000000)"
)


def _time_filter(signal: str, *, pruning: bool = False, exclusive_end: bool = False) -> str:
    end_op = "<" if exclusive_end else "<="
    if signal == "logs":
        base = f"timestamp >= {{start_ns}} AND timestamp {end_op} {{end_ns}}"
    elif signal == "traces":
        base = (
            "timestamp >= fromUnixTimestamp64Nano({start_ns}) "
            f"AND timestamp {end_op} fromUnixTimestamp64Nano({{end_ns}})"
        )
    else:
        return (
            "unix_milli >= intDiv({start_ns}, 1000000) "
            f"AND unix_milli {end_op} intDiv({{end_ns}}, 1000000)"
        )
    return f"{base} AND {_TS_BUCKET_FILTER}" if pruning else base


@dataclass
class ChunkSpec:
    """How to run one aggregate as several sub-window queries and merge them."""

    build_sql: Callable[[bool], str]  # exclusive_end -> SQL for one sub-window
    key_columns: tuple[str, ...]
    merge_ops: dict[str, str]  # column -> sum | min | max
    finalize: str | None  # None | "avg" (value = __sum / __count) | "latest" (max __ts wins)
    sort_column: str
    sort_desc: bool
    limit: int
    row_limit: int


@dataclass
class StructuredPlan:
    tool: str
    args: list[str]
    window: TimeWindow
    chunk: ChunkSpec | None = None
    chunk_unavailable_reason: str | None = None
    postprocess: Callable[[Any], tuple[Any, dict[str, Any]]] | None = field(default=None, repr=False)


def _bounded_int(
    value: Any,
    default: int,
    *,
    minimum: int = 1,
    maximum: int = 1000,
    field_name: str = "limit",
) -> int:
    if value in (None, ""):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an integer") from exc
    return max(minimum, min(parsed, maximum))


def _bool_arg(value: Any, field_name: str, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    raise ValueError(f"{field_name} must be a boolean")


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
    field_name = str(value or "").strip()
    if not field_name or not _FIELD_RE.fullmatch(field_name):
        raise ValueError("field must contain only letters, numbers, dots, underscores, or hyphens")
    return field_name


def _field_expression(
    signal: str,
    field_value: Any,
    *,
    context: str | None = None,
    numeric: bool = False,
) -> tuple[str, str]:
    name = _safe_field_name(field_value)
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


def _log_filters(
    arguments: dict[str, Any],
    *,
    pruning: bool = False,
    exclusive_end: bool = False,
) -> list[str]:
    where = [_time_filter("logs", pruning=pruning, exclusive_end=exclusive_end)]
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


def _trace_filters(
    arguments: dict[str, Any],
    *,
    pruning: bool = False,
    exclusive_end: bool = False,
) -> list[str]:
    where = [_time_filter("traces", pruning=pruning, exclusive_end=exclusive_end)]
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
    return [_field_expression(signal, item) for item in fields]


_ALLOWED_AGGREGATIONS = {
    "count", "count_distinct", "avg", "min", "max", "sum", "p50", "p75", "p90", "p95", "p99",
}
# Aggregations whose per-sub-window results can be merged exactly.
_CHUNK_MERGE = {"count": "sum", "sum": "sum", "min": "min", "max": "max", "avg": None}


def _aggregation_expression(signal: str, arguments: dict[str, Any]) -> tuple[str, str, str | None]:
    """Return (aggregation name, SQL aggregate, operand expression)."""
    aggregation = str(arguments.get("aggregation") or "count").lower()
    if aggregation not in _ALLOWED_AGGREGATIONS:
        raise ValueError(f"unsupported aggregation: {aggregation}")
    if aggregation == "count":
        return aggregation, "count()", None

    aggregate_on = arguments.get("aggregateOn")
    if aggregate_on in (None, ""):
        raise ValueError(f"aggregateOn field is required for {aggregation}")
    expression, _ = _field_expression(
        signal,
        aggregate_on,
        numeric=aggregation != "count_distinct",
    )
    if aggregation == "count_distinct":
        return aggregation, f"uniqExact({expression})", expression
    if aggregation.startswith("p"):
        quantile = int(aggregation[1:]) / 100
        return aggregation, f"quantile({quantile})({expression})", expression
    return aggregation, f"{aggregation}({expression})", expression


def _request_type(arguments: dict[str, Any]) -> str:
    request_type = str(arguments.get("requestType") or "scalar").lower()
    if request_type not in {"scalar", "time_series"}:
        raise ValueError("requestType must be scalar or time_series")
    return request_type


def _aggregate_sql(
    signal: str,
    arguments: dict[str, Any],
    where: list[str],
    *,
    chunk_row_limit: int | None = None,
) -> str:
    """Build the aggregate SQL. With ``chunk_row_limit`` the query is shaped for
    merging across sub-windows (avg is split into __sum/__count, large LIMIT)."""
    config = _SIGNALS[signal]
    aggregation, aggregate, operand = _aggregation_expression(signal, arguments)
    groups = _group_expressions(signal, arguments.get("groupBy"))
    select_parts: list[str] = []
    group_aliases: list[str] = []

    request_type = _request_type(arguments)
    if request_type == "time_series":
        step = _bounded_int(
            arguments.get("stepInterval"), 300, minimum=1, maximum=86400, field_name="stepInterval"
        )
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

    order_column = "value"
    if chunk_row_limit is not None and aggregation == "avg":
        select_parts.append(f"sum({operand}) AS __sum")
        select_parts.append("count() AS __count")
        order_column = "__count"
    else:
        select_parts.append(f"{aggregate} AS value")

    limit = chunk_row_limit if chunk_row_limit is not None else _bounded_int(arguments.get("limit"), 100)
    group_clause = f" GROUP BY {', '.join(group_aliases)}" if group_aliases else ""
    order_clause = (
        " ORDER BY bucket ASC" if request_type == "time_series" else f" ORDER BY {order_column} DESC"
    )
    return (
        f"SELECT {', '.join(select_parts)} FROM {config['table']} "
        f"WHERE {' AND '.join(where)}{group_clause}{order_clause} LIMIT {limit}"
    )


def _aggregate_plan_chunk(
    signal: str,
    arguments: dict[str, Any],
    filters: Callable[..., list[str]],
    *,
    pruning: bool,
    row_limit: int,
) -> tuple[ChunkSpec | None, str | None]:
    aggregation, _, _ = _aggregation_expression(signal, arguments)
    if aggregation not in _CHUNK_MERGE:
        return None, f"{aggregation} cannot be merged across sub-windows; it runs as one query"
    request_type = _request_type(arguments)
    keys = (["bucket"] if request_type == "time_series" else []) + [
        alias for _, alias in _group_expressions(signal, arguments.get("groupBy"))
    ]
    merge_op = _CHUNK_MERGE[aggregation]
    return (
        ChunkSpec(
            build_sql=lambda exclusive_end: _aggregate_sql(
                signal,
                arguments,
                filters(arguments, pruning=pruning, exclusive_end=exclusive_end),
                chunk_row_limit=row_limit,
            ),
            key_columns=tuple(keys),
            merge_ops={"__sum": "sum", "__count": "sum"} if merge_op is None else {"value": merge_op},
            finalize="avg" if merge_op is None else None,
            sort_column="bucket" if request_type == "time_series" else "value",
            sort_desc=request_type != "time_series",
            limit=_bounded_int(arguments.get("limit"), 100),
            row_limit=row_limit,
        ),
        None,
    )


def _metric_field_expression(field_value: Any) -> tuple[str, str]:
    expression, alias = _field_expression("metrics", field_value)
    return f"t.{expression}", alias


_METRIC_AGGREGATIONS = {
    "avg": "avg(s.value)",
    "min": "min(s.value)",
    "max": "max(s.value)",
    "sum": "sum(s.value)",
    "latest": "argMax(s.value, s.unix_milli)",
}


def _metric_query_sql(
    arguments: dict[str, Any],
    *,
    exclusive_end: bool = False,
    chunk_row_limit: int | None = None,
) -> str:
    metric_name = str(arguments.get("metricName") or "").strip()
    if not metric_name:
        raise ValueError("metricName is required")
    metric_literal = _sql_literal(metric_name)
    aggregation = str(arguments.get("aggregation") or "avg").lower()
    aggregate = _METRIC_AGGREGATIONS.get(aggregation)
    if aggregate is None:
        raise ValueError(f"unsupported metric aggregation: {aggregation}")

    groups: list[tuple[str, str]] = []
    raw_group_by = arguments.get("groupBy")
    if raw_group_by not in (None, ""):
        fields = [part.strip() for part in str(raw_group_by).split(",") if part.strip()]
        if not fields or len(fields) > 3:
            raise ValueError("groupBy field list must contain between 1 and 3 fields")
        groups = [_metric_field_expression(item) for item in fields]

    select_parts: list[str] = []
    group_aliases: list[str] = []
    request_type = _request_type(arguments)
    if request_type == "time_series":
        step = _bounded_int(
            arguments.get("stepInterval"), 300, minimum=1, maximum=86400, field_name="stepInterval"
        )
        select_parts.append(
            "toStartOfInterval(fromUnixTimestamp64Milli(s.unix_milli), "
            f"INTERVAL {step} SECOND) AS bucket"
        )
        group_aliases.append("bucket")
    for expression, alias in groups:
        select_parts.append(f"{expression} AS `{alias}`")
        group_aliases.append(f"`{alias}`")

    order_column = "value"
    if chunk_row_limit is not None and aggregation == "avg":
        select_parts.extend(["sum(s.value) AS __sum", "count() AS __count"])
        order_column = "__count"
    elif chunk_row_limit is not None and aggregation == "latest":
        select_parts.extend([f"{aggregate} AS value", "max(s.unix_milli) AS __ts"])
    else:
        select_parts.append(f"{aggregate} AS value")

    end_op = "<" if exclusive_end else "<="
    where = [
        "s.unix_milli >= intDiv({start_ns}, 1000000)",
        f"s.unix_milli {end_op} intDiv({{end_ns}}, 1000000)",
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
    order_clause = (
        " ORDER BY bucket ASC" if request_type == "time_series" else f" ORDER BY {order_column} DESC"
    )
    limit = chunk_row_limit if chunk_row_limit is not None else _bounded_int(arguments.get("limit"), 100)
    return (
        f"SELECT {', '.join(select_parts)} "
        "FROM signoz_metrics.distributed_samples_v4 AS s "
        "ANY INNER JOIN signoz_metrics.distributed_time_series_v4 AS t "
        "ON s.metric_name = t.metric_name AND s.fingerprint = t.fingerprint "
        f"WHERE {' AND '.join(where)}{group_clause}{order_clause} LIMIT {limit}"
    )


def _metric_plan_chunk(arguments: dict[str, Any], row_limit: int) -> ChunkSpec:
    aggregation = str(arguments.get("aggregation") or "avg").lower()
    request_type = _request_type(arguments)
    keys = (["bucket"] if request_type == "time_series" else []) + [
        alias
        for _, alias in (
            _metric_field_expression(item)
            for item in str(arguments.get("groupBy") or "").split(",")
            if item.strip()
        )
    ]
    if aggregation == "avg":
        merge_ops, finalize = {"__sum": "sum", "__count": "sum"}, "avg"
    elif aggregation == "latest":
        merge_ops, finalize = {}, "latest"
    else:
        merge_ops, finalize = {"value": aggregation}, None
    return ChunkSpec(
        build_sql=lambda exclusive_end: _metric_query_sql(
            arguments, exclusive_end=exclusive_end, chunk_row_limit=row_limit
        ),
        key_columns=tuple(keys),
        merge_ops=merge_ops,
        finalize=finalize,
        sort_column="bucket" if request_type == "time_series" else "value",
        sort_desc=request_type != "time_series",
        limit=_bounded_int(arguments.get("limit"), 100),
        row_limit=row_limit,
    )


_LOG_TIMESTAMP_COLUMN = "fromUnixTimestamp64Nano(toInt64(timestamp)) AS timestamp_utc"
_LOG_DEFAULT_COLUMNS = (
    f"{_LOG_TIMESTAMP_COLUMN}, id, trace_id, "
    "span_id, severity_text, body, attributes_string, resources_string"
)
_LOG_FIELD_COLUMNS = {
    "timestamp_utc": _LOG_TIMESTAMP_COLUMN,
    "timestamp": _LOG_TIMESTAMP_COLUMN,
    "id": "id",
    "trace_id": "trace_id",
    "span_id": "span_id",
    "severity_text": "severity_text",
    "severity": "severity_text",
    "body": "body",
    "attributes_string": "attributes_string",
    "attributes": "attributes_string",
    "resources_string": "resources_string",
    "resources": "resources_string",
}


def _log_select_columns(value: Any) -> str:
    if value in (None, "", []):
        return _LOG_DEFAULT_COLUMNS
    if isinstance(value, str):
        names = [part.strip() for part in value.split(",") if part.strip()]
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        names = [item.strip() for item in value if item.strip()]
    else:
        raise ValueError("fields must be an array of strings or a comma-separated string")
    if not names or len(names) > 30:
        raise ValueError("fields must contain between 1 and 30 field names")
    columns: list[str] = []
    for name in names:
        if name in _LOG_FIELD_COLUMNS:
            column = _LOG_FIELD_COLUMNS[name]
        elif name.startswith(("attribute.", "resource.")):
            context, key = name.split(".", 1)
            key = _safe_field_name(key)
            map_name = "attributes_string" if context == "attribute" else "resources_string"
            column = f"{map_name}['{_sql_literal(key)}'] AS `{context}.{key}`"
        else:
            raise ValueError(
                f"unsupported log field: {name}; use one of "
                f"{', '.join(sorted(set(_LOG_FIELD_COLUMNS)))}, attribute.<key>, or resource.<key>"
            )
        if column not in columns:
            columns.append(column)
    return ", ".join(columns)


def _settings(settings: Settings | None) -> Settings:
    return settings if settings is not None else get_settings()


def build_structured_plan(
    tool_name: str,
    arguments: Any,
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> StructuredPlan:
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be an object")
    cfg = _settings(settings)
    pruning = bool(cfg.ts_bucket_pruning)
    row_limit = max(1, int(cfg.chunk_row_limit))

    def window(default: str) -> TimeWindow:
        return resolve_window(
            arguments, default, max_window_hours=cfg.max_window_hours, now=now
        )

    if tool_name == "signoz_list_services":
        win = window("1h")
        limit = _bounded_int(arguments.get("limit"), 30)
        args = ["services", *win.sz_args(), "--limit", str(limit)]
        environment = arguments.get("environment")
        if environment:
            args.extend(["--env", str(environment)])

        def services_sql(exclusive_end: bool) -> str:
            where = [_time_filter("logs", pruning=pruning, exclusive_end=exclusive_end)]
            if environment:
                where.append(
                    f"resources_string['deployment.environment'] = '{_sql_literal(str(environment))}'"
                )
            return (
                "SELECT resources_string['k8s.deployment.name'] AS deployment, "
                "resources_string['service.name'] AS service, "
                "resources_string['deployment.environment'] AS environment, "
                "count(*) AS log_count "
                f"FROM signoz_logs.distributed_logs_v2 WHERE {' AND '.join(where)} "
                "GROUP BY deployment, service, environment ORDER BY log_count DESC "
                f"LIMIT {row_limit}"
            )

        chunk = ChunkSpec(
            build_sql=services_sql,
            key_columns=("deployment", "service", "environment"),
            merge_ops={"log_count": "sum"},
            finalize=None,
            sort_column="log_count",
            sort_desc=True,
            limit=limit,
            row_limit=row_limit,
        )
        return StructuredPlan(tool_name, args, win, chunk=chunk)

    if tool_name == "signoz_get_trace_details":
        trace_id = str(arguments.get("traceId") or "")
        if not _TRACE_ID_RE.fullmatch(trace_id):
            raise ValueError("traceId must be exactly 32 hexadecimal characters")
        win = window("6h")
        limit = _bounded_int(arguments.get("limit"), 500)
        sql = (
            "SELECT toString(timestamp) AS timestamp_utc, trace_id, span_id, parent_span_id, "
            "serviceName AS service, name AS operation, kind_string AS span_kind, "
            "duration_nano, has_error, status_code_string, status_message, "
            "http_method, http_url, response_status_code, attributes_string, resources_string "
            "FROM signoz_traces.distributed_signoz_index_v3 "
            f"WHERE {_time_filter('traces', pruning=pruning)} "
            f"AND trace_id = '{trace_id.lower()}' "
            f"ORDER BY timestamp ASC LIMIT {limit}"
        )
        return StructuredPlan(tool_name, ["query", sql, *win.sz_args()], win)

    if tool_name == "signoz_list_metrics":
        win = window("1h")
        limit = _bounded_int(arguments.get("limit"), 50)
        where = [_time_filter("metrics")]
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
        return StructuredPlan(tool_name, ["query", sql, *win.sz_args()], win)

    if tool_name == "signoz_get_field_keys":
        signal, config = _signal_config(arguments.get("signal"))
        win = window("1h")
        limit = _bounded_int(arguments.get("limit"), 100)
        attribute_map = config["attribute_map"]
        resource_map = config["resource_map"]
        time_filter = _time_filter(signal, pruning=pruning)
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
        return StructuredPlan(tool_name, ["query", sql, *win.sz_args()], win)

    if tool_name == "signoz_get_field_values":
        signal, config = _signal_config(arguments.get("signal"))
        win = window("1h")
        limit = _bounded_int(arguments.get("limit"), 50)
        expression, _ = _field_expression(
            signal,
            arguments.get("name"),
            context=str(arguments.get("fieldContext") or "").lower(),
        )
        search_text = arguments.get("searchText")

        def values_sql(exclusive_end: bool, row_cap: int) -> str:
            where = [
                _time_filter(signal, pruning=pruning, exclusive_end=exclusive_end),
                f"toString({expression}) != ''",
            ]
            if search_text not in (None, ""):
                where.append(
                    f"lower(toString({expression})) LIKE '%"
                    f"{_sql_literal(str(search_text).lower())}%'"
                )
            return (
                f"SELECT toString({expression}) AS value, count() AS count "
                f"FROM {config['table']} WHERE {' AND '.join(where)} "
                f"GROUP BY value ORDER BY count DESC LIMIT {row_cap}"
            )

        chunk = ChunkSpec(
            build_sql=lambda exclusive_end: values_sql(exclusive_end, row_limit),
            key_columns=("value",),
            merge_ops={"count": "sum"},
            finalize=None,
            sort_column="count",
            sort_desc=True,
            limit=limit,
            row_limit=row_limit,
        )
        return StructuredPlan(
            tool_name, ["query", values_sql(False, limit), *win.sz_args()], win, chunk=chunk
        )

    if tool_name == "signoz_search_logs":
        win = window("1h")
        limit = _bounded_int(arguments.get("limit"), 100)
        offset = _non_negative_int(arguments.get("offset"), "offset", 0, maximum=100000)
        columns = _log_select_columns(arguments.get("fields"))
        compact = _bool_arg(arguments.get("compact"), "compact", True)
        max_chars = _bounded_int(
            arguments.get("maxFieldChars"),
            max(200, int(cfg.log_max_field_chars)),
            minimum=200,
            maximum=100000,
            field_name="maxFieldChars",
        )
        sql = (
            f"SELECT {columns} "
            "FROM signoz_logs.distributed_logs_v2 "
            f"WHERE {' AND '.join(_log_filters(arguments, pruning=pruning))} "
            f"ORDER BY timestamp DESC LIMIT {limit} OFFSET {offset}"
        )
        postprocess = (
            partial(compact_log_rows, denylist=cfg.log_compact_denyset, max_chars=max_chars)
            if compact
            else None
        )
        return StructuredPlan(
            tool_name, ["query", sql, *win.sz_args()], win, postprocess=postprocess
        )

    if tool_name == "signoz_aggregate_logs":
        win = window("1h")
        sql = _aggregate_sql("logs", arguments, _log_filters(arguments, pruning=pruning))
        chunk, reason = _aggregate_plan_chunk(
            "logs", arguments, _log_filters, pruning=pruning, row_limit=row_limit
        )
        return StructuredPlan(
            tool_name, ["query", sql, *win.sz_args()], win,
            chunk=chunk, chunk_unavailable_reason=reason,
        )

    if tool_name == "signoz_search_traces":
        win = window("1h")
        limit = _bounded_int(arguments.get("limit"), 100)
        offset = _non_negative_int(arguments.get("offset"), "offset", 0, maximum=100000)
        sql = (
            "SELECT toString(timestamp) AS timestamp_utc, trace_id, span_id, parent_span_id, "
            "serviceName AS service, name AS operation, duration_nano, has_error, "
            "status_code_string, response_status_code, attributes_string, resources_string "
            "FROM signoz_traces.distributed_signoz_index_v3 "
            f"WHERE {' AND '.join(_trace_filters(arguments, pruning=pruning))} "
            f"ORDER BY timestamp DESC LIMIT {limit} OFFSET {offset}"
        )
        return StructuredPlan(tool_name, ["query", sql, *win.sz_args()], win)

    if tool_name == "signoz_aggregate_traces":
        win = window("1h")
        sql = _aggregate_sql("traces", arguments, _trace_filters(arguments, pruning=pruning))
        chunk, reason = _aggregate_plan_chunk(
            "traces", arguments, _trace_filters, pruning=pruning, row_limit=row_limit
        )
        return StructuredPlan(
            tool_name, ["query", sql, *win.sz_args()], win,
            chunk=chunk, chunk_unavailable_reason=reason,
        )

    if tool_name == "signoz_query_metrics":
        win = window("1h")
        sql = _metric_query_sql(arguments)
        return StructuredPlan(
            tool_name, ["query", sql, *win.sz_args()], win,
            chunk=_metric_plan_chunk(arguments, row_limit),
        )

    raise ValueError(f"unknown structured tool: {tool_name}")


def build_structured_sz_args(
    tool_name: str,
    arguments: Any,
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> list[str]:
    """Return the sz argv for the single-query form of a structured tool."""
    return build_structured_plan(tool_name, arguments, settings=settings, now=now).args
