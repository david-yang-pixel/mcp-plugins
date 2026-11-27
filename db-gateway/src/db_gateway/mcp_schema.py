"""Shared MCP tool schema for DB Gateway."""
from __future__ import annotations

import copy
from typing import Any


TOOL_SCHEMA: dict[str, Any] = {
    "name": "db_gateway_query",
    # Read-only SQL only. Clients such as OpenConnector derive read/write from these hints.
    "annotations": {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
    "description": (
        "Run a read-only SELECT/WITH query through the DB Gateway. "
        "Databases: prod, billing, plugin, knowledgefs (KnowledgeFS production: knowledge-base "
        "documents, parsing/compilation, source workflows, retrieval answer traces, deletions). "
        "The gateway enforces read-only SQL "
        "(single statement; may start with '(' e.g. '(SELECT 1) UNION ALL (SELECT 2)'). "
        "Results are paginated: at most `limit` rows (<= server max_rows, default 1000) "
        "starting at `offset`. The response includes row_count, total_rows (null if the "
        "bounded count timed out), has_more, next_offset (pass it as `offset` for the next "
        "page), warnings and timing_ms. Add ORDER BY (ideally on a unique key) when paging, "
        "otherwise page order is not stable. format=csv (default) returns `csv` text where "
        "NULL and empty string both appear as an empty field; format=json returns `columns`, "
        "`column_types` and `rows` as an array of arrays of strings, with SQL NULL as null. "
        "explain=true returns the query plan (EXPLAIN without ANALYZE: the query is planned, "
        "not executed) as rows of the `QUERY PLAN` column; use it to check index usage before "
        "running a slow query. A query that times out returns the indexes of the tables it "
        "reads. Use db_gateway_schema to look up real column names and types."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "database": {
                "type": "string",
                "enum": ["prod", "billing", "plugin", "knowledgefs"],
                "description": (
                    "prod=langgenius, billing=billing, plugin=dify_plugin_daemon, "
                    "knowledgefs=KnowledgeFS production (knowledge-fs-production-readonly, db knowledgefs)"
                ),
            },
            "sql": {
                "type": "string",
                "description": (
                    "Single read-only SELECT/WITH query (optionally wrapped in parentheses). "
                    "Do not add LIMIT/OFFSET for paging; use the offset/limit arguments."
                ),
            },
            "offset": {
                "type": "integer",
                "minimum": 0,
                "default": 0,
                "description": "Rows to skip before this page (use next_offset from the previous page).",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "description": "Page size; defaults to and is capped at the server max_rows (default 1000).",
            },
            "format": {
                "type": "string",
                "enum": ["csv", "json"],
                "default": "csv",
                "description": (
                    "csv: `csv` text (header + rows). json: `columns`, `column_types`, and "
                    "`rows` as array of arrays (string values, SQL NULL as null)."
                ),
            },
            "explain": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Return the query plan instead of rows. The gateway prepends EXPLAIN "
                    "itself (no ANALYZE, the query is never executed); do not write EXPLAIN "
                    "in `sql`."
                ),
            },
        },
        "required": ["database", "sql"],
    },
}


SCHEMA_TOOL_SCHEMA: dict[str, Any] = {
    "name": "db_gateway_schema",
    "annotations": {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
    "description": (
        "Look up the real columns (name, type, nullable, default) and indexes of one or more "
        "tables through the DB Gateway, from information_schema.columns and pg_indexes. "
        "Databases: prod, billing, plugin, knowledgefs. Use it before writing SQL to avoid 'column does "
        "not exist' errors and type mismatches (e.g. uuid = text), and to pick filters that "
        "hit an index. Read-only."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "database": {
                "type": "string",
                "enum": ["prod", "billing", "plugin", "knowledgefs"],
                "description": (
                    "prod=langgenius, billing=billing, plugin=dify_plugin_daemon, "
                    "knowledgefs=KnowledgeFS production (knowledge-fs-production-readonly, db knowledgefs)"
                ),
            },
            "tables": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 20,
                "description": (
                    "Table names, e.g. [\"workflow_runs\"]; `schema.table` is accepted. "
                    "Unqualified names match every non-system schema."
                ),
            },
        },
        "required": ["database", "tables"],
    },
}

TOOLS: list[dict[str, Any]] = [TOOL_SCHEMA, SCHEMA_TOOL_SCHEMA]


_READ_ONLY_ANNOTATIONS: dict[str, Any] = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": True,
}

