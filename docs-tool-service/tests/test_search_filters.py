from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from docs_tool_service import tools

SETTINGS = SimpleNamespace(docs_timeout_seconds=5.0, docs_cache_ttl_seconds=900.0)
ENT = "https://enterprise-docs.dify.ai"
COM = "https://docs.dify.ai"

DOC_INDEX_NOTICE = (
    "> ## Documentation Index\n"
    "> Fetch the complete documentation index at: https://enterprise-docs.dify.ai/llms.txt\n"
    "> Use this file to discover all available pages before exploring further.\n\n"
)


def _page(title: str, body: str) -> str:
    return f"{DOC_INDEX_NOTICE}# {title}\n\n{body}\n"


ENTERPRISE_FULL = f"""# Upgrade Guide
Source: {ENT}/en/3.13.x/deploy/upgrade

How to upgrade Dify Enterprise 3.13 with Helm.

# Environment Variables
Source: {ENT}/en/3.13.x/deploy/environment-variables

WORKFLOW_MAX_EXECUTION_TIME controls the workflow timeout.
"""

ENTERPRISE_LLMS_TXT = f"""# Dify Enterprise Docs

- [3.13.x (2 pages)]({ENT}/_llms/en/3-13-x.txt): Documentation for 3.13.x.
- [3.12.x (1 pages)]({ENT}/_llms/en/3-12-x.txt): Documentation for 3.12.x.
- [3.5.x (2 pages)]({ENT}/_llms/en/3-5-x.txt): Documentation for 3.5.x.
- [2.5.x (1 pages)]({ENT}/_llms/en/2-5-x.txt): Documentation for 2.5.x.
"""


def _routes() -> dict[str, tuple[int, str, str]]:
    md = "text/markdown; charset=utf-8"
    txt = "text/plain; charset=utf-8"
    return {
        f"{ENT}/llms-full.txt": (200, ENTERPRISE_FULL, txt),
        f"{ENT}/llms.txt": (200, ENTERPRISE_LLMS_TXT, txt),
        f"{ENT}/_llms/zh/3-12-x.md": (
            200,
            f"- [升级指南]({ENT}/zh/3.12.x/deploy/upgrade.md)\n",
            md,
        ),
        f"{ENT}/_llms/ja/3-12-x.md": (
            200,
            f"- [アップグレード]({ENT}/ja/3.12.x/deploy/upgrade.md)\n",
            md,
        ),
        f"{ENT}/zh/3.12.x/deploy/upgrade.md": (200, _page("升级指南", "升级 Dify 企业版。"), md),
        f"{ENT}/ja/3.12.x/deploy/upgrade.md": (
            200,
            _page("アップグレード", "Upgrade Dify Enterprise の手順。upgrade"),
            md,
        ),
        f"{ENT}/_llms/en/3-5-x.txt": (
            200,
            f"- [Dev]({ENT}/_llms/en/3-5-x/developer-resources.txt): nested\n"
            f"- [Upgrade Guide]({ENT}/en/3.5.x/deploy/upgrade.md)\n",
            txt,
        ),
        f"{ENT}/_llms/en/3-5-x/developer-resources.txt": (
            200,
            f"- [Admin API]({ENT}/en/3.5.x/develop/admin-api.md)\n",
            txt,
        ),
        f"{ENT}/en/3.5.x/deploy/upgrade.md": (
            200,
            _page("Upgrade Guide", "Upgrade Dify Enterprise 3.5 with Helm."),
            md,
        ),
        f"{ENT}/en/3.5.x/develop/admin-api.md": (
            200,
            _page("Admin API", "Upgrade automation through the admin API."),
            md,
        ),
        f"{COM}/llms-full.txt": (
            200,
            f"""# Cloud Billing
Source: {COM}/en/cloud/management/billing

Upgrade your Dify Cloud plan here.

# Upgrade Self-hosted Dify
Source: {COM}/en/self-host/deploy/upgrade

Upgrade a self-hosted Docker Compose deployment.

# Workflow API
Source: {COM}/en/api-reference/workflow

Upgrade notes for the API.

# Model Credits
Source: {COM}/en/learn/model-credits

Upgrade options. This feature is only available on Dify Cloud.
""",
            txt,
        ),
        f"{COM}/_llms/zh.md": (200, "# Dify Docs: Chinese\n", md),
        f"{COM}/_llms/ja.md": (200, "# Dify Docs: Japanese\n", md),
    }


