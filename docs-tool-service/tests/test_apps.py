from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}








def test_docs_mcp_rejects_github_tool(monkeypatch) -> None:
    monkeypatch.setenv("DOCS_TOOL_SERVICE_API_KEY", "docs-secret")
    from docs_tool_service.settings import get_settings

    get_settings.cache_clear()
    from docs_tool_service.app import create_service_app

    client = TestClient(create_service_app("docs-tool-service"))
    response = client.post(
        "/mcp",
        headers=_auth("docs-secret"),
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "github_search_prs", "arguments": {}},
        },
    )

    assert response.status_code == 200
    assert response.json()["error"]["message"] == "unknown tool: github_search_prs"


def test_docs_mcp_exposes_docs_and_official_site_tools(monkeypatch) -> None:
    monkeypatch.setenv("DOCS_TOOL_SERVICE_API_KEY", "docs-secret")
    from docs_tool_service.settings import get_settings

    get_settings.cache_clear()
    from docs_tool_service.app import create_service_app

    client = TestClient(create_service_app("docs-tool-service"))
    response = client.post(
        "/mcp",
        headers=_auth("docs-secret"),
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )

    assert response.status_code == 200
    assert {tool["name"] for tool in response.json()["result"]["tools"]} == {
        "official_docs_search",
        "official_docs_read",
        "official_site_list",
        "official_site_search",
        "official_site_read",
    }


def test_docs_full_text_search_matches_body_and_preserves_source() -> None:
    from docs_tool_service.tools import _parse_llms_full, _search_documents

    documents = _parse_llms_full(
        """# Publish as MCP Server
Source: https://docs.dify.ai/zh/cloud/use-dify/publish/publish-mcp

这里介绍如何将 Dify 应用发布为 MCP 服务。正文独有词：远程工具调用。

# Other page
Source: https://docs.dify.ai/en/other

Unrelated body.
""",
        source="official_docs_community",
    )

    results = _search_documents(documents, query="远程工具调用", limit=5)

    assert len(results) == 1
    assert results[0].title == "Publish as MCP Server"
    assert results[0].source == "official_docs_community"
    assert "远程工具调用" in results[0].description


def test_docs_index_parser_keeps_page_and_nested_index_links() -> None:
    from docs_tool_service.tools import _parse_llms_index

    references = _parse_llms_index(
        """- [Cloud](https://docs.dify.ai/_llms/zh/cloud.md): index
- [发布 MCP](https://docs.dify.ai/zh/cloud/use-dify/publish/publish-mcp.md): page
- [External](https://example.com/not-allowed.md): external
"""
    )

    assert [reference.url for reference in references] == [
        "https://docs.dify.ai/_llms/zh/cloud.md",
        "https://docs.dify.ai/zh/cloud/use-dify/publish/publish-mcp.md",
    ]


def test_official_content_url_validation_blocks_ssrf_targets() -> None:
    from docs_tool_service.tools import DOCS_HOSTS, SITE_HOSTS, _validate_url

    for url in (
        "http://docs.dify.ai/en/cloud",
        "https://127.0.0.1/internal",
        "https://docs.dify.ai:8443/en/cloud",
        "https://docs.dify.ai@example.com/en/cloud",
    ):
        try:
            _validate_url(url, DOCS_HOSTS)
        except ValueError:
            pass
        else:
            raise AssertionError(f"URL should have been rejected: {url}")

    _validate_url("https://docs.dify.ai/zh/cloud", DOCS_HOSTS)
    _validate_url("https://dify.ai/zh", SITE_HOSTS)


def test_redirect_target_is_validated_before_following() -> None:
    from docs_tool_service.tools import DOCS_HOSTS, _fetch_text_with_client

    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
        ) as client:
            await _fetch_text_with_client(
                client,
                "https://docs.dify.ai/start",
                allowed_hosts=DOCS_HOSTS,
                max_bytes=1024,
            )

    try:
        asyncio.run(run())
    except ValueError as exc:
        assert "HTTPS" in str(exc)
    else:
        raise AssertionError("unsafe redirect target should have been rejected")
    assert requests == ["https://docs.dify.ai/start"]


def test_site_filters_cover_chinese_pages_and_pagination_inputs() -> None:
    from docs_tool_service.tools import _filter_site_urls

    urls = [
        "https://dify.ai/",
        "https://dify.ai/pricing",
        "https://dify.ai/zh",
        "https://dify.ai/zh/pricing",
        "https://dify.ai/ja/pricing",
    ]

    assert _filter_site_urls(urls, language="zh", section="") == [
        "https://dify.ai/zh",
        "https://dify.ai/zh/pricing",
    ]
    assert _filter_site_urls(urls, language="zh", section="pricing") == [
        "https://dify.ai/zh/pricing"
    ]


def test_html_reader_removes_navigation_scripts_and_keeps_page_content() -> None:
    from docs_tool_service.tools import _strip_html

    title, content = _strip_html(
        """<html><head><title>Dify 中文官网</title><script>secret()</script></head>
<body><header>Navigation</header><main><h1>构建 AI 应用</h1><p>官网正文</p></main>
<footer>Footer</footer></body></html>"""
    )

    assert title == "Dify 中文官网"
    assert "构建 AI 应用" in content
    assert "官网正文" in content
    assert "Navigation" not in content
    assert "secret" not in content
