from __future__ import annotations

import json

import anyio
import pytest

from source_mcp import app, runtime
from source_mcp.git_ops import SourceError
from source_mcp.runtime import RuntimeRefUnavailable, _select_primary_image_tag
from source_mcp.settings import Settings


def test_select_primary_image_tag_prefers_latest_runtime_signal():
    rows = [
        {"image_tag": "old-high-volume", "latest_ts": 100, "log_count": 10_000},
        {"image_tag": "new-low-volume", "latest_ts": 200, "log_count": 1},
    ]

    selected = _select_primary_image_tag(rows)

    assert selected == "new-low-volume"


def test_default_runtime_deployments_match_production_aliases(monkeypatch):
    monkeypatch.delenv("SOURCE_MCP_RUNTIME_DEPLOYMENTS", raising=False)

    deployments = Settings().runtime_deployments

    assert deployments == {
        "dify": "dify-api-oltp",
        "dify-plugin-daemon": "dify-plugindaemon,dify-plugin-daemon",
        "dify-billing": "dify-saas-billing,billing",
    }


def test_resolve_runtime_ref_tries_configured_deployment_aliases(monkeypatch, tmp_path):
    queried_deployments: list[str] = []
    monkeypatch.setenv("SOURCE_MCP_REPOS_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("SOURCE_MCP_REPOS", "dify-billing")
    monkeypatch.setenv(
        "SOURCE_MCP_RUNTIME_DEPLOYMENTS",
        json.dumps({"dify-billing": "billing,dify-saas-billing"}),
    )
    settings = Settings()

    async def query_runtime_image_tag(deployment: str, _settings: Settings) -> str | None:
        queried_deployments.append(deployment)
        return "release-20260708.1" if deployment == "dify-saas-billing" else None

    async def rev_parse(
        repo: str,
        ref: str,
        _settings: Settings,
    ) -> str:
        assert repo == "dify-billing"
        assert ref == "release-20260708.1"
        return "a" * 40

    monkeypatch.setattr(runtime, "_query_runtime_image_tag", query_runtime_image_tag)

    async def resolve() -> dict[str, object]:
        return await runtime.resolve_runtime_ref(
            repo="dify-billing",
            deployment=None,
            settings=settings,
            rev_parse_func=rev_parse,
        )

    result = anyio.run(resolve)

    assert queried_deployments == ["billing", "dify-saas-billing"]
    assert result["deployment"] == "dify-saas-billing"


def test_resolve_runtime_ref_marks_unpublished_git_ref(monkeypatch, tmp_path):
    monkeypatch.setenv("SOURCE_MCP_REPOS_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("SOURCE_MCP_REPOS", "dify-billing")
    monkeypatch.setenv(
        "SOURCE_MCP_RUNTIME_DEPLOYMENTS",
        '{"dify-billing":"dify-saas-billing"}',
    )
    settings = Settings()

    async def query_runtime_image_tag(_deployment: str, _settings: Settings) -> str:
        return "release-20260708.1"

    async def rev_parse(
        _repo: str,
        ref: str,
        _settings: Settings,
    ) -> str:
        raise SourceError("REF_NOT_FOUND", f"missing {ref}")

    monkeypatch.setattr(runtime, "_query_runtime_image_tag", query_runtime_image_tag)

    async def resolve() -> None:
        await runtime.resolve_runtime_ref(
            repo="dify-billing",
            deployment=None,
            settings=settings,
            rev_parse_func=rev_parse,
        )

    with pytest.raises(SourceError) as exc_info:
        anyio.run(resolve)

    assert exc_info.value.code == "GIT_REF_UNPUBLISHED"


def test_resolve_runtime_ref_preserves_git_transport_errors(monkeypatch, tmp_path):
    monkeypatch.setenv("SOURCE_MCP_REPOS_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("SOURCE_MCP_REPOS", "dify-billing")
    monkeypatch.setenv(
        "SOURCE_MCP_RUNTIME_DEPLOYMENTS",
        '{"dify-billing":"dify-saas-billing"}',
    )
    settings = Settings()

    async def query_runtime_image_tag(_deployment: str, _settings: Settings) -> str:
        return "release-20260708.1"

    async def rev_parse(
        _repo: str,
        _ref: str,
        _settings: Settings,
    ) -> str:
        raise SourceError("GIT_CLONE_FAILED", "repository transport failed")

    monkeypatch.setattr(runtime, "_query_runtime_image_tag", query_runtime_image_tag)

    async def resolve() -> None:
        await runtime.resolve_runtime_ref(
            repo="dify-billing",
            deployment=None,
            settings=settings,
            rev_parse_func=rev_parse,
        )

    with pytest.raises(SourceError) as exc_info:
        anyio.run(resolve)

    assert exc_info.value.code == "GIT_CLONE_FAILED"


def test_runtime_telemetry_transport_failure_is_not_an_empty_alias(monkeypatch):
    monkeypatch.setenv("SOURCE_MCP_SIGNOZ_MCP_URL", "https://signoz.invalid/mcp")
    settings = Settings()

    def post_json(*_args: object) -> tuple[int, str]:
        return 599, ""

    monkeypatch.setattr(runtime, "_post_json", post_json)

    async def query() -> None:
        await runtime._query_runtime_image_tag("dify-saas-billing", settings)

    with pytest.raises(SourceError) as exc_info:
        anyio.run(query)

    assert exc_info.value.code == "RUNTIME_TELEMETRY_FAILED"


def test_runtime_sql_rejects_non_kubernetes_deployment_names():
    with pytest.raises(RuntimeRefUnavailable):
        runtime._runtime_sql(r"billing\' OR 1=1 --", "production")


def test_tool_call_preserves_runtime_error_code(monkeypatch):
    settings = Settings()

    async def resolve_runtime_ref(**_kwargs: object) -> dict[str, object]:
        raise SourceError("GIT_REF_UNPUBLISHED", "runtime tag is not published")

    monkeypatch.setattr(app, "resolve_runtime_ref", resolve_runtime_ref)

    async def call_tool() -> dict[str, object]:
        return await app._handle_tool_call(
            name="source_runtime_ref",
            arguments={"repo": "dify-billing"},
            settings=settings,
        )

    payload = anyio.run(call_tool)

    assert payload["code"] == "GIT_REF_UNPUBLISHED"
