from __future__ import annotations

import json
from typing import Any

import anyio
import pytest

from source_mcp import runtime
from source_mcp.git_ops import SourceError
from source_mcp.runtime import RuntimeRefUnavailable
from source_mcp.settings import Settings


def _rev_parse_ok(_repo: str, _ref: str, _settings: Settings) -> Any:
    async def inner() -> str:
        return "b" * 40

    return inner()


def test_explicit_deployment_falls_back_to_any_environment(monkeypatch):
    monkeypatch.delenv("SOURCE_MCP_RUNTIME_REF_OVERRIDES", raising=False)
    queried: list[tuple[str, str | None]] = []

    async def query(deployment: str, _settings: Settings, environment: str | None = None):
        queried.append((deployment, environment))
        if environment == "any":
            return {"image_tag": "1.14.2-fix.5", "environment": "gray"}
        return None

    monkeypatch.setattr(runtime, "_query_runtime_image", query)

    async def resolve() -> dict[str, Any]:
        return await runtime.resolve_runtime_ref(
            repo="dify",
            deployment="dify-api-oltp-gray",
            settings=Settings(_env_file=None),
            rev_parse_func=_rev_parse_ok,
        )

    result = anyio.run(resolve)

    assert queried == [("dify-api-oltp-gray", "production"), ("dify-api-oltp-gray", "any")]
    assert result["environment"] == "gray"
    assert result["environment_fallback"] is True
    assert "gray" in result["environment_warning"]
    assert result["deployment"] == "dify-api-oltp-gray"


def test_explicit_environment_is_not_widened(monkeypatch):
    monkeypatch.delenv("SOURCE_MCP_RUNTIME_REF_OVERRIDES", raising=False)
    queried: list[str | None] = []

    async def query(_deployment: str, _settings: Settings, environment: str | None = None):
        queried.append(environment)
        return None

    monkeypatch.setattr(runtime, "_query_runtime_image", query)

    async def resolve() -> None:
        await runtime.resolve_runtime_ref(
            repo="dify",
            deployment="dify-api-oltp",
            environment="staging",
            settings=Settings(_env_file=None),
            rev_parse_func=_rev_parse_ok,
        )

    with pytest.raises(RuntimeRefUnavailable) as exc_info:
        anyio.run(resolve)

    assert queried == ["staging"]
    assert "environment staging" in exc_info.value.message


def test_configured_deployments_do_not_widen_environment(monkeypatch):
    monkeypatch.delenv("SOURCE_MCP_RUNTIME_REF_OVERRIDES", raising=False)
    queried: list[tuple[str, str | None]] = []

    async def query(deployment: str, _settings: Settings, environment: str | None = None):
        queried.append((deployment, environment))
        return None

    monkeypatch.setattr(runtime, "_query_runtime_image", query)

    async def resolve() -> None:
        await runtime.resolve_runtime_ref(
            repo="dify-marketplace",
            deployment=None,
            settings=Settings(_env_file=None),
            rev_parse_func=_rev_parse_ok,
        )

    with pytest.raises(RuntimeRefUnavailable):
        anyio.run(resolve)

    assert queried == [
        ("dify-marketplace-backend", "production"),
        ("dify-marketplace-ops-backend", "production"),
    ]


def test_runtime_sql_environment_filter():
    assert "deployment.environment'] = 'gray'" in runtime._runtime_sql("dify-web", "gray")
    any_sql = runtime._runtime_sql("dify-web", "any")
    assert "deployment.environment'] =" not in any_sql
    assert "GROUP BY image_tag, environment" in any_sql
    with pytest.raises(RuntimeRefUnavailable):
        runtime._runtime_sql("dify-web", "prod' OR 1=1 --")


def _signoz_response(rows: list[dict[str, Any]]) -> str:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"content": [{"type": "text", "text": json.dumps({"success": True, "data": rows})}]},
        }
    )


