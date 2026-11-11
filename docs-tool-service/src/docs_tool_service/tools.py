from __future__ import annotations

import asyncio
import html as html_lib
import math
import re
import time
import xml.etree.ElementTree as ET
from collections import OrderedDict
from dataclasses import dataclass
from functools import lru_cache
from html.parser import HTMLParser
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse

import httpx
import yaml

from .mcp import failure, optional_int, optional_str, required_str, success
from .settings import Settings

DOCS_FULL_URLS = {
    "official_docs_community": "https://docs.dify.ai/llms-full.txt",
    "official_docs_enterprise": "https://enterprise-docs.dify.ai/llms-full.txt",
}

# The official full feeds currently contain English pages only. These official
# indexes add the current Chinese and Japanese corpus without relying on a
# third-party search engine or Zendesk Help Center.
DOCS_TRANSLATED_INDEXES = {
    "official_docs_community": (
        "https://docs.dify.ai/_llms/zh.md",
        "https://docs.dify.ai/_llms/ja.md",
    ),
    "official_docs_enterprise": (
        "https://enterprise-docs.dify.ai/_llms/zh/3-12-x.md",
        "https://enterprise-docs.dify.ai/_llms/ja/3-12-x.md",
    ),
}

COMMUNITY_SOURCE = "official_docs_community"
ENTERPRISE_SOURCE = "official_docs_enterprise"

# Enterprise docs publish one llms index per version family and language, e.g.
# https://enterprise-docs.dify.ai/_llms/en/3-5-x.txt. The root llms.txt lists the
# published version families. Page URLs carry the family as a path segment
# (/en/3.5.x/...), which is what search results are filtered on.
ENTERPRISE_VERSIONS_INDEX_URL = "https://enterprise-docs.dify.ai/llms.txt"
ENTERPRISE_VERSION_INDEX_TEMPLATE = "https://enterprise-docs.dify.ai/_llms/{language}/{slug}.txt"
MAX_VERSION_CORPORA = 8

DOC_LANGUAGES = ("en", "zh", "ja")
DOC_EDITIONS = ("all", "cloud", "self-hosted")

# With edition=cloud, shared pages (API reference, plugin development, learn, ...)
# stay in the results but Cloud-specific pages are preferred. A Cloud page scores
# as if every query word it matches also hit one more field of this weight
# (compare title 12, heading 8, URL path 6, body 2-5), scaled by the word's
# rarity like any other field hit. Ranking still puts pages matching more query
# words first, so the boost only reorders pages with the same number of matched
# words. 0 disables it; edition=all and edition=self-hosted are unaffected.
CLOUD_EDITION_BOOST = 5.0

SUMMARY_CHARS = 420

SITE_SITEMAP_INDEX_URL = "https://dify.ai/sitemap-index.xml"
DOCS_HOSTS = {"enterprise-docs.dify.ai", "docs.dify.ai"}
SITE_HOSTS = {"dify.ai", "www.dify.ai"}

DEFAULT_READ_CHARS = 18_000
MAX_READ_CHARS = 50_000
MAX_DOWNLOAD_BYTES = 12_000_000
MAX_REDIRECTS = 5
PAGE_FETCH_CONCURRENCY = 20

TOOLS = [
    {
        "name": "official_docs_search",
        "description": (
            "Full-text search of the latest Dify official documentation corpus in English, "
            "Chinese, and Japanese. Uses Dify official feeds only, never Zendesk Help Center. "
            "Results rank pages matching all query words first, then by matched-word count, "
            "with title/path/heading hits and rare words weighted higher. Each result reports "
            "its language, version family (Enterprise docs only), and edition."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "source": {
                    "type": "string",
                    "default": "auto",
                    "description": (
                        "auto, official_docs_community, or official_docs_enterprise"
                    ),
                },
                "language": {
                    "type": "string",
                    "default": "auto",
                    "description": (
                        "auto, all, en, zh, or ja. auto infers the language from the query "
                        "(kana: ja, Han characters: zh, otherwise en) and falls back to all "
                        "languages when nothing matches"
                    ),
                },
                "version": {
                    "type": "string",
                    "default": "",
                    "description": (
                        "Enterprise docs version family such as 3.5, 3.5.x, or v3.5.2, or "
                        "latest. Empty means no version filter; a version written as 3.5.x "
                        "or v3.5 in the query is detected automatically. Community docs are "
                        "unversioned and are excluded while a version filter is active"
                    ),
                },
                "edition": {
                    "type": "string",
                    "default": "all",
                    "description": (
                        "all, cloud, or self-hosted. Based on the community docs URL "
                        "section (/cloud/ or /self-host/); shared pages such as API "
                        "reference, plugin development, and learn are kept for both, but "
                        "with cloud, Cloud-specific pages rank above shared pages that "
                        "match the same number of query words. Enterprise docs count as "
                        "self-hosted"
                    ),
                },
                "limit": {
                    "type": "integer",
                    "default": 5,
                    "minimum": 1,
                    "maximum": 20,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "official_docs_read",
        "description": (
            "Read a Dify official documentation page. Use offset and max_chars to read "
            "long pages completely without permanent truncation. Returns the page title, "
            "language, version family, and edition; image links and the llms "
            "Documentation Index notice are removed from the content."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "offset": {"type": "integer", "default": 0, "minimum": 0},
                "max_chars": {
                    "type": "integer",
                    "default": DEFAULT_READ_CHARS,
                    "minimum": 1,
                    "maximum": MAX_READ_CHARS,
                },
            },
            "required": ["url"],
        },
    },
    {
        "name": "official_site_list",
        "description": (
            "List every public dify.ai page from the official sitemap, with cursor pagination."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "cursor": {"type": "integer", "default": 0, "minimum": 0},
                "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 100},
                "language": {
                    "type": "string",
                    "default": "auto",
                    "description": "auto, en, zh, ja, or ko",
                },
                "section": {
                    "type": "string",
                    "default": "",
                    "description": "Optional path prefix such as blog, pricing, or legal",
                },
            },
        },
    },
    {
        "name": "official_site_search",
        "description": (
            "Find public dify.ai pages across the complete official sitemap by URL/title. "
            "Use official_site_read to inspect the complete current page content."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "language": {
                    "type": "string",
                    "default": "auto",
                    "description": "auto, en, zh, ja, or ko",
                },
                "limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50},
            },
            "required": ["query"],
        },
    },
    {
        "name": "official_site_read",
        "description": (
            "Read any HTTPS page on the official dify.ai website. Use offset and max_chars "
            "to read long pages completely."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "offset": {"type": "integer", "default": 0, "minimum": 0},
                "max_chars": {
                    "type": "integer",
                    "default": DEFAULT_READ_CHARS,
                    "minimum": 1,
                    "maximum": MAX_READ_CHARS,
                },
            },
            "required": ["url"],
        },
    },
]


@dataclass(frozen=True)
class Document:
    title: str
    url: str
    content: str
    source: str
    language: str = ""
    version: str = ""
    edition: str = ""
    # Fixed search-result description, e.g. an API reference page's OpenAPI
    # operation summary and description. Empty means a query-centred snippet.
    summary: str = ""


