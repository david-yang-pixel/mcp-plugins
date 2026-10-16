from __future__ import annotations

import asyncio
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse

import httpx

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
            "Chinese, and Japanese. Uses Dify official feeds only, never Zendesk Help Center."
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
            "long pages completely without permanent truncation."
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


_DOCS_CACHE: dict[str, tuple[float, float, list[Document]]] = {}
_SITEMAP_CACHE: tuple[float, float, list[str]] | None = None


async def handle(name: str, arguments: dict[str, Any], settings: Settings) -> dict[str, Any]:
    try:
        if name == "official_docs_search":
            query = required_str(arguments, "query")
            source = optional_str(arguments, "source", "auto") or "auto"
            limit = optional_int(arguments, "limit", 5, minimum=1, maximum=20)
            sources = _resolve_sources(source)
            documents: list[Document] = []
            refreshed_at: list[float] = []
            for item in sources:
                source_documents, source_refreshed_at = await _load_docs_corpus(item, settings)
                documents.extend(source_documents)
                refreshed_at.append(source_refreshed_at)
            results = _search_documents(documents, query=query, limit=limit)
            return success(
                {
                    "results": [result.__dict__ for result in results],
                    "indexed_pages": len(documents),
                    "sources": sources,
                    "languages": ["en", "zh", "ja"],
                    "refreshed_at_unix": min(refreshed_at) if refreshed_at else None,
                }
            )

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
        references = [reference for reference in references if reference.url not in existing_urls]
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
                    Document(
                        title=reference.title,
                        url=_without_markdown_suffix(final_url),
                        content=content.strip(),
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
            Document(
                title=match.group("title").strip(),
                url=url,
                content=content[match.end() : end].strip(),
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


def _search_documents(documents: list[Document], *, query: str, limit: int) -> list[SearchResult]:
    normalized = _normalize(query)
    tokens = _tokens(normalized)
    scored: list[tuple[int, Document, str]] = []
    for document in documents:
        title = _normalize(document.title)
        url = _normalize(document.url)
        body = _normalize(document.content)
        score = 0
        if normalized in title:
            score += 100
        if normalized in url:
            score += 40
        if normalized in body:
            score += 30
        for token in tokens:
            if token in title:
                score += 20
            if token in url:
                score += 8
            if token in body:
                score += 4
        if score:
            scored.append(
                (score, document, _make_snippet(document.content, normalized, tokens))
            )
    scored.sort(key=lambda item: (-item[0], item[1].title.casefold(), item[1].url))
    return [
        SearchResult(
            title=document.title,
            url=document.url,
            description=snippet,
            source=document.source,
        )
        for _, document, snippet in scored[:limit]
    ]


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

    title = ""
    if "html" in content_type.casefold():
        title, text = _strip_html(text)
    text = text.strip()
    total_chars = len(text)
    safe_offset = min(offset, total_chars)
    end = min(safe_offset + max_chars, total_chars)
    next_offset = end if end < total_chars else None
    return {
        "url": url,
        "fetched_url": final_url,
        "title": title,
        "content": text[safe_offset:end],
        "offset": safe_offset,
        "next_offset": next_offset,
        "total_chars": total_chars,
        "truncated": next_offset is not None,
    }


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
    normalized: str,
    tokens: list[str],
    width: int = 420,
) -> str:
    folded = content.casefold()
    positions = [folded.find(normalized)] if normalized else []
    positions.extend(folded.find(token) for token in tokens)
    positions = [position for position in positions if position >= 0]
    position = min(positions) if positions else 0
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
        self._ignored_depth = 0
        self._main_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.IGNORED_TAGS:
            self._ignored_depth += 1
        if tag == "main":
            self._main_depth += 1
        if tag == "title" and not self._ignored_depth and not self.title_parts:
            self._in_title = True
        if not self._ignored_depth and tag in self.BLOCK_TAGS:
            self._append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title" and self._in_title:
            self._in_title = False
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
            self._append(value)

    def _append(self, value: str) -> None:
        self.parts.append(value)
        if self._main_depth:
            self.main_parts.append(value)


def _strip_html(html: str) -> tuple[str, str]:
    parser = _HTMLTextExtractor()
    parser.feed(html)
    title = " ".join(parser.title_parts).strip()
    text = " ".join(parser.main_parts or parser.parts)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return title, text.strip()
