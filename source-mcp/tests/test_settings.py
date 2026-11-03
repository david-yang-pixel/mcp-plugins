from __future__ import annotations

import pytest
from pydantic import ValidationError

from source_mcp.settings import Settings


def test_full_repo_search_concurrency_defaults_to_one(monkeypatch):
    monkeypatch.delenv("SOURCE_MCP_FULL_REPO_SEARCH_CONCURRENCY", raising=False)

    assert Settings(_env_file=None).full_repo_search_concurrency == 1


@pytest.mark.parametrize("configured_concurrency", ["0", "5"])
def test_full_repo_search_concurrency_rejects_unsafe_values(
    monkeypatch,
    configured_concurrency,
):
    monkeypatch.setenv(
        "SOURCE_MCP_FULL_REPO_SEARCH_CONCURRENCY",
        configured_concurrency,
    )

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_runtime_path_deployments_default_maps_dify_web(monkeypatch):
    monkeypatch.delenv("SOURCE_MCP_RUNTIME_PATH_DEPLOYMENTS", raising=False)

    assert Settings(_env_file=None).runtime_path_deployments == {"dify": {"web/": "dify-web"}}


def test_runtime_path_deployments_ignores_invalid_entries(monkeypatch):
    monkeypatch.setenv(
        "SOURCE_MCP_RUNTIME_PATH_DEPLOYMENTS",
        '{"dify": {"/web/": "dify-web", "": "x", "api/": ""}, "bad": "x"}',
    )

    assert Settings(_env_file=None).runtime_path_deployments == {"dify": {"web/": "dify-web"}}


def test_background_refresh_defaults(monkeypatch):
    monkeypatch.delenv("SOURCE_MCP_BACKGROUND_REFRESH_SECONDS", raising=False)
    monkeypatch.delenv("SOURCE_MCP_FULL_HISTORY_REPOS", raising=False)
    settings = Settings(_env_file=None)

    assert settings.background_refresh_seconds == 3600
    assert settings.full_history_repos == "*"


def test_default_repo_hints_cover_known_modules(monkeypatch):
    monkeypatch.delenv("SOURCE_MCP_REPO_HINTS", raising=False)
    hints = Settings(_env_file=None).repo_hints

    assert {"billing", "ops", "ops-backend", "ops-web", "gateway", "sandbox-gateway"} <= set(hints)
    assert "rbac" not in hints
    assert "dify-saas-billing" in hints["billing"]
    assert "langgenius/dify-saas" in hints["gateway"]


def test_deploy_runtime_config_matches_new_settings():
    import json
    from pathlib import Path

    config = json.loads(
        (Path(__file__).resolve().parents[1] / "deploy" / "runtime.json").read_text()
    )["environment"]
    repos = config["SOURCE_MCP_REPOS"].split(",")
    for repo in ("dify-billing", "rbac", "dify-marketplace", "dify-marketplace-backend"):
        assert repo in repos
    deployments = json.loads(config["SOURCE_MCP_RUNTIME_DEPLOYMENTS"])
    assert deployments["dify"] == "dify-api-oltp"
    assert deployments["dify-sandbox"] == "dify-sandbox"
    assert "dify-marketplace-backend" not in deployments
    assert deployments["dify-billing"] == "dify-billing,billing"
    assert deployments["rbac"] == "dify-rbac,rbac"
    assert deployments["dify-marketplace"] == "dify-marketplace-backend,dify-marketplace-ops-backend"
    assert json.loads(config["SOURCE_MCP_RUNTIME_PATH_DEPLOYMENTS"]) == {"dify": {"web/": "dify-web"}}
    assert config["SOURCE_MCP_FULL_HISTORY_REPOS"] == "*"
    assert config["SOURCE_MCP_BACKGROUND_REFRESH_SECONDS"] == "3600"