@dataclass(frozen=True)
class PageReference:
    title: str
    url: str
    description: str


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    description: str
    source: str
    language: str = ""
    version: str | None = None
    edition: str = ""
    matched_terms: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "url": self.url,
            "description": self.description,
            "source": self.source,
            "language": self.language,
            "version": self.version,
            "edition": self.edition,
            "matched_terms": list(self.matched_terms),
        }


_DOCS_CACHE: dict[str, tuple[float, float, list[Document]]] = {}
_SITEMAP_CACHE: tuple[float, float, list[str]] | None = None
_ENTERPRISE_VERSIONS_CACHE: tuple[float, list[str]] | None = None
# (version family, language) -> (expires_at, documents or None when not published)
_VERSION_CORPUS_CACHE: "OrderedDict[tuple[str, str], tuple[float, list[Document] | None]]" = (
    OrderedDict()
)


async def handle(name: str, arguments: dict[str, Any], settings: Settings) -> dict[str, Any]:
    try:
        if name == "official_docs_search":
            return success(await _docs_search(arguments, settings))

        if name == "official_docs_read":
            url = required_str(arguments, "url")
            _validate_url(url, DOCS_HOSTS)
            return success(await _read_content(url, arguments, settings, prefer_markdown=True))

        if name == "official_site_list":
            cursor = optional_int(arguments, "cursor", 0, minimum=0, maximum=2_147_483_647)
            limit = optional_int(arguments, "limit", 50, minimum=1, maximum=100)
            language = optional_str(arguments, "language", "auto") or "auto"
            section = optional_str(arguments, "section", "")
            urls, refreshed_at = await _load_site_urls(settings)
            filtered = _filter_site_urls(urls, language=language, section=section)
            page = filtered[cursor : cursor + limit]
            next_cursor = cursor + len(page)
            if next_cursor >= len(filtered):
                next_cursor = None
            return success(
                {
                    "urls": page,
                    "total": len(filtered),
                    "cursor": cursor,
                    "next_cursor": next_cursor,
                    "refreshed_at_unix": refreshed_at,
                }
            )

        if name == "official_site_search":
            query = required_str(arguments, "query")
            language = optional_str(arguments, "language", "auto") or "auto"
            limit = optional_int(arguments, "limit", 10, minimum=1, maximum=50)
            urls, refreshed_at = await _load_site_urls(settings)
            filtered = _filter_site_urls(urls, language=language, section="")
            return success(
                {
                    "results": _search_site_urls(filtered, query=query, limit=limit),
                    "indexed_pages": len(filtered),
                    "refreshed_at_unix": refreshed_at,
                }
            )

        if name == "official_site_read":
            url = required_str(arguments, "url")
            _validate_url(url, SITE_HOSTS)
            return success(await _read_content(url, arguments, settings, prefer_markdown=False))

        return failure("UNKNOWN_TOOL", f"unknown tool: {name}")
    except ValueError as exc:
        return failure("BAD_ARGUMENT", str(exc))
    except httpx.HTTPStatusError as exc:
        return failure("OFFICIAL_CONTENT_HTTP_ERROR", f"HTTP {exc.response.status_code}")
    except httpx.HTTPError as exc:
        return failure("OFFICIAL_CONTENT_NETWORK_ERROR", str(exc))
    except Exception as exc:  # noqa: BLE001
        return failure("INTERNAL_ERROR", str(exc))


async def _docs_search(arguments: dict[str, Any], settings: Settings) -> dict[str, Any]:
    query = required_str(arguments, "query")
    source = optional_str(arguments, "source", "auto") or "auto"
    limit = optional_int(arguments, "limit", 5, minimum=1, maximum=20)
    language_arg = _normalize_language_arg(optional_str(arguments, "language", "auto"))
    edition = _normalize_edition(optional_str(arguments, "edition", "all"))
    version = _normalize_version(optional_str(arguments, "version", ""))
    sources = _resolve_sources(source)

    search_query = query
    version_origin = "explicit" if version else None
    if not version and ENTERPRISE_SOURCE in sources:
        detected, remaining = _detect_query_version(
            query, enterprise_only=sources == [ENTERPRISE_SOURCE]
        )
        if detected and remaining.strip():
            version, search_query, version_origin = detected, remaining.strip(), "query"

    if language_arg == "auto":
        languages = [_infer_query_language(search_query)]
    elif language_arg == "all":
        languages = list(DOC_LANGUAGES)
    else:
        languages = [language_arg]

    documents: list[Document] = []
    refreshed_at: list[float] = []
    for item in sources:
        source_documents, source_refreshed_at = await _load_docs_corpus(item, settings)
        documents.extend(source_documents)
        refreshed_at.append(source_refreshed_at)
    indexed_pages = len(documents)

    notices: list[str] = []
    available_versions: list[str] | None = None
    if version:
        if ENTERPRISE_SOURCE not in sources:
            documents = []
            notices.append(
                "official_docs_community is unversioned (it only publishes the latest "
                "docs), so a version filter matches nothing there. Use "
                "source=official_docs_enterprise for versioned docs or omit version."
            )
        else:
            available_versions = await _load_enterprise_versions(settings, documents)
            if version == "latest":
                version = available_versions[0] if available_versions else ""
            if version not in available_versions:
                notices.append(
                    f"Enterprise docs version {version or 'latest'} is not published. "
                    f"Available versions: {', '.join(available_versions) or 'none'}."
                )
                documents = []
            else:
                documents = await _enterprise_version_documents(
                    version, languages, documents, settings, notices
                )
                if COMMUNITY_SOURCE in sources:
                    notices.append(
                        "official_docs_community is unversioned and was excluded because a "
                        "version filter is active."
                    )

    edition_pool = [document for document in documents if _matches_edition(document, edition)]
    pool = [document for document in edition_pool if document.language in languages]
    results = _search_documents(pool, query=search_query, limit=limit, edition=edition)
    applied_language = language_arg if language_arg != "auto" else languages[0]
    if not results and language_arg == "auto" and len(pool) != len(edition_pool):
        fallback = _search_documents(
            edition_pool, query=search_query, limit=limit, edition=edition
        )
        if fallback:
            notices.append(
                f"No {languages[0]} results for this query; showing results from all languages."
            )
            results, pool, applied_language = fallback, edition_pool, "all"
    if version and not results and available_versions is not None:
        if pool:
            notices.append(f"No pages in Enterprise docs {version} match this query.")
        elif version in available_versions:
            notices.append(
                f"Enterprise docs {version} has no pages for language {applied_language}."
            )

    payload: dict[str, Any] = {
        "results": [result.as_dict() for result in results],
        "indexed_pages": indexed_pages,
        "searched_pages": len(pool),
        "sources": sources,
        "languages": list(DOC_LANGUAGES),
        "filters": {
            "language": applied_language,
            "version": version or None,
            "version_origin": version_origin,
            "edition": edition,
        },
        "notices": notices,
        "refreshed_at_unix": min(refreshed_at) if refreshed_at else None,
    }
    if version_origin == "query":
        payload["filters"]["query_without_version"] = search_query
    if available_versions is not None:
        payload["available_versions"] = available_versions
    return payload