def test_list_runtime_deployments_reports_rows_and_mappings(monkeypatch):
    monkeypatch.setenv("SOURCE_MCP_SIGNOZ_MCP_URL", "http://signoz.invalid")
    monkeypatch.delenv("SOURCE_MCP_RUNTIME_DEPLOYMENTS", raising=False)
    monkeypatch.delenv("SOURCE_MCP_RUNTIME_PATH_DEPLOYMENTS", raising=False)
    sent: list[dict[str, Any]] = []
    rows = [
        {
            "deployment": "dify-api-oltp-gray",
            "environment": "gray",
            "image_tag": "1.14.2-fix.5",
            "log_count": "12",
            "latest_ts": "1793577600000000000",
        },
        {
            "deployment": "dify-web",
            "environment": "production",
            "image_tag": "1.14.2-fix.3",
            "log_count": 7,
            "latest_ts": 1793577600000000000,
        },
        {"deployment": "dify-extra", "environment": "production", "image_tag": "x", "log_count": 1},
    ]

    def post_json(endpoint: str, payload: dict[str, Any], _headers: Any, _timeout: float):
        assert endpoint == "http://signoz.invalid/mcp"
        sent.append(payload)
        return 200, _signoz_response(rows)

    monkeypatch.setattr(runtime, "_post_json", post_json)

    async def call() -> dict[str, Any]:
        return await runtime.list_runtime_deployments(
            settings=Settings(_env_file=None), name_contains="Dify", limit=2
        )

    data = anyio.run(call)

    args = sent[0]["params"]["arguments"]["args"]
    sql = args[1]
    assert args[2:] == ["--hours", "3"]
    assert "LIKE '%dify%'" in sql
    assert "deployment.environment'] =" not in sql
    assert "LIMIT 3" in sql
    assert data["truncated"] is True
    assert data["returned"] == 2
    gray = data["deployments"][0]
    assert gray["environment"] == "gray"
    assert gray["log_count"] == 12
    assert gray["latest_at"].startswith("2026-")
    assert gray["repos"] == []
    assert data["deployments"][1]["repos"] == ["dify"]
    assert data["deployment_repos"]["dify-api-oltp"] == ["dify"]
    assert data["configured_repo_deployments"]["dify-sandbox"] == ["dify-sandbox"]
    assert "dify-api-oltp" in data["configured_without_logs"]


def test_list_runtime_deployments_filters_environment(monkeypatch):
    monkeypatch.setenv("SOURCE_MCP_SIGNOZ_MCP_URL", "http://signoz.invalid")
    sent: list[str] = []

    def post_json(_endpoint: str, payload: dict[str, Any], _headers: Any, _timeout: float):
        sent.append(payload["params"]["arguments"]["args"][1])
        return 200, _signoz_response([])

    monkeypatch.setattr(runtime, "_post_json", post_json)

    async def call() -> dict[str, Any]:
        return await runtime.list_runtime_deployments(
            settings=Settings(_env_file=None), environment="gray", hours=24
        )

    data = anyio.run(call)

    assert "deployment.environment'] = 'gray'" in sent[0]
    assert data["hours"] == 24
    assert data["deployments"] == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"name_contains": "x' OR 1=1"},
        {"name_contains": "dify%"},
        {"environment": "prod'"},
        {"hours": 0},
        {"hours": 1000},
        {"limit": 0},
    ],
)
def test_list_runtime_deployments_validates_inputs(monkeypatch, kwargs):
    monkeypatch.setenv("SOURCE_MCP_SIGNOZ_MCP_URL", "http://signoz.invalid")

    def post_json(*_args: Any):
        raise AssertionError("must not query SigNoz with invalid input")

    monkeypatch.setattr(runtime, "_post_json", post_json)

    async def call() -> None:
        await runtime.list_runtime_deployments(settings=Settings(_env_file=None), **kwargs)

    with pytest.raises(SourceError) as exc_info:
        anyio.run(call)

    assert exc_info.value.code == "BAD_ARGUMENT"


def test_list_runtime_deployments_tool_is_exposed(monkeypatch):
    from source_mcp import app

    async def fake(**kwargs: Any) -> dict[str, Any]:
        return {"called_with": kwargs["environment"], "hours": kwargs["hours"]}

    monkeypatch.setattr(app, "list_runtime_deployments", fake)

    async def call() -> dict[str, Any]:
        return await app._handle_tool_call(
            name="list_runtime_deployments",
            arguments={"environment": "gray", "hours": 6},
            settings=Settings(_env_file=None),
        )

    payload = anyio.run(call)

    assert payload == {"success": True, "data": {"called_with": "gray", "hours": 6}}
    names = {tool["name"] for tool in app._tools_list()}
    assert {"list_runtime_deployments", "source_tree"} <= names
    search = next(tool for tool in app._tools_list() if tool["name"] == "source_search")
    assert {"deployment", "environment", "offset", "limit"} <= set(search["inputSchema"]["properties"])