@pytest.fixture
def mock_http(monkeypatch):
    routes = _routes()
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        requested.append(url)
        if url not in routes:
            return httpx.Response(404, text="not found")
        status, body, content_type = routes[url]
        return httpx.Response(status, text=body, headers={"content-type": content_type})

    def client(settings: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)

    monkeypatch.setattr(tools, "_http_client", client)
    tools._DOCS_CACHE.clear()
    tools._VERSION_CORPUS_CACHE.clear()
    monkeypatch.setattr(tools, "_ENTERPRISE_VERSIONS_CACHE", None)
    tools._document_fields.cache_clear()
    yield SimpleNamespace(routes=routes, requested=requested)
    tools._DOCS_CACHE.clear()
    tools._VERSION_CORPUS_CACHE.clear()


def _search(**arguments: Any) -> dict[str, Any]:
    payload = asyncio.run(tools.handle("official_docs_search", arguments, SETTINGS))
    assert payload["success"], payload
    return payload["data"]


# --- 1. version filter -----------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("3.5", "3.5.x"), ("3.5.x", "3.5.x"), ("v3.5.2", "3.5.x"), ("3-5-x", "3.5.x"), ("", "")],
)
def test_version_argument_is_normalized_to_family(raw: str, expected: str) -> None:
    assert tools._normalize_version(raw) == expected


def test_invalid_version_argument_is_rejected(mock_http) -> None:
    payload = asyncio.run(
        tools.handle("official_docs_search", {"query": "x", "version": "three"}, SETTINGS)
    )
    assert payload["success"] is False
    assert payload["code"] == "BAD_ARGUMENT"


def test_query_version_detection_requires_unambiguous_form_outside_enterprise() -> None:
    assert tools._detect_query_version("upgrade 3.5.x helm", enterprise_only=False) == (
        "3.5.x",
        "upgrade helm",
    )
    assert tools._detect_query_version("python 3.12 sandbox", enterprise_only=False) == (
        "",
        "python 3.12 sandbox",
    )
    assert tools._detect_query_version("python 3.12 sandbox", enterprise_only=True)[0] == "3.12.x"
    assert tools._detect_query_version("升级 3.5.x版本", enterprise_only=False)[0] == "3.5.x"


def test_explicit_version_loads_that_version_index_and_filters_results(mock_http) -> None:
    data = _search(query="upgrade", source="enterprise", version="3.5")

    assert data["filters"]["version"] == "3.5.x"
    assert data["filters"]["version_origin"] == "explicit"
    assert data["results"], data
    assert {result["version"] for result in data["results"]} == {"3.5.x"}
    assert {result["url"] for result in data["results"]} == {
        f"{ENT}/en/3.5.x/deploy/upgrade",
        f"{ENT}/en/3.5.x/develop/admin-api",
    }
    assert data["available_versions"] == ["3.13.x", "3.12.x", "3.5.x", "2.5.x"]
    assert f"{ENT}/_llms/en/3-5-x.txt" in mock_http.requested


def test_version_in_query_is_detected_and_removed_from_ranking(mock_http) -> None:
    data = _search(query="upgrade 3.5.x", source="auto")

    assert data["filters"]["version"] == "3.5.x"
    assert data["filters"]["version_origin"] == "query"
    assert data["filters"]["query_without_version"] == "upgrade"
    assert {result["version"] for result in data["results"]} == {"3.5.x"}
    assert {result["source"] for result in data["results"]} == {"official_docs_enterprise"}
    assert any("unversioned" in notice for notice in data["notices"])