LAMBDA_LOGS_TOOL_SCHEMA: dict[str, Any] = {
    "name": "plugin_lambda_logs",
    "annotations": dict(_READ_ONLY_ANNOTATIONS),
    "description": (
        "Read CloudWatch logs of a Dify SaaS plugin Lambda (us-east-1, log group "
        "/aws/lambda/<function_name>) through the gateway's Teleport session. Locate the "
        "function with exactly one of: `function_name` (full name including any --2 suffix), or "
        "`tenant_id` + `plugin_id` (workspace UUID + publisher/name; the gateway looks up the "
        "current installation, so check the version at the time of the incident if the plugin "
        "was upgraded or reinstalled since). Window: exactly one of `since_minutes` or "
        "`start_time` + `end_time` (end exclusive; ISO 8601 or millisecond timestamps; times "
        "without a zone are Asia/Shanghai; second-precision numbers are rejected). Messages are "
        "never truncated. When `complete` is false, call again with the returned `next_token`, "
        "start_time=window.start_ms, end_time=window.end_ms and the same other arguments "
        "(since_minutes cannot be combined with next_token). Many or long events use a lot of context: lower `limit` "
        "or add `filter_pattern` (CloudWatch syntax). REPORT lines are invocation stats only; "
        "for errors filter on '\"ERROR\"' or '\"Traceback\"', or on '\"<RequestId>\"' when known. "
        "One function is shared by every workspace using that plugin version. To list all "
        "versions and checksums of a plugin, run with db_gateway_query (database=plugin): "
        "SELECT r.plugin_unique_identifier, r.function_name, r.updated_at FROM "
        "public.serverless_runtimes AS r WHERE split_part(r.plugin_unique_identifier, '@', 1) = "
        "'langgenius/openai:1.2.1' ORDER BY r.updated_at DESC. Per workspace: SELECT "
        "i.plugin_unique_identifier, i.runtime_type, r.function_name FROM "
        "public.plugin_installations AS i LEFT JOIN public.serverless_runtimes AS r ON "
        "r.plugin_unique_identifier = i.plugin_unique_identifier WHERE i.tenant_id = "
        "'<uuid>'::uuid AND i.plugin_id = 'langgenius/openai'."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "function_name": {
                "type": "string",
                "description": "Lambda function name, e.g. 9a442b2d0d9b43e56a190df6ca7dc207 (keep any --N suffix).",
            },
            "tenant_id": {"type": "string", "description": "Workspace (tenant) UUID; use with plugin_id."},
            "plugin_id": {"type": "string", "description": "publisher/name, e.g. langgenius/openai; use with tenant_id."},
            "start_time": {
                "type": ["string", "number"],
                "description": "Window start (inclusive): ISO 8601 or epoch milliseconds; no zone = Asia/Shanghai.",
            },
            "end_time": {
                "type": ["string", "number"],
                "description": "Window end (exclusive): ISO 8601 or epoch milliseconds; no zone = Asia/Shanghai.",
            },
            "since_minutes": {"type": "integer", "minimum": 1, "description": "Last N minutes up to now (no upper bound)."},
            "filter_pattern": {
                "type": "string",
                "description": "CloudWatch filter pattern, passed through, e.g. '\"ERROR\"'.",
            },
            "log_stream_names": {"type": "array", "items": {"type": "string"}, "description": "Only these log streams."},
            "limit": {"type": "integer", "minimum": 1, "description": "Max events for this call (default 1000, max 10000)."},
            "next_token": {
                "type": "string",
                "description": "next_token from the previous call; pass window.start_ms/end_ms as start_time/end_time with it.",
            },
        },
        "required": [],
    },
}

KUBE_READ_TOOL_SCHEMA: dict[str, Any] = {
    "name": "kube_read",
    "annotations": dict(_READ_ONLY_ANNOTATIONS),
    "description": (
        "Run a read-only kubectl command on a Kubernetes cluster through the gateway's Teleport "
        "session. `args` are kubectl arguments without 'kubectl'; the first must be one of get, "
        "describe, logs, top, explain, api-resources, api-versions, version, auth can-i. The "
        "gateway adds --context itself; do not pass --context/--kubeconfig/--as/--server, "
        "-f/--filename, -k, --follow/-f, --watch/-w. The current Teleport role can read pods, "
        "pods/log and deployments; other resources are refused by Teleport (code KUBECTL_ERROR "
        "with the original message; tsh exits 0, errors are detected from stderr). Production "
        "SaaS runs in the `prod` cluster, namespace dify-plugin; staging in the `staging` "
        "cluster, namespace dify-staging. Deployments are labelled "
        "workload.user.cattle.io/workloadselector=apps.deployment-<namespace>-<deployment>. "
        "Output is capped at 1 MB (truncated=true); limit logs with --tail, --since, "
        "--since-time. Examples: [\"get\",\"deploy\",\"-n\",\"dify-plugin\",\"-o\",\"wide\"] "
        "(images and replicas); [\"get\",\"pods\",\"-n\",\"dify-plugin\",\"-l\",\"<label>\"]; "
        "[\"describe\",\"pod\",\"<name>\",\"-n\",\"dify-plugin\"]; "
        "[\"logs\",\"deploy/dify-api-oltp\",\"-n\",\"dify-plugin\",\"--since=10m\",\"--tail=500\"]."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "cluster": {"type": "string", "enum": ["prod", "staging"], "description": "Cluster alias."},
            "args": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "description": "kubectl arguments, e.g. [\"get\", \"pods\", \"-n\", \"dify-plugin\"].",
            },
        },
        "required": ["cluster", "args"],
    },
}


def _tool_enabled(section: Any) -> bool:
    # `is True`: a MagicMock config (tests) must not switch the tools on.
    return getattr(section, "enabled", False) is True


def tools_for(cfg: Any) -> list[dict[str, Any]]:
    """TOOLS plus the Teleport tools enabled in ``cfg.teleport_tools``."""
    tools = list(TOOLS)
    teleport_tools = getattr(cfg, "teleport_tools", None)
    if _tool_enabled(getattr(teleport_tools, "lambda_logs", None)):
        tools.append(LAMBDA_LOGS_TOOL_SCHEMA)
    kube = getattr(teleport_tools, "kube", None)
    if _tool_enabled(kube):
        schema = copy.deepcopy(KUBE_READ_TOOL_SCHEMA)
        clusters = dict(kube.clusters)
        prop = schema["inputSchema"]["properties"]["cluster"]
        prop["enum"] = list(clusters)
        prop["description"] = "Cluster alias: " + ", ".join(f"{k}={v}" for k, v in clusters.items())
        tools.append(schema)
    return tools


def tool_enabled(cfg: Any, name: str) -> bool:
    return any(t["name"] == name for t in tools_for(cfg))
