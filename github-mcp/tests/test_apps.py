from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def test_github_mcp_exposes_only_github_tools(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_MCP_API_KEY", "github-secret")
    from github_mcp.settings import get_settings

    get_settings.cache_clear()
    from github_mcp.app import create_service_app

    client = TestClient(create_service_app("github-mcp"))
    response = client.post(
        "/mcp",
        headers=_auth("github-secret"),
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )

    assert response.status_code == 200
    tool_names = {tool["name"] for tool in response.json()["result"]["tools"]}
    assert tool_names == {
        "github_search_prs",
        "github_get_pr",
        "github_search_issues",
        "github_get_issue",
        "github_code_search",
    }


def test_search_issues_uses_issue_qualifier() -> None:
    from github_mcp.tools import handle

    settings = SimpleNamespace(github_token="", github_timeout_seconds=10)
    with patch("github_mcp.tools._request", new_callable=AsyncMock) as request:
        request.return_value = {"items": []}
        result = asyncio.run(
            handle(
                "github_search_issues",
                {"repo": "langgenius/dify", "query": "workflow timeout", "limit": 4},
                settings,
            )
        )

    assert result["success"] is True
    assert request.await_args.args[1] == "/search/issues"
    assert request.await_args.kwargs["params"] == {
        "q": "repo:langgenius/dify is:issue workflow timeout",
        "per_page": "4",
    }


def test_get_issue_uses_issue_endpoint() -> None:
    from github_mcp.tools import handle

    settings = SimpleNamespace(github_token="", github_timeout_seconds=10)
    with patch("github_mcp.tools._request", new_callable=AsyncMock) as request:
        request.return_value = {"number": 123, "title": "Regression"}
        result = asyncio.run(
            handle(
                "github_get_issue",
                {"repo": "langgenius/dify", "issue_number": 123},
                settings,
            )
        )

    assert result["success"] is True
    assert request.await_args.args[1] == "/repos/langgenius/dify/issues/123"
