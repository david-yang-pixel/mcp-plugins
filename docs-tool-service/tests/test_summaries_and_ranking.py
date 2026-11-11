"""Search-result summaries without Mintlify/OpenAPI noise, and the Cloud edition boost."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from docs_tool_service import tools

SETTINGS = SimpleNamespace(docs_timeout_seconds=5.0, docs_cache_ttl_seconds=900.0)
COM = "https://docs.dify.ai"
ENT = "https://enterprise-docs.dify.ai"

# Exactly as Mintlify serves them (checked against docs.dify.ai on 2026-11-11).
INDEX_NOTICE = (
    "> ## Documentation Index\n"
    "> Fetch the complete documentation index at: https://docs.dify.ai/llms.txt\n"
    "> Use this file to discover all available pages before exploring further.\n\n"
)
MINTLIFY_FOOTER = (
    "\n\nThis documentation is built and hosted on [Mintlify](https://mintlify.com), "
    "a developer documentation platform."
)
ZH_TRANSLATION_NOTICE = (
    "> 本文档由 AI 自动翻译。如有任何不准确之处，请参考 "
    "[英文原版](https://docs.dify.ai/en/cloud/use-dify/knowledge/knowledge-storage-limit.md)。\n\n"
)
YAML_NOISE = ("openapi:", "````", "operationId", "paths:", "securitySchemes", "requestBody", ">-")


def _mintlify_page(title: str, lead: str, body: str) -> str:
    return f"{INDEX_NOTICE}# {title}\n\n> {lead}\n\n{ZH_TRANSLATION_NOTICE}{body}{MINTLIFY_FOOTER}\n"


CLOUD_QUOTA_PAGE = _mintlify_page(
    "知识库存储与文档配额",
    "工作空间的知识库最多能容纳多少文档、存储多少索引数据",
    "每个工作空间都有两项知识库配额：一项限制文档数量，另一项限制索引数据量。\n\n"
    "工作空间所有者可在 **设置** > **账单** 的 **文档上传配额** 中查看用量。",
)

# A Mintlify API reference page: the whole OpenAPI document of one operation in
# a four-backtick YAML fence, including spec-wide info/servers/tags/components.
API_PAGE = f"""{INDEX_NOTICE}# 从文件创建文档

> 在知识库中通过上传文件创建文档。支持 PDF、TXT、DOCX 等常见格式。



## OpenAPI

````yaml /zh/api-reference/openapi_service.json post /datasets/{{dataset_id}}/document/create-by-file
openapi: 3.0.1
info:
  description: 用于 Dify 应用与知识库的 REST API。
  title: Dify 服务 API
  version: 1.0.0
servers:
  - description: Dify 服务 API 的基础 URL。
    url: https://{{api_base_url}}
tags:
  - description: 用于管理知识库的操作，包括创建、配置和检索。
    name: 知识库
  - description: 文字转语音和语音转文字操作。
    name: 语音与文字转换
paths:
  /datasets/{{dataset_id}}/document/create-by-file:
    post:
      tags:
        - 文档
      summary: 从文件创建文档
      description: >-
        在知识库中通过上传文件创建文档。支持 PDF、TXT、DOCX 等常见格式。索引异步进行；使用返回的 `batch` ID
        跟踪。
      operationId: createDocumentFromFile
      parameters:
        - description: >-
            知识库 ID。
          in: path
          name: dataset_id
          required: true
          schema:
            type: string
      requestBody:
        content:
          multipart/form-data:
            schema:
              $ref: '#/components/schemas/CreateByFileRequest'
      responses:
        '200':
          description: 文档已创建。
        '413':
          description: '`file_too_large`：文件超过大小限制。'
components:
  schemas:
    CreateByFileRequest:
      type: object
      properties:
        file:
          description: 要上传的文件。
          type: string
        data:
          description: 包含 `indexing_technique` 等配置的 JSON 字符串。
          type: string
  securitySchemes:
    ApiKeyAuth:
      type: http
      scheme: bearer

````{MINTLIFY_FOOTER}
"""

ZH_CLOUD_INDEX = f"""# Dify Docs: Chinese Cloud

- [知识库存储与文档配额]({COM}/zh/cloud/use-dify/knowledge/knowledge-storage-limit.md): 配额
- [从文件创建文档]({COM}/zh/api-reference/documents/create-document-by-file.md): API
"""

COMMUNITY_FULL = f"""# Create Document by File
Source: {COM}/en/api-reference/documents/create-document-by-file

/en/api-reference/openapi_service.json post /datasets/{{dataset_id}}/document/create-by-file
**Available for**: Knowledge bases.