def _normalize_language_arg(value: str) -> str:
    language = (value or "auto").strip().casefold()
    if language in {"any", "*"}:
        return "all"
    if language not in {"auto", "all", *DOC_LANGUAGES}:
        raise ValueError("language must be auto, all, en, zh, or ja")
    return language


def _normalize_edition(value: str) -> str:
    edition = (value or "all").strip().casefold().replace("_", "-")
    edition = {
        "any": "all",
        "": "all",
        "self-host": "self-hosted",
        "selfhosted": "self-hosted",
        "self hosted": "self-hosted",
    }.get(edition, edition)
    if edition not in DOC_EDITIONS:
        raise ValueError("edition must be all, cloud, or self-hosted")
    return edition


_VERSION_INPUT = re.compile(r"^v?(\d{1,3})[.\-](\d{1,3})(?:[.\-](?:\d{1,4}|x))?$", re.I)
_QUERY_VERSION = re.compile(
    r"(?<![A-Za-z0-9.])(?P<v>v)?(?P<major>\d{1,3})\.(?P<minor>\d{1,3})"
    r"(?:\.(?P<patch>\d{1,4}|x))?(?![A-Za-z0-9.])",
    re.I,
)


def _normalize_version(value: str) -> str:
    version = (value or "").strip().casefold()
    if not version or version in {"all", "any"}:
        return ""
    if version == "latest":
        return "latest"
    match = _VERSION_INPUT.match(version)
    if not match:
        raise ValueError("version must look like 3.5, 3.5.x, v3.5.2, or latest")
    return f"{int(match.group(1))}.{int(match.group(2))}.x"


def _detect_query_version(query: str, *, enterprise_only: bool) -> tuple[str, str]:
    """Find a version family in free text.

    When only Enterprise docs are searched any ``major.minor`` number counts.
    Otherwise only unambiguous forms (``3.5.x`` or ``v3.5``) count, so a query
    such as "python 3.12" does not silently drop the community docs.
    """
    for match in _QUERY_VERSION.finditer(query):
        explicit = bool(match.group("v")) or (match.group("patch") or "").casefold() == "x"
        if not (enterprise_only or explicit):
            continue
        family = f"{int(match.group('major'))}.{int(match.group('minor'))}.x"
        remaining = f"{query[: match.start()]} {query[match.end() :]}"
        remaining = re.sub(r"\s+", " ", remaining).strip()
        return family, remaining
    return "", query


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", version))


def _infer_query_language(text: str) -> str:
    if re.search(r"[぀-ヿ]", text):
        return "ja"
    if re.search(r"[㐀-䶿一-鿿]", text):
        return "zh"
    return "en"


def _matches_edition(document: Document, edition: str) -> bool:
    if edition == "all" or document.edition == "shared":
        return True
    if edition == "cloud":
        return document.edition == "cloud"
    return document.edition in {"self-hosted", "enterprise"}


_CLOUD_ONLY_MARKERS = re.compile(
    r"only available (?:on|in|for) (?:the )?dify cloud|dify cloud only|cloud[- ]only feature"
    r"|仅(?:适用于|限于?|支持)\s*(?:dify\s*)?cloud|クラウド版のみ",
    re.I,
)
_SELF_HOSTED_ONLY_MARKERS = re.compile(
    r"only available (?:on|in|for) self-host(?:ed|ing)?|self-host(?:ed)?[- ]only"
    r"|community edition only|仅(?:适用于|限于?|支持)\s*(?:自部署|私有化部署|社区版)"
    r"|セルフホスト(?:版)?のみ",
    re.I,
)


def _classify_document(url: str, content: str) -> tuple[str, str, str]:
    """Return (language, version family, edition) for an official docs page."""
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if parts and parts[0] in DOC_LANGUAGES:
        language, rest = parts[0], parts[1:]
    else:
        language, rest = _infer_query_language(content[:4000]), parts
    version = next((part for part in rest[:2] if re.fullmatch(r"\d+\.\d+\.x", part)), "")
    if parsed.hostname == "enterprise-docs.dify.ai":
        return language, version, "enterprise"
    section = rest[0] if rest else ""
    if section == "cloud":
        edition = "cloud"
    elif section in {"self-host", "self-hosted"}:
        edition = "self-hosted"
    else:
        cloud = bool(_CLOUD_ONLY_MARKERS.search(content))
        self_hosted = bool(_SELF_HOSTED_ONLY_MARKERS.search(content))
        if cloud != self_hosted:
            edition = "cloud" if cloud else "self-hosted"
        else:
            edition = "shared"
    return language, version, edition


def _make_document(*, title: str, url: str, content: str, source: str) -> Document:
    cleaned, summary = _index_content(_clean_content(content))
    language, version, edition = _classify_document(url, cleaned)
    return Document(
        title=title,
        url=url,
        content=cleaned,
        source=source,
        language=language,
        version=version,
        edition=edition,
        summary=summary,
    )


# Mintlify adds a machine-translation notice to every zh/ja page; it says
# nothing about the page and only pollutes snippets. official_docs_read keeps it.
_TRANSLATION_NOTICE = re.compile(
    r"(?m)^[ \t]*>[ \t]*(?:本文档由 AI 自动翻译|このドキュメントは AI によって自動翻訳されています)"
    r"[^\n]*(?:\n|$)"
)
# Mintlify API reference pages (zh/ja .md pages, versioned Enterprise pages)
# embed the whole OpenAPI document of one operation as a fenced YAML block:
#   ## OpenAPI
#   ````yaml /zh/api-reference/openapi_service.json post /datasets/{id}/...
_OPENAPI_BLOCK = re.compile(
    r"(?ms)(?:^#{1,6}[ \t]+OpenAPI[ \t]*\n\s*)?"
    r"^(?P<fence>`{3,}|~{3,})[ \t]*ya?ml[ \t]+\S+\.(?:json|ya?ml)[ \t]+"
    r"(?P<method>get|post|put|patch|delete|head|options|trace)[ \t]+(?P<path>/\S*)[ \t]*\n"
    r"(?P<body>.*?)^(?P=fence)[ \t]*$\n?"
)
# The English llms-full.txt feeds only keep the spec reference line:
#   /en/api-reference/openapi_service.json post /apps/annotations
_OPENAPI_REFERENCE_LINE = re.compile(
    r"(?m)^[ \t]*/\S+\.(?:json|ya?ml)[ \t]+"
    r"(?P<method>get|post|put|patch|delete|head|options|trace)[ \t]+(?P<path>/\S*)[ \t]*$"
)
_HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options", "trace")