def test_version_already_in_default_corpus_needs_no_extra_fetch(mock_http) -> None:
    data = _search(query="workflow timeout", source="enterprise", version="3.13.x")

    assert [result["version"] for result in data["results"]] == ["3.13.x"]
    assert f"{ENT}/_llms/en/3-13-x.txt" not in mock_http.requested


def test_unpublished_version_returns_empty_results_with_reason(mock_http) -> None:
    data = _search(query="upgrade", source="enterprise", version="2.1")

    assert data["results"] == []
    assert data["available_versions"] == ["3.13.x", "3.12.x", "3.5.x", "2.5.x"]
    assert any("2.1.x is not published" in notice for notice in data["notices"])


def test_version_missing_for_language_is_reported(mock_http) -> None:
    data = _search(query="upgrade", source="enterprise", version="2.5", language="ja")

    assert data["results"] == []
    assert any("not published in language ja" in notice for notice in data["notices"])


def test_version_filter_on_community_docs_explains_they_are_unversioned(mock_http) -> None:
    data = _search(query="upgrade", source="community", version="3.5")

    assert data["results"] == []
    assert any("unversioned" in notice for notice in data["notices"])


# --- 2. language filter ----------------------------------------------------


def test_language_auto_follows_query_script(mock_http) -> None:
    english = _search(query="upgrade", source="enterprise")
    assert english["filters"]["language"] == "en"
    assert {result["language"] for result in english["results"]} == {"en"}

    chinese = _search(query="升级", source="enterprise")
    assert chinese["filters"]["language"] == "zh"
    assert {result["language"] for result in chinese["results"]} == {"zh"}


def test_language_explicit_and_all(mock_http) -> None:
    japanese = _search(query="upgrade", source="enterprise", language="ja")
    assert {result["language"] for result in japanese["results"]} == {"ja"}

    everything = _search(query="upgrade", source="enterprise", language="all", limit=20)
    assert {result["language"] for result in everything["results"]} == {"en", "zh", "ja"}


def test_language_auto_falls_back_to_all_languages_when_empty(mock_http) -> None:
    data = _search(query="アップグレード", source="enterprise", language="auto")
    assert data["results"]
    assert data["filters"]["language"] == "ja"

    data = _search(query="手順", source="enterprise")
    assert data["filters"]["language"] == "all"
    assert data["results"][0]["language"] == "ja"
    assert any("all languages" in notice for notice in data["notices"])


def test_invalid_language_is_rejected(mock_http) -> None:
    payload = asyncio.run(
        tools.handle("official_docs_search", {"query": "x", "language": "fr"}, SETTINGS)
    )
    assert payload["code"] == "BAD_ARGUMENT"


# --- 3. relevance ----------------------------------------------------------


def _doc(title: str, path: str, content: str) -> tools.Document:
    return tools._make_document(
        title=title, url=f"{COM}/en/{path}", content=content, source="official_docs_community"
    )


def test_ranking_prefers_pages_matching_all_terms_and_rare_terms() -> None:
    documents = [
        _doc(f"Workflow Tutorial {index}", f"cloud/workflow-{index}", "workflow " * 30)
        for index in range(6)
    ]
    documents.append(_doc("Workflow API", "api-reference/workflow", "Run a workflow via API."))
    documents.append(
        _doc(
            "Environment Variables",
            "self-host/deploy/environments",
            "## Workflow\n\nWORKFLOW_MAX_EXECUTION_TIME sets the workflow timeout in seconds.",
        )
    )
    documents.append(_doc("Timeout Errors", "learn/timeout-errors", "Requests can time out."))

    results = tools._search_documents(documents, query="workflow timeout", limit=5)

    assert results[0].title == "Environment Variables"
    assert results[0].matched_terms == ("workflow", "timeout")
    # A single rare term outranks pages that only repeat the common term.
    assert results[1].title == "Timeout Errors"
    assert "timeout" in results[0].description.casefold()