Creates a document in a knowledge base by uploading a file.
"""


@pytest.fixture
def mock_docs(monkeypatch):
    md = "text/markdown; charset=utf-8"
    routes = {
        f"{COM}/llms-full.txt": (COMMUNITY_FULL, "text/plain; charset=utf-8"),
        f"{COM}/_llms/zh.md": (f"- [Chinese / Cloud]({COM}/_llms/zh/cloud.txt): 使用\n", md),
        f"{COM}/_llms/zh/cloud.txt": (ZH_CLOUD_INDEX, "text/plain; charset=utf-8"),
        f"{COM}/_llms/ja.md": ("# Dify Docs: Japanese\n", md),
        f"{COM}/zh/cloud/use-dify/knowledge/knowledge-storage-limit.md": (CLOUD_QUOTA_PAGE, md),
        f"{COM}/zh/api-reference/documents/create-document-by-file.md": (API_PAGE, md),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) not in routes:
            return httpx.Response(404, text="not found")
        body, content_type = routes[str(request.url)]
        return httpx.Response(200, text=body, headers={"content-type": content_type})

    monkeypatch.setattr(
        tools,
        "_http_client",
        lambda settings: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    tools._DOCS_CACHE.clear()
    tools._document_fields.cache_clear()
    yield routes
    tools._DOCS_CACHE.clear()


def _search(**arguments: Any) -> dict[str, Any]:
    payload = asyncio.run(tools.handle("official_docs_search", arguments, SETTINGS))
    assert payload["success"], payload
    return payload["data"]


# --- Item 16: summary de-noising -------------------------------------------


def test_clean_content_removes_mintlify_footer_but_keeps_prose_mentions() -> None:
    raw = (
        "Body.\n\nWe render docs with Mintlify components.\n"
        "Was this page helpful?\n"
        "Powered by Mintlify\n"
        "Users find a helpful message here."
        + MINTLIFY_FOOTER
    )

    cleaned = tools._clean_content(raw)

    assert "built and hosted on" not in cleaned
    assert "Was this page helpful" not in cleaned
    assert "Powered by Mintlify" not in cleaned
    assert "We render docs with Mintlify components." in cleaned
    assert "Users find a helpful message here." in cleaned


def test_openapi_page_is_indexed_as_readable_operation_text() -> None:
    document = tools._make_document(
        title="从文件创建文档",
        url=f"{COM}/zh/api-reference/documents/create-document-by-file",
        content=API_PAGE,
        source="official_docs_community",
    )

    assert document.summary == (
        "从文件创建文档 — 在知识库中通过上传文件创建文档。支持 PDF、TXT、DOCX 等常见格式。"
        "索引异步进行；使用返回的 `batch` ID 跟踪。"
    )
    content = document.content
    for noise in YAML_NOISE + ("Mintlify", "## OpenAPI"):
        assert noise not in content
    # Spec-wide boilerplate shared by every API page is not indexed.
    assert "语音与文字转换" not in content
    assert "Dify 服务 API 的基础 URL" not in content
    # The operation itself stays searchable, including fields behind a $ref.
    assert "POST /datasets/{dataset_id}/document/create-by-file" in content
    assert "- dataset_id: 知识库 ID。" in content
    assert "- file: 要上传的文件。" in content
    assert "- data: 包含 `indexing_technique` 等配置的 JSON 字符串。" in content
    assert "- 413: `file_too_large`：文件超过大小限制。" in content


def test_english_feed_spec_reference_line_becomes_method_and_path() -> None:
    documents = tools._parse_llms_full(COMMUNITY_FULL, source="official_docs_community")

    content = documents[0].content
    assert "openapi_service.json" not in content
    assert content.startswith("POST /datasets/{dataset_id}/document/create-by-file")
    assert documents[0].summary == ""


def test_unparsable_openapi_block_is_still_removed() -> None:
    page = (
        "# Broken\n\n````yaml /zh/api-reference/openapi_service.json get /broken\n"
        "paths: [unclosed\n````\n"
    )
    document = tools._make_document(
        title="Broken", url=f"{COM}/zh/api-reference/broken", content=page, source="x"
    )

    assert document.summary == ""
    assert "paths:" not in document.content
    assert "GET /broken" in document.content


def test_search_descriptions_have_no_mintlify_footer_or_yaml(mock_docs) -> None:
    data = _search(query="知识库 文档", source="community", language="all", limit=10)

    english = _search(query="create document file", source="community", language="en")
    results = data["results"] + english["results"]
    descriptions = {result["url"]: result["description"] for result in results}
    assert len(descriptions) == 3
    assert "openapi_service.json" not in str(descriptions)
    for description in descriptions.values():
        assert "built and hosted on Mintlify" not in description
        assert "AI 自动翻译" not in description
        for noise in YAML_NOISE:
            assert noise not in description
    assert descriptions[f"{COM}/zh/api-reference/documents/create-document-by-file"].startswith(
        "从文件创建文档 — 在知识库中通过上传文件创建文档。"
    )


def test_official_docs_read_drops_footer_but_keeps_api_yaml(mock_docs) -> None:
    payload = asyncio.run(
        tools.handle(
            "official_docs_read",
            {"url": f"{COM}/zh/api-reference/documents/create-document-by-file"},
            SETTINGS,
        )
    )

    content = payload["data"]["content"]
    assert "built and hosted on Mintlify" not in content
    assert "operationId: createDocumentFromFile" in content


# --- Item 17: Cloud edition boost ------------------------------------------

QUERY = "知识库 文档上传 大小限制"


def _ranking_corpus() -> list[tools.Document]:
    def doc(title: str, path: str, content: str) -> tools.Document:
        return tools._make_document(
            title=title, url=f"{COM}/zh/{path}", content=content, source="official_docs_community"
        )

    return [
        # Shared API reference page: matches 知识库 and 大小限制 in title/heading.
        doc(
            "Agent 知识库 API",
            "api-reference/guides/knowledge-base",
            "# Agent 知识库 API\n\n在自己的代码中查询知识库。\n\n"
            "## 请求大小限制\n\n每个请求都要低于其操作页面上标注的字节上限。",
        ),
        # Cloud page: matches 知识库 and the rarer 文档上传, but only in the body.
        doc(
            "知识库存储与文档配额",
            "cloud/use-dify/knowledge/knowledge-storage-limit",
            "# 知识库存储与文档配额\n\n每个工作空间都有两项知识库配额。\n\n"
            "可在 **文档上传配额** 中查看用量。",
        ),
        doc(
            "部署配置",
            "self-host/deploy/configuration",
            "# 部署配置\n\n`UPLOAD_FILE_SIZE_LIMIT` 控制文档上传大小限制，也影响知识库。",
        ),
        # Fillers make 知识库 a common word and 大小限制 less rare than 文档上传,
        # as in the real corpus.
        *(
            doc(f"知识库教程 {index}", f"learn/knowledge-{index}", "介绍知识库的用法。")
            for index in range(12)
        ),
        *(
            doc(f"文件大小限制 {index}", f"develop-plugin/limits-{index}", "插件的大小限制。")
            for index in range(3)
        ),
    ]


def _titles(edition: str, documents: list[tools.Document] | None = None) -> list[str]:
    pool = [
        document
        for document in documents or _ranking_corpus()
        if tools._matches_edition(document, edition)
    ]
    results = tools._search_documents(pool, query=QUERY, limit=10, edition=edition)
    return [result.title for result in results]


def test_cloud_edition_ranks_cloud_page_above_shared_api_reference() -> None:
    titles = _titles("cloud")

    assert titles[:2] == ["知识库存储与文档配额", "Agent 知识库 API"]
    assert "部署配置" not in titles


def test_without_boost_shared_api_reference_would_win(monkeypatch) -> None:
    monkeypatch.setattr(tools, "CLOUD_EDITION_BOOST", 0.0)

    assert _titles("cloud")[:2] == ["Agent 知识库 API", "知识库存储与文档配额"]


@pytest.mark.parametrize("edition", ["all", "self-hosted"])
def test_other_editions_keep_their_ordering(monkeypatch, edition: str) -> None:
    boosted = _titles(edition)
    monkeypatch.setattr(tools, "CLOUD_EDITION_BOOST", 0.0)

    assert _titles(edition) == boosted
    if edition == "all":
        # Pages matching all query words still come first; then the shared
        # API page outranks the Cloud page, exactly as before the boost.
        assert boosted[:3] == ["部署配置", "Agent 知识库 API", "知识库存储与文档配额"]


def test_cloud_boost_never_beats_a_page_matching_more_query_words() -> None:
    documents = _ranking_corpus()
    documents.append(
        tools._make_document(
            title="文件上传",
            url=f"{COM}/zh/api-reference/files/upload",
            content="知识库 文档上传 大小限制 说明。",
            source="official_docs_community",
        )
    )

    assert _titles("cloud", documents)[0] == "文件上传"


def test_cloud_boost_end_to_end(mock_docs) -> None:
    cloud = _search(query=QUERY, edition="cloud", source="community")
    assert cloud["results"][0]["title"] == "知识库存储与文档配额"
    assert cloud["results"][0]["edition"] == "cloud"