def _index_content(content: str) -> tuple[str, str]:
    """Return (searchable text, fixed summary) for a cleaned docs page.

    OpenAPI YAML is replaced by a readable rendering of the operation (method,
    path, summary, description, parameter and response descriptions). The
    spec-wide boilerplate repeated on every API page (info, servers, tags,
    components) is dropped so it no longer matches unrelated queries.
    """
    content = _TRANSLATION_NOTICE.sub("", content)
    summaries: list[str] = []

    def render_block(match: re.Match[str]) -> str:
        method, path = match.group("method").upper(), match.group("path")
        spec, operation = _openapi_operation(match.group("body"), match.group("method"), path)
        summary = _operation_text(operation.get("summary"))
        description = _operation_text(operation.get("description"))
        lead = " — ".join(part for part in (summary, description) if part)
        if lead:
            summaries.append(lead)
        lines = [f"{method} {path}", "", summary, "", description, ""]
        lines.extend(
            f"- {name}: {text}" if text else f"- {name}"
            for name, text in _operation_fields(operation, spec)
        )
        return "\n".join(lines).strip() + "\n"

    content = _OPENAPI_BLOCK.sub(render_block, content)
    content = _OPENAPI_REFERENCE_LINE.sub(
        lambda match: f"{match.group('method').upper()} {match.group('path')}", content
    )
    content = re.sub(r"\n{3,}", "\n\n", content).strip()
    summary = _truncate(summaries[0], SUMMARY_CHARS) if summaries else ""
    return content, summary