def test_ranking_weights_title_and_heading_over_body() -> None:
    documents = [
        _doc("Overview", "learn/overview", "Mentions retry policy somewhere in passing text."),
        _doc("Retry Policy", "learn/retry-policy", "Configure how nodes retry on errors policy."),
        _doc("Nodes", "learn/nodes", "## Retry policy\n\nDetails here."),
    ]

    results = tools._search_documents(documents, query="retry policy", limit=3)

    assert [result.title for result in results] == ["Retry Policy", "Nodes", "Overview"]


def test_stopwords_do_not_count_as_matches() -> None:
    documents = [
        _doc("How to use the app", "learn/how-to", "How to use the app."),
        _doc("Timeout", "learn/timeout", "Timeout settings."),
    ]

    results = tools._search_documents(documents, query="how to set a timeout", limit=5)

    assert [result.title for result in results][0] == "Timeout"


# --- 4. edition filter -----------------------------------------------------


def test_document_edition_is_classified_from_url_and_content() -> None:
    assert tools._classify_document(f"{COM}/en/cloud/x", "")[2] == "cloud"
    assert tools._classify_document(f"{COM}/zh/self-host/x", "")[2] == "self-hosted"
    assert tools._classify_document(f"{COM}/en/api-reference/x", "")[2] == "shared"
    assert (
        tools._classify_document(f"{COM}/en/learn/x", "Only available on Dify Cloud.")[2]
        == "cloud"
    )
    assert tools._classify_document(f"{ENT}/en/3.5.x/deploy/x", "") == (
        "en",
        "3.5.x",
        "enterprise",
    )


def test_edition_filter_on_community_docs(mock_http) -> None:
    cloud = _search(query="upgrade", source="community", edition="cloud", limit=20)
    assert {result["title"] for result in cloud["results"]} == {
        "Cloud Billing",
        "Workflow API",
        "Model Credits",
    }
    assert {result["edition"] for result in cloud["results"]} == {"cloud", "shared"}

    self_hosted = _search(query="upgrade", source="auto", edition="self-hosted", limit=20)
    titles = {result["title"] for result in self_hosted["results"]}
    assert "Upgrade Self-hosted Dify" in titles
    assert "Cloud Billing" not in titles
    assert "Model Credits" not in titles
    assert any(result["edition"] == "enterprise" for result in self_hosted["results"])

    everything = _search(query="upgrade", source="community", limit=20)
    assert len(everything["results"]) == 4
    assert everything["filters"]["edition"] == "all"


def test_cloud_edition_excludes_enterprise_docs(mock_http) -> None:
    data = _search(query="upgrade", source="auto", edition="cloud", limit=20)
    assert all(result["source"] == "official_docs_community" for result in data["results"])


def test_invalid_edition_is_rejected(mock_http) -> None:
    payload = asyncio.run(
        tools.handle("official_docs_search", {"query": "x", "edition": "saas"}, SETTINGS)
    )
    assert payload["code"] == "BAD_ARGUMENT"


# --- 5. titles -------------------------------------------------------------


def test_title_extraction_order() -> None:
    assert tools._extract_title("---\ntitle: 'From Frontmatter'\n---\n# H1\n") == "From Frontmatter"
    assert tools._extract_title("<html><title>Page &amp; Title</title><h1>H</h1>") == (
        "Page & Title"
    )
    assert (
        tools._extract_title(DOC_INDEX_NOTICE + "```bash\n# not a title\n```\n# Real Title\n")
        == "Real Title"
    )
    assert tools._extract_title("<div><h1 class='x'>HTML <b>Heading</b></h1></div>") == (
        "HTML Heading"
    )
    assert tools._extract_title("no title here") == ""


