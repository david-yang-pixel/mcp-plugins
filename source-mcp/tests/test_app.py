from __future__ import annotations

import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from source_mcp.runtime import runtime_ref_candidates


def _run(*args: str, cwd: Path) -> None:
    subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)


def _make_repo(base: Path, name: str = "dify") -> Path:
    repo = base / name
    repo.mkdir(parents=True)
    _run("git", "init", cwd=repo)
    _run("git", "config", "user.email", "test@example.com", cwd=repo)
    _run("git", "config", "user.name", "Test User", cwd=repo)
    (repo / "api").mkdir()
    (repo / "api" / "service.py").write_text(
        "def answer_icon():\n    return 'runtime source'\n",
        encoding="utf-8",
    )
    _run("git", "add", ".", cwd=repo)
    _run("git", "commit", "-m", "initial", cwd=repo)
    return repo


def _client(monkeypatch, tmp_path: Path) -> TestClient:
    _make_repo(tmp_path)
    monkeypatch.setenv("SOURCE_MCP_API_KEY", "x" * 32)
    monkeypatch.setenv("SOURCE_MCP_REPOS_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("SOURCE_MCP_REPOS", "dify")
    monkeypatch.setenv("SOURCE_MCP_RUNTIME_REF_OVERRIDES", '{"dify":"HEAD"}')

    from source_mcp.settings import get_settings

    get_settings.cache_clear()
    from source_mcp.app import create_app

    return TestClient(create_app())


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer " + "x" * 32}


def test_mcp_rejects_missing_bearer_token(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)

    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )

    assert response.status_code == 401


def test_mcp_initialize_and_tools_list(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)

    init_response = client.post(
        "/mcp",
        headers=_auth(),
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    )
    tools_response = client.post(
        "/mcp",
        headers=_auth(),
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    )

    assert init_response.status_code == 200
    assert init_response.json()["result"]["capabilities"]["tools"] == {}
    tool_names = {tool["name"] for tool in tools_response.json()["result"]["tools"]}
    assert {
        "source_runtime_ref",
        "source_search",
        "source_read",
        "repo_status",
        "refresh_repos",
        "list_repo_tags",
        "source_log",
        "source_diff",
    } <= tool_names


def test_health_exposes_process_pressure(monkeypatch, tmp_path):
    from source_mcp import app as app_module

    monkeypatch.setattr(
        app_module,
        "process_health_snapshot",
        lambda **_kwargs: {
            "zombie_count": 12,
            "pids_current": 3900,
            "pids_max": 4330,
            "pids_usage_ratio": 0.9007,
            "degraded": True,
            "degraded_reasons": ["zombie_count", "pids_usage_ratio"],
        },
    )
    client = _client(monkeypatch, tmp_path)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert response.json()["full_repo_search_concurrency"] == 1
    assert response.json()["process"]["zombie_count"] == 12
    assert response.json()["process"]["pids_current"] == 3900


def test_source_runtime_ref_uses_override(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)

    response = client.post(
        "/mcp",
        headers=_auth(),
        json={
            "jsonrpc": "2.0",
            "id": "runtime",
            "method": "tools/call",
            "params": {
                "name": "source_runtime_ref",
                "arguments": {"repo": "dify"},
            },
        },
    )

    payload = response.json()["result"]["structuredContent"]
    assert payload["success"] is True
    assert payload["data"]["repo"] == "dify"
    assert payload["data"]["ref"] == "HEAD"
    assert payload["data"]["source"] == "override"
    assert payload["data"]["commit"]


def test_source_search_defaults_to_runtime_ref(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)

    response = client.post(
        "/mcp",
        headers=_auth(),
        json={
            "jsonrpc": "2.0",
            "id": "search",
            "method": "tools/call",
            "params": {
                "name": "source_search",
                "arguments": {"repo": "dify", "pattern": "answer_icon"},
            },
        },
    )

    payload = response.json()["result"]["structuredContent"]
    assert payload["success"] is True
    assert payload["data"]["repo"] == "dify"
    assert payload["data"]["ref"] == "HEAD"
    assert "api/service.py" in payload["data"]["output"]
    assert "answer_icon" in payload["data"]["output"]


def test_source_search_retries_invalid_regex_as_fixed_string(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)

    response = client.post(
        "/mcp",
        headers=_auth(),
        json={
            "jsonrpc": "2.0",
            "id": "search-invalid-regex",
            "method": "tools/call",
            "params": {
                "name": "source_search",
                "arguments": {
                    "repo": "dify",
                    "pattern": r"subscription/info\?tenant_id=\{tenant_id\}",
                },
            },
        },
    )

    payload = response.json()["result"]["structuredContent"]
    assert payload["success"] is True
    assert payload["data"]["repo"] == "dify"
    assert "GIT_GREP_FAILED" not in str(payload)


def test_source_read_rejects_unresolved_runtime(monkeypatch, tmp_path):
    _make_repo(tmp_path)
    monkeypatch.setenv("SOURCE_MCP_API_KEY", "x" * 32)
    monkeypatch.setenv("SOURCE_MCP_REPOS_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("SOURCE_MCP_REPOS", "dify")
    monkeypatch.delenv("SOURCE_MCP_RUNTIME_REF_OVERRIDES", raising=False)

    from source_mcp.settings import get_settings

    get_settings.cache_clear()
    from source_mcp.app import create_app

    client = TestClient(create_app())
    response = client.post(
        "/mcp",
        headers=_auth(),
        json={
            "jsonrpc": "2.0",
            "id": "read",
            "method": "tools/call",
            "params": {
                "name": "source_read",
                "arguments": {
                    "repo": "dify",
                    "filepath": "api/service.py",
                    "ref": "runtime",
                },
            },
        },
    )

    payload = response.json()["result"]["structuredContent"]
    assert payload["success"] is False
    assert payload["code"] == "RUNTIME_TELEMETRY_FAILED"


def test_runtime_ref_candidates_include_semver_variant_base():
    assert runtime_ref_candidates("0.6.1-serverless") == ["0.6.1", "0.6.1-serverless"]
    assert runtime_ref_candidates("hotfix-1.14.2-fix.11") == [
        "hotfix/1.14.2-fix.11",
        "hotfix-1.14.2-fix.11",
    ]