def _openapi_operation(
    body: str, method: str, path: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (whole spec, the page's operation object); empty dicts if unparsable."""
    try:
        spec = yaml.load(body, Loader=_YAML_LOADER)
    except yaml.YAMLError:
        return {}, {}
    if not isinstance(spec, dict):
        return {}, {}
    paths = spec.get("paths")
    if not isinstance(paths, dict):
        return spec, {}
    item = paths.get(path)
    if not isinstance(item, dict):
        item = next((value for value in paths.values() if isinstance(value, dict)), {})
    operation = item.get(method.casefold())
    if not isinstance(operation, dict):
        operation = next(
            (item[key] for key in _HTTP_METHODS if isinstance(item.get(key), dict)), {}
        )
    return spec, operation


_YAML_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def _operation_text(value: Any) -> str:
    return re.sub(r"\s+", " ", value).strip() if isinstance(value, str) else ""


def _operation_fields(operation: dict[str, Any], spec: dict[str, Any]) -> list[tuple[str, str]]:
    """Parameter, request body field and response descriptions of one operation.

    Local ``$ref`` pointers (``#/components/...``) are followed, so fields of
    referenced schemas stay searchable even though ``components`` is dropped.
    """
    fields: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def resolve(node: Any, trail: tuple[str, ...] = ()) -> tuple[Any, tuple[str, ...]]:
        while isinstance(node, dict) and isinstance(node.get("$ref"), str):
            ref = node["$ref"]
            if not ref.startswith("#/") or ref in trail or len(trail) > 20:
                return {}, trail
            trail = (*trail, ref)
            target: Any = spec
            for part in ref[2:].split("/"):
                part = part.replace("~1", "/").replace("~0", "~")
                target = target.get(part) if isinstance(target, dict) else None
            node = target
        return node, trail

    def add(name: Any, description: Any) -> None:
        item = (str(name), _operation_text(description))
        if item[0] and item not in seen:
            seen.add(item)
            fields.append(item)

    def walk_schema(schema: Any, depth: int = 0, trail: tuple[str, ...] = ()) -> None:
        schema, trail = resolve(schema, trail)
        if not isinstance(schema, dict) or depth > 6:
            return
        properties = schema.get("properties")
        if isinstance(properties, dict):
            for name, child in properties.items():
                child_schema, _ = resolve(child, trail)
                description = (child.get("description") if isinstance(child, dict) else "") or (
                    child_schema.get("description") if isinstance(child_schema, dict) else ""
                )
                add(name, description)
                walk_schema(child, depth + 1, trail)
        walk_schema(schema.get("items"), depth + 1, trail)
        for key in ("allOf", "oneOf", "anyOf"):
            children = schema.get(key)
            for child in children if isinstance(children, list) else []:
                walk_schema(child, depth + 1, trail)

    parameters = operation.get("parameters")
    for parameter in parameters if isinstance(parameters, list) else []:
        parameter, _ = resolve(parameter)
        if isinstance(parameter, dict):
            add(parameter.get("name", ""), parameter.get("description"))
    body, _ = resolve(operation.get("requestBody"))
    if isinstance(body, dict) and isinstance(body.get("content"), dict):
        for media in body["content"].values():
            if isinstance(media, dict):
                schema, _ = resolve(media.get("schema"))
                if isinstance(schema, dict) and schema.get("description"):
                    add("body", schema.get("description"))
                walk_schema(media.get("schema"))
    responses = operation.get("responses")
    for status, response in (responses.items() if isinstance(responses, dict) else []):
        response, _ = resolve(response)
        if isinstance(response, dict):
            add(status, response.get("description"))
    return fields


def _truncate(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


async def _load_enterprise_versions(
    settings: Settings,
    documents: list[Document] | None = None,
) -> list[str]:
    global _ENTERPRISE_VERSIONS_CACHE

    now = time.monotonic()
    if _ENTERPRISE_VERSIONS_CACHE and _ENTERPRISE_VERSIONS_CACHE[0] > now:
        versions = list(_ENTERPRISE_VERSIONS_CACHE[1])
    else:
        async with _http_client(settings) as client:
            text, _, _ = await _fetch_text_with_client(
                client,
                ENTERPRISE_VERSIONS_INDEX_URL,
                allowed_hosts=DOCS_HOSTS,
                max_bytes=MAX_DOWNLOAD_BYTES,
            )
        versions = _parse_enterprise_versions(text)
        _ENTERPRISE_VERSIONS_CACHE = (
            now + max(21_600.0, settings.docs_cache_ttl_seconds),
            versions,
        )
    for document in documents or []:
        if document.source == ENTERPRISE_SOURCE and document.version:
            versions.append(document.version)
    return sorted(set(versions), key=_version_key, reverse=True)


def _parse_enterprise_versions(text: str) -> list[str]:
    versions = {
        f"{int(major)}.{int(minor)}.x"
        for major, minor in re.findall(r"/_llms/[a-z]{2}/(\d+)-(\d+)-x\.(?:txt|md)\b", text)
    }
    return sorted(versions, key=_version_key, reverse=True)


async def _enterprise_version_documents(
    version: str,
    languages: list[str],
    documents: list[Document],
    settings: Settings,
    notices: list[str],
) -> list[Document]:
    """Return Enterprise documents of one version family for the given languages.

    The default corpus only holds the current version (plus one translated
    version); missing (version, language) pairs are fetched from that version's
    own llms index and cached.
    """
    selected = [
        document
        for document in documents
        if document.source == ENTERPRISE_SOURCE and document.version == version
    ]
    present = {document.language for document in selected}
    for language in languages:
        if language in present:
            continue
        extra = await _load_enterprise_version_corpus(version, language, settings)
        if extra is None:
            notices.append(f"Enterprise docs {version} is not published in language {language}.")
            continue
        selected.extend(document for document in extra if document.version == version)
    return _dedupe_documents(selected)


async def _load_enterprise_version_corpus(
    version: str,
    language: str,
    settings: Settings,
) -> list[Document] | None:
    key = (version, language)
    now = time.monotonic()
    cached = _VERSION_CORPUS_CACHE.get(key)
    if cached and cached[0] > now:
        _VERSION_CORPUS_CACHE.move_to_end(key)
        return cached[1]

    index_url = ENTERPRISE_VERSION_INDEX_TEMPLATE.format(
        language=language, slug=version.replace(".", "-")
    )
    documents: list[Document] | None
    async with _http_client(settings) as client:
        try:
            references = await _load_page_references(
                client, index_url, allowed_hosts=DOCS_HOSTS
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise
            documents = None
        else:
            documents = _dedupe_documents(
                await _fetch_documents(client, references, source=ENTERPRISE_SOURCE)
            )

    ttl = max(21_600.0, settings.docs_cache_ttl_seconds)
    _VERSION_CORPUS_CACHE[key] = (now + ttl, documents)
    _VERSION_CORPUS_CACHE.move_to_end(key)
    while len(_VERSION_CORPUS_CACHE) > MAX_VERSION_CORPORA:
        _VERSION_CORPUS_CACHE.popitem(last=False)
    return documents


async def _load_docs_corpus(source: str, settings: Settings) -> tuple[list[Document], float]:
    now = time.monotonic()
    cached = _DOCS_CACHE.get(source)
    if cached and cached[0] > now:
        return cached[2], cached[1]

    async with _http_client(settings) as client:
        full_text, _, _ = await _fetch_text_with_client(
            client,
            DOCS_FULL_URLS[source],
            allowed_hosts=DOCS_HOSTS,
            max_bytes=MAX_DOWNLOAD_BYTES,
        )
        documents = _parse_llms_full(full_text, source=source)
        references: list[PageReference] = []
        for index_url in DOCS_TRANSLATED_INDEXES[source]:
            references.extend(
                await _load_page_references(client, index_url, allowed_hosts=DOCS_HOSTS)
            )
        existing_urls = {document.url for document in documents}
        references = [
            reference
            for reference in references
            if _without_markdown_suffix(reference.url) not in existing_urls
        ]
        documents.extend(await _fetch_documents(client, references, source=source))

    documents = _dedupe_documents(documents)
    if not documents:
        raise ValueError(f"official docs corpus is empty: {source}")
    refreshed_at = time.time()
    # Full multilingual corpora are expensive to rebuild. Keep them for at least
    # six hours while still honoring a larger configured TTL.
    ttl = max(21_600.0, settings.docs_cache_ttl_seconds)
    _DOCS_CACHE[source] = (now + ttl, refreshed_at, documents)
    return documents, refreshed_at


async def _load_page_references(
    client: httpx.AsyncClient,
    root_index_url: str,
    *,
    allowed_hosts: set[str],
) -> list[PageReference]:
    pending = [root_index_url]
    seen_indexes: set[str] = set()
    references: list[PageReference] = []
    while pending:
        index_url = pending.pop()
        if index_url in seen_indexes:
            continue
        seen_indexes.add(index_url)
        text, _, _ = await _fetch_text_with_client(
            client,
            index_url,
            allowed_hosts=allowed_hosts,
            max_bytes=MAX_DOWNLOAD_BYTES,
        )
        for reference in _parse_llms_index(text):
            if "/_llms/" in reference.url:
                if reference.url not in seen_indexes:
                    pending.append(reference.url)
            elif reference.url.endswith(".md"):
                references.append(reference)
    return _dedupe_references(references)


async def _fetch_documents(
    client: httpx.AsyncClient,
    references: list[PageReference],
    *,
    source: str,
) -> list[Document]:
    semaphore = asyncio.Semaphore(PAGE_FETCH_CONCURRENCY)

    async def fetch(reference: PageReference) -> tuple[Document | None, str | None]:
        async with semaphore:
            try:
                content, _, final_url = await _fetch_text_with_client(
                    client,
                    reference.url,
                    allowed_hosts=DOCS_HOSTS,
                    max_bytes=MAX_DOWNLOAD_BYTES,
                )
                return (
                    _make_document(
                        title=reference.title or _extract_title(content),
                        url=_without_markdown_suffix(final_url),
                        content=content,
                        source=source,
                    ),
                    None,
                )
            except (httpx.HTTPError, ValueError) as exc:
                return None, f"{reference.url}: {exc}"

    results = await asyncio.gather(*(fetch(reference) for reference in references))
    documents = [document for document, _ in results if document is not None]
    errors = [error for _, error in results if error is not None]
    if errors:
        raise ValueError(
            f"failed to refresh {len(errors)} official docs pages; first failure: {errors[0]}"
        )
    return documents


async def _load_site_urls(settings: Settings) -> tuple[list[str], float]:
    global _SITEMAP_CACHE

    now = time.monotonic()
    if _SITEMAP_CACHE and _SITEMAP_CACHE[0] > now:
        return _SITEMAP_CACHE[2], _SITEMAP_CACHE[1]

    async with _http_client(settings) as client:
        index_text, _, _ = await _fetch_text_with_client(
            client,
            SITE_SITEMAP_INDEX_URL,
            allowed_hosts=SITE_HOSTS,
            max_bytes=MAX_DOWNLOAD_BYTES,
        )
        sitemap_urls = _parse_sitemap(index_text)
        page_urls: list[str] = []
        for sitemap_url in sitemap_urls:
            _validate_url(sitemap_url, SITE_HOSTS)
            sitemap_text, _, _ = await _fetch_text_with_client(
                client,
                sitemap_url,
                allowed_hosts=SITE_HOSTS,
                max_bytes=MAX_DOWNLOAD_BYTES,
            )
            page_urls.extend(_parse_sitemap(sitemap_text))

    page_urls = _dedupe_urls(url for url in page_urls if _is_allowed(url, SITE_HOSTS))
    refreshed_at = time.time()
    _SITEMAP_CACHE = (
        now + max(0.0, settings.docs_cache_ttl_seconds),
        refreshed_at,
        page_urls,
    )
    return page_urls, refreshed_at


def _parse_llms_full(content: str, *, source: str) -> list[Document]:
    content = content.replace("\r\n", "\n")
    marker = re.compile(
        r"(?m)^# (?P<title>[^\n]+)\nSource: (?P<url>https://[^\s]+)\s*$"
    )
    matches = list(marker.finditer(content))
    documents: list[Document] = []
    for index, match in enumerate(matches):
        url = match.group("url").strip()
        if not _is_allowed(url, DOCS_HOSTS):
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
        documents.append(
            _make_document(
                title=match.group("title").strip(),
                url=url,
                content=content[match.end() : end],
                source=source,
            )
        )
    return _dedupe_documents(documents)


def _parse_llms_index(content: str) -> list[PageReference]:
    pattern = re.compile(
        r"^- \[(?P<title>.+?)\]\((?P<url>https://[^)]+)\)(?:: (?P<desc>.*))?$"
    )
    references: list[PageReference] = []
    for raw_line in content.splitlines():
        match = pattern.match(raw_line.strip())
        if not match:
            continue
        url = match.group("url").strip()
        if not _is_allowed(url, DOCS_HOSTS):
            continue
        references.append(
            PageReference(
                title=match.group("title").strip(),
                url=url,
                description=(match.group("desc") or "").strip(),
            )
        )
    return references


def _parse_sitemap(content: str) -> list[str]:
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise ValueError("invalid official sitemap XML") from exc
    urls: list[str] = []
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] == "loc" and element.text:
            urls.append(element.text.strip())
    return urls


_STOPWORDS = frozenset(
    """a an and are as at be by can could do does for from get how i if in into is it its
    me my not of on or should that the this to use using via was what when where which
    who why will with you your 如何 怎么 怎样 什么 为什么 的 是 吗 呢""".split()
)


@dataclass
class _TermMatch:
    title: bool = False
    heading: bool = False
    path: bool = False
    body_count: int = 0

    @property
    def any(self) -> bool:
        return self.title or self.heading or self.path or self.body_count > 0


def _query_terms(normalized: str) -> list[str]:
    tokens = list(dict.fromkeys(_tokens(normalized)))
    meaningful = [token for token in tokens if token not in _STOPWORDS]
    return meaningful or tokens


def _term_pattern(term: str) -> re.Pattern[str]:
    escaped = re.escape(term)
    if re.fullmatch(r"[0-9a-z]+", term):
        # Word-prefix match: "timeout" matches "timeouts" but "api" not "rapid".
        return re.compile(rf"(?<![0-9a-z]){escaped}")
    return re.compile(escaped)


@lru_cache(maxsize=32_768)
def _document_fields(document: Document) -> dict[str, str]:
    return {
        "title": _normalize(document.title),
        "path": _normalize(re.sub(r"[-_/.]+", " ", urlparse(document.url).path)),
        "heading": _normalize(" ".join(re.findall(r"(?m)^#{1,6}[ \t]+(.+)$", document.content))),
        "body": _normalize(document.content),
    }


def _search_documents(
    documents: list[Document],
    *,
    query: str,
    limit: int,
    edition: str = "all",
) -> list[SearchResult]:
    normalized = _normalize(query)
    terms = _query_terms(normalized)
    if not terms:
        return []
    patterns = {term: _term_pattern(term) for term in terms}
    phrase = normalized if len(terms) > 1 else ""

    matches: list[tuple[Document, dict[str, _TermMatch], dict[str, str]]] = []
    document_frequency = dict.fromkeys(terms, 0)
    for document in documents:
        fields = _document_fields(document)
        term_matches: dict[str, _TermMatch] = {}
        for term, pattern in patterns.items():
            match = _TermMatch(
                title=bool(pattern.search(fields["title"])),
                heading=bool(pattern.search(fields["heading"])),
                path=bool(pattern.search(fields["path"])),
                body_count=sum(1 for _ in zip(range(10), pattern.finditer(fields["body"]))),
            )
            if match.any:
                term_matches[term] = match
                document_frequency[term] += 1
        if term_matches:
            matches.append((document, term_matches, fields))

    total = max(1, len(documents))
    idf = {
        term: 1.0 + math.log((total + 1) / (frequency + 1))
        for term, frequency in document_frequency.items()
    }

    scored: list[tuple[int, float, Document, tuple[str, ...]]] = []
    for document, term_matches, fields in matches:
        score = 0.0
        for term, match in term_matches.items():
            field_score = (
                12.0 * match.title
                + 8.0 * match.heading
                + 6.0 * match.path
                + (2.0 + 3.0 * min(match.body_count, 5) / 5 if match.body_count else 0.0)
            )
            score += idf[term] * field_score
        if phrase:
            score += 30.0 * (phrase in fields["title"])
            score += 20.0 * (phrase in fields["heading"])
            score += 10.0 * (phrase in fields["body"])
        if edition == "cloud" and document.edition == "cloud":
            score += CLOUD_EDITION_BOOST * sum(idf[term] for term in term_matches)
        matched = tuple(term for term in terms if term in term_matches)
        scored.append((len(matched), score, document, matched))

    # All query words first, then by number of matched words, then weighted score.
    scored.sort(
        key=lambda item: (-item[0], -item[1], item[2].title.casefold(), item[2].url)
    )
    results: list[SearchResult] = []
    for _, _, document, matched in scored[:limit]:
        snippet_terms = sorted(matched, key=lambda term: -idf[term])
        results.append(
            SearchResult(
                title=document.title,
                url=document.url,
                description=document.summary
                or _make_snippet(document.content, phrase, snippet_terms, patterns=patterns),
                source=document.source,
                language=document.language,
                version=document.version or None,
                edition=document.edition,
                matched_terms=matched,
            )
        )
    return results


def _search_site_urls(urls: list[str], *, query: str, limit: int) -> list[dict[str, str]]:
    normalized = _normalize(query)
    tokens = _tokens(normalized)
    scored: list[tuple[int, str, str]] = []
    for url in urls:
        title = _title_from_url(url)
        normalized_title = _normalize(title)
        haystack = _normalize(f"{title} {url}")
        score = 0
        if normalized in normalized_title:
            score += 20
        if normalized in haystack:
            score += 10
        score += sum(3 for token in tokens if token in haystack)
        if score:
            scored.append((score, title, url))
    scored.sort(key=lambda item: (-item[0], item[1].casefold(), item[2]))
    return [{"title": title, "url": url} for _, title, url in scored[:limit]]


async def _read_content(
    url: str,
    arguments: dict[str, Any],
    settings: Settings,
    *,
    prefer_markdown: bool,
) -> dict[str, Any]:
    offset = optional_int(arguments, "offset", 0, minimum=0, maximum=2_147_483_647)
    max_chars = optional_int(
        arguments,
        "max_chars",
        DEFAULT_READ_CHARS,
        minimum=1,
        maximum=MAX_READ_CHARS,
    )
    allowed_hosts = DOCS_HOSTS if prefer_markdown else SITE_HOSTS
    fetch_url = _markdown_url(url) if prefer_markdown else url
    async with _http_client(settings) as client:
        try:
            text, content_type, final_url = await _fetch_text_with_client(
                client,
                fetch_url,
                allowed_hosts=allowed_hosts,
                max_bytes=MAX_DOWNLOAD_BYTES,
            )
        except httpx.HTTPStatusError as exc:
            if not prefer_markdown or fetch_url == url or exc.response.status_code != 404:
                raise
            text, content_type, final_url = await _fetch_text_with_client(
                client,
                url,
                allowed_hosts=allowed_hosts,
                max_bytes=MAX_DOWNLOAD_BYTES,
            )

    if "html" in content_type.casefold():
        title, text = _strip_html(text)
    else:
        title = _extract_title(text)
    text = _clean_content(text)
    if not title:
        title = _extract_title(text)
    total_chars = len(text)
    safe_offset = min(offset, total_chars)
    end = min(safe_offset + max_chars, total_chars)
    next_offset = end if end < total_chars else None
    payload: dict[str, Any] = {
        "url": url,
        "fetched_url": final_url,
        "title": title,
        "content": text[safe_offset:end],
        "offset": safe_offset,
        "next_offset": next_offset,
        "total_chars": total_chars,
        "truncated": next_offset is not None,
    }
    if prefer_markdown:
        language, version, edition = _classify_document(_without_markdown_suffix(final_url), text)
        payload.update({"language": language, "version": version or None, "edition": edition})
    return payload


_FRONTMATTER = re.compile(r"\A\ufeff?---[ \t]*\n(?P<body>.*?)\n---[ \t]*(?:\n|\Z)", re.S)
_CODE_FENCE = re.compile(r"(?ms)^[ \t]*(```|~~~).*?^[ \t]*\1[^\n]*$")


def _extract_title(text: str) -> str:
    """Title from YAML frontmatter, then <title>, then the first H1."""
    frontmatter = _FRONTMATTER.match(text)
    if frontmatter:
        for line in frontmatter.group("body").splitlines():
            match = re.match(r"^title[ \t]*:[ \t]*(.+?)[ \t]*$", line)
            if match:
                value = match.group(1).strip().strip("'\"").strip()
                if value:
                    return value
        text = text[frontmatter.end() :]
    match = re.search(r"<title\b[^>]*>(.*?)</title>", text, re.I | re.S)
    if match and _inline_html_text(match.group(1)):
        return _inline_html_text(match.group(1))
    without_code = _CODE_FENCE.sub("", text)
    match = re.search(r"(?m)^[ \t]{0,3}#[ \t]+(.+?)[ \t]*#*[ \t]*$", without_code)
    if match:
        return _inline_html_text(match.group(1))
    match = re.search(r"<h1\b[^>]*>(.*?)</h1>", without_code, re.I | re.S)
    if match:
        return _inline_html_text(match.group(1))
    return ""


def _inline_html_text(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", html_lib.unescape(value)).strip()


_IMAGE_EXTENSIONS = r"(?:png|jpe?g|gif|webp|svg|bmp|ico|avif)"
_LINK_TARGET = r"\((?:[^()\n]|\([^()\n]*\))*\)"
_DOC_INDEX_HEADING = re.compile(
    r"(?mi)^[ \t]*>?[ \t]*#{0,6}[ \t]*\**Documentation Index\**[ \t]*\n"
    r"(?:[ \t]*>.*(?:\n|$))*"
)
_DOC_INDEX_LINES = re.compile(
    r"(?mi)^[ \t]*>?[ \t]*(?:Fetch the complete documentation index at:"
    r"|Use this file to discover all available pages)[^\n]*(?:\n|$)"
)
_MD_IMAGE = re.compile(r"!\[[^\]\n]*\]" + _LINK_TARGET)
_MD_IMAGE_LINK = re.compile(
    r"\[([^\]\n]*)\]\(\s*https?://[^\s()]+?\." + _IMAGE_EXTENSIONS
    + r"(?:\?[^\s()]*)?(?:\s+\"[^\"]*\")?\s*\)",
    re.I,
)
_EMPTY_LINK = re.compile(r"\[\s*\]" + _LINK_TARGET)
_IMAGE_REFERENCE = re.compile(
    r"(?mi)^[ \t]*\[[^\]\n]+\]:[ \t]*\S+?\." + _IMAGE_EXTENSIONS + r"(?:\?\S*)?[ \t]*(?:\n|$)"
)
_HTML_IMG = re.compile(r"<img\b[^>]*>", re.I | re.S)
# Site chrome that Mintlify appends to every page .md (currently only the
# "built and hosted on Mintlify" line). Matched as whole lines only, so prose
# that merely mentions Mintlify or "helpful" is kept.
_MINTLIFY_BOILERPLATE = re.compile(
    r"(?mi)^[ \t]*(?:"
    r"This documentation is built and hosted on \[?Mintlify\]?(?:\([^)\n]*\))?[^\n]*"
    r"|(?:Powered|Built) (?:by|with) \[?Mintlify\]?(?:\([^)\n]*\))?[ \t.]*"
    r"|Was this page helpful\??(?:[ \t]*(?:Yes|No))*"
    r"|(?:Suggest edits|Raise issue|Edit this page)(?:[ \t]*(?:Suggest edits|Raise issue))*"
    r")[ \t]*$\n?"
)
_EMPTY_FRAME = re.compile(r"<Frame\b[^>]*>\s*</Frame>", re.I)
_BARE_IMAGE_URL = re.compile(
    r"https?://[^\s<>()\[\]\"']+?\." + _IMAGE_EXTENSIONS
    + r"(?:\?[^\s<>()\[\]\"']*)?(?=[\s<>()\[\]\"']|$)",
    re.I,
)


def _clean_content(text: str) -> str:
    """Remove llms index notices, Mintlify footer and image noise from docs content."""
    text = text.replace("\r\n", "\n")
    text = _DOC_INDEX_HEADING.sub("", text)
    text = _DOC_INDEX_LINES.sub("", text)
    text = _MINTLIFY_BOILERPLATE.sub("", text)
    text = _MD_IMAGE.sub("", text)
    text = _MD_IMAGE_LINK.sub(lambda match: match.group(1), text)
    text = _EMPTY_LINK.sub("", text)
    text = _IMAGE_REFERENCE.sub("", text)
    text = _HTML_IMG.sub("", text)
    text = _EMPTY_FRAME.sub("", text)
    text = _BARE_IMAGE_URL.sub("", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _http_client(settings: Settings) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=settings.docs_timeout_seconds,
        follow_redirects=False,
        trust_env=False,
        headers={"User-Agent": "DifyOfficialContentMCP/0.2"},
    )


async def _fetch_text_with_client(
    client: httpx.AsyncClient,
    url: str,
    *,
    allowed_hosts: set[str],
    max_bytes: int,
) -> tuple[str, str, str]:
    current_url = url
    for _ in range(MAX_REDIRECTS + 1):
        _validate_url(current_url, allowed_hosts)
        async with client.stream("GET", current_url) as response:
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    response.raise_for_status()
                current_url = urljoin(str(response.url), location)
                continue
            response.raise_for_status()
            content_length = response.headers.get("content-length")
            if content_length:
                try:
                    if int(content_length) > max_bytes:
                        raise ValueError("official content response exceeds size limit")
                except ValueError as exc:
                    if str(exc) == "official content response exceeds size limit":
                        raise
            chunks: list[bytes] = []
            received = 0
            async for chunk in response.aiter_bytes():
                received += len(chunk)
                if received > max_bytes:
                    raise ValueError("official content response exceeds size limit")
                chunks.append(chunk)
            content_type = response.headers.get("content-type", "")
            if not _is_text_content(content_type):
                raise ValueError("official content URL did not return readable text")
            encoding = response.encoding or "utf-8"
            text = b"".join(chunks).decode(encoding, errors="replace")
            return text, content_type, str(response.url)
    raise ValueError("too many redirects from official content URL")


def _resolve_sources(source: str) -> list[str]:
    aliases = {
        "auto": list(DOCS_FULL_URLS),
        "community": ["official_docs_community"],
        "cloud": ["official_docs_community"],
        "enterprise": ["official_docs_enterprise"],
    }
    if source in DOCS_FULL_URLS:
        return [source]
    if source in aliases:
        return aliases[source]
    raise ValueError(
        "source must be auto, official_docs_community, or official_docs_enterprise"
    )


def _filter_site_urls(urls: list[str], *, language: str, section: str) -> list[str]:
    if language not in {"auto", "en", "zh", "ja", "ko"}:
        raise ValueError("language must be auto, en, zh, ja, or ko")
    normalized_section = section.strip().strip("/").casefold()
    filtered: list[str] = []
    for url in urls:
        path = urlparse(url).path.strip("/")
        parts = [part for part in path.split("/") if part]
        detected_language = parts[0] if parts and parts[0] in {"zh", "ja", "ko"} else "en"
        content_parts = parts[1:] if detected_language != "en" else parts
        if language != "auto" and detected_language != language:
            continue
        if normalized_section and not "/".join(content_parts).casefold().startswith(
            normalized_section
        ):
            continue
        filtered.append(url)
    return filtered


def _validate_url(url: str, allowed_hosts: set[str]) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError("only HTTPS Dify official URLs are allowed")
    if parsed.username or parsed.password:
        raise ValueError("URL user information is not allowed")
    if parsed.hostname not in allowed_hosts:
        raise ValueError("only Dify official hosts are allowed")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("invalid URL port") from exc
    if port not in {None, 443}:
        raise ValueError("only the standard HTTPS port is allowed")


def _is_allowed(url: str, allowed_hosts: set[str]) -> bool:
    try:
        _validate_url(url, allowed_hosts)
    except ValueError:
        return False
    return True


def _is_text_content(content_type: str) -> bool:
    media_type = content_type.split(";", 1)[0].strip().casefold()
    return (
        not media_type
        or media_type.startswith("text/")
        or media_type in {"application/json", "application/xml", "application/xhtml+xml"}
        or media_type.endswith("+json")
        or media_type.endswith("+xml")
    )


def _markdown_url(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path
    if path.endswith((".md", ".txt", ".json", ".xml")):
        return url
    if path.endswith("/"):
        path = path[:-1]
    return parsed._replace(path=f"{path}.md").geturl()


def _without_markdown_suffix(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.path.endswith(".md"):
        return url
    return parsed._replace(path=parsed.path[:-3]).geturl()


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value).casefold().strip()


def _tokens(normalized: str) -> list[str]:
    return [token for token in re.split(r"[\s/,_:.-]+", normalized) if token]


def _make_snippet(
    content: str,
    phrase: str,
    terms: list[str],
    width: int = 420,
    *,
    patterns: dict[str, re.Pattern[str]] | None = None,
) -> str:
    """Pick the window that covers the most distinct query terms.

    ``terms`` should be ordered rarest first; candidate windows start around the
    occurrences of the phrase or of the rarest term.
    """
    folded = content.casefold()
    patterns = patterns or {term: _term_pattern(term) for term in terms}
    occurrences = {
        term: [match.start() for _, match in zip(range(50), patterns[term].finditer(folded))]
        for term in terms
        if term in patterns
    }
    candidates: list[int] = []
    if phrase:
        position = folded.find(phrase)
        if position >= 0:
            candidates.append(position)
    for term in terms:
        if occurrences.get(term):
            candidates.extend(occurrences[term])
            break
    if not candidates:
        firsts = [positions[0] for positions in occurrences.values() if positions]
        candidates = [min(firsts, default=0)]

    def coverage(center: int) -> int:
        low, high = center - width // 3, center - width // 3 + width
        return sum(
            1 for positions in occurrences.values() if any(low <= p < high for p in positions)
        )

    position = max(candidates, key=lambda candidate: (coverage(candidate), -candidate))
    start = max(0, position - width // 3)
    end = min(len(content), start + width)
    snippet = re.sub(r"\s+", " ", content[start:end]).strip()
    if start:
        snippet = f"…{snippet}"
    if end < len(content):
        snippet = f"{snippet}…"
    return snippet


def _title_from_url(url: str) -> str:
    path = urlparse(url).path.strip("/")
    if not path:
        return "Dify"
    parts = [part for part in path.split("/") if part]
    if parts and parts[0] in {"zh", "ja", "ko"}:
        parts = parts[1:]
    slug = parts[-1] if parts else "Dify"
    return re.sub(r"[-_]+", " ", slug).strip().title() or "Dify"


def _dedupe_documents(documents: list[Document]) -> list[Document]:
    seen: set[str] = set()
    output: list[Document] = []
    for document in documents:
        if document.url in seen:
            continue
        seen.add(document.url)
        output.append(document)
    return output


def _dedupe_references(references: list[PageReference]) -> list[PageReference]:
    seen: set[str] = set()
    output: list[PageReference] = []
    for reference in references:
        if reference.url in seen:
            continue
        seen.add(reference.url)
        output.append(reference)
    return output


def _dedupe_urls(urls: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        output.append(url)
    return output


class _HTMLTextExtractor(HTMLParser):
    IGNORED_TAGS = {"script", "style", "noscript", "svg", "nav", "header", "footer"}
    BLOCK_TAGS = {
        "article",
        "br",
        "div",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "main",
        "p",
        "section",
        "table",
        "td",
        "th",
        "tr",
    }

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.main_parts: list[str] = []
        self.title_parts: list[str] = []
        self.h1_parts: list[str] = []
        self._ignored_depth = 0
        self._in_h1 = False
        self._h1_done = False
        self._main_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.IGNORED_TAGS:
            self._ignored_depth += 1
        if tag == "main":
            self._main_depth += 1
        if tag == "title" and not self._ignored_depth and not self.title_parts:
            self._in_title = True
        if tag == "h1" and not self._ignored_depth and not self._h1_done:
            self._in_h1 = True
        if not self._ignored_depth and tag in self.BLOCK_TAGS:
            self._append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title" and self._in_title:
            self._in_title = False
        if tag == "h1" and self._in_h1:
            self._in_h1 = False
            self._h1_done = bool(self.h1_parts)
        if tag in self.IGNORED_TAGS and self._ignored_depth:
            self._ignored_depth -= 1
        if not self._ignored_depth and tag in self.BLOCK_TAGS:
            self._append("\n")
        if tag == "main" and self._main_depth:
            self._main_depth -= 1

    def handle_data(self, data: str) -> None:
        value = re.sub(r"\s+", " ", data).strip()
        if not value:
            return
        if self._in_title:
            self.title_parts.append(value)
        elif not self._ignored_depth:
            if self._in_h1:
                self.h1_parts.append(value)
            self._append(value)

    def _append(self, value: str) -> None:
        self.parts.append(value)
        if self._main_depth:
            self.main_parts.append(value)


def _strip_html(html: str) -> tuple[str, str]:
    parser = _HTMLTextExtractor()
    parser.feed(html)
    title = " ".join(parser.title_parts).strip() or " ".join(parser.h1_parts).strip()
    text = " ".join(parser.main_parts or parser.parts)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return title, text.strip()