def test_official_docs_read_returns_title_and_clean_markdown(mock_http) -> None:
    mock_http.routes[f"{ENT}/en/3.5.x/deploy/sso.md"] = (
        200,
        _page(
            "SSO Setup",
            "Intro text.\n\n![Diagram](https://assets-docs.dify.ai/2025/02/a.png)\n\n"
            '<img src="https://mintcdn.com/x/y.webp?w=1" alt="x" />\n\n'
            "See https://assets-docs.dify.ai/2025/02/b.jpg for the screenshot.\n\n"
            "[Guide](https://enterprise-docs.dify.ai/en/3.5.x/deploy/upgrade)",
        ),
        "text/markdown; charset=utf-8",
    )

    payload = asyncio.run(
        tools.handle("official_docs_read", {"url": f"{ENT}/en/3.5.x/deploy/sso"}, SETTINGS)
    )

    data = payload["data"]
    assert data["title"] == "SSO Setup"
    assert data["version"] == "3.5.x"
    assert data["language"] == "en"
    assert data["edition"] == "enterprise"
    content = data["content"]
    assert content.startswith("# SSO Setup")
    assert "Documentation Index" not in content
    assert "llms.txt" not in content
    for noise in (".png", ".webp", ".jpg", "<img", "!["):
        assert noise not in content
    assert "[Guide](https://enterprise-docs.dify.ai/en/3.5.x/deploy/upgrade)" in content


def test_official_docs_read_html_falls_back_to_h1(mock_http) -> None:
    mock_http.routes[f"{COM}/en/cloud/page"] = (
        200,
        "<html><body><main><h1>Heading Title</h1><p>Body</p></main></body></html>",
        "text/html; charset=utf-8",
    )

    payload = asyncio.run(
        tools.handle("official_docs_read", {"url": f"{COM}/en/cloud/page"}, SETTINGS)
    )

    assert payload["data"]["title"] == "Heading Title"
    assert payload["data"]["edition"] == "cloud"


# --- 6. content cleaning ---------------------------------------------------


def test_clean_content_removes_index_notice_and_images() -> None:
    raw = (
        DOC_INDEX_NOTICE
        + "## Documentation Index\n"
        + "Fetch the complete documentation index at: https://docs.dify.ai/llms.txt\n"
        + "Use this file to discover all available pages before exploring further.\n\n"
        + "Body start.\n\n"
        + "![Alt](https://mintcdn.com/a/b.png?fit=max\\&auto=format)\n"
        + "[![Badge](https://img.example/badge.svg)](https://docs.dify.ai/en/x)\n"
        + "[screenshot](https://assets-docs.dify.ai/2025/02/c.jpeg)\n"
        + "[logo]: https://assets-docs.dify.ai/logo.png\n"
        + '<Frame>\n  <img src="https://mintcdn.com/z.gif" />\n</Frame>\n'
        + "Bare https://assets-docs.dify.ai/2025/02/d.png here.\n"
        + "Keep [link](https://docs.dify.ai/en/y) and https://example.com/file.yaml\n"
    )

    cleaned = tools._clean_content(raw)

    assert cleaned.startswith("Body start.")
    assert "Documentation Index" not in cleaned
    assert "Fetch the complete documentation index" not in cleaned
    for noise in (".png", ".svg", ".jpeg", ".gif", "<img", "![", "<Frame"):
        assert noise not in cleaned
    assert "screenshot" in cleaned
    assert "Bare  here." in cleaned or "Bare here." in cleaned
    assert "[link](https://docs.dify.ai/en/y)" in cleaned
    assert "https://example.com/file.yaml" in cleaned


def test_search_snippets_come_from_cleaned_content(mock_http) -> None:
    data = _search(query="admin api", source="enterprise", version="3.5")

    assert data["results"][0]["title"] == "Admin API"
    description = data["results"][0]["description"]
    assert "Documentation Index" not in description
    assert "llms.txt" not in description


def test_search_tool_schema_documents_new_parameters() -> None:
    schema = next(tool for tool in tools.TOOLS if tool["name"] == "official_docs_search")
    properties = schema["inputSchema"]["properties"]
    assert properties["language"]["default"] == "auto"
    assert properties["version"]["default"] == ""
    assert properties["edition"]["default"] == "all"
    for name in ("language", "version", "edition"):
        assert properties[name]["description"]
