from __future__ import annotations

import re
from typing import Any

import httpx

from .mcp import failure, optional_int, optional_str, required_int, required_str, success
from .settings import Settings

GITHUB_API_BASE = "https://api.github.com"
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_ORG_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

TOOLS = [
    {
        "name": "github_search_prs",
        "description": "Search GitHub pull requests for known fixes.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 5},
            },
            "required": ["repo", "query"],
        },
    },
    {
        "name": "github_get_pr",
        "description": "Read a GitHub pull request by repo and PR number.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "pr_number": {"type": "integer"},
            },
            "required": ["repo", "pr_number"],
        },
    },
    {
        "name": "github_search_issues",
        "description": "Search GitHub issues for bug reports, regressions, and known limitations.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 5},
            },
            "required": ["repo", "query"],
        },
    },
    {
        "name": "github_get_issue",
        "description": "Read a GitHub issue by repo and issue number.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "issue_number": {"type": "integer"},
            },
            "required": ["repo", "issue_number"],
        },
    },
    {
        "name": "github_code_search",
        "description": "Search GitHub code without local clones.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "scope": {"type": "string", "default": "langgenius"},
                "language": {"type": "string"},
                "path": {"type": "string"},
                "limit": {"type": "integer", "default": 10},
            },
            "required": ["query"],
        },
    },
]


async def handle(name: str, arguments: dict[str, Any], settings: Settings) -> dict[str, Any]:
    try:
        if name == "github_search_prs":
            repo = _repo(required_str(arguments, "repo"))
            query = required_str(arguments, "query")
            limit = optional_int(arguments, "limit", 5, minimum=1, maximum=10)
            data = await _request(
                settings,
                "/search/issues",
                params={"q": f"repo:{repo} is:pr {query}", "per_page": str(limit)},
            )
            return success(data)

        if name == "github_get_pr":
            repo = _repo(required_str(arguments, "repo"))
            pr_number = required_int(arguments, "pr_number")
            data = await _request(settings, f"/repos/{repo}/pulls/{pr_number}")
            return success(data)

        if name == "github_search_issues":
            repo = _repo(required_str(arguments, "repo"))
            query = required_str(arguments, "query")
            limit = optional_int(arguments, "limit", 5, minimum=1, maximum=10)
            data = await _request(
                settings,
                "/search/issues",
                params={"q": f"repo:{repo} is:issue {query}", "per_page": str(limit)},
            )
            return success(data)

        if name == "github_get_issue":
            repo = _repo(required_str(arguments, "repo"))
            issue_number = required_int(arguments, "issue_number")
            data = await _request(settings, f"/repos/{repo}/issues/{issue_number}")
            return success(data)

        if name == "github_code_search":
            query = required_str(arguments, "query")
            scope = optional_str(arguments, "scope", "langgenius") or "langgenius"
            language = optional_str(arguments, "language")
            path = optional_str(arguments, "path")
            limit = optional_int(arguments, "limit", 10, minimum=1, maximum=30)
            full_query = _code_query(query, scope=scope, language=language, path=path)
            data = await _request(
                settings,
                "/search/code",
                params={"q": full_query, "per_page": str(limit)},
                text_match=True,
            )
            return success(data)

        return failure("UNKNOWN_TOOL", f"unknown tool: {name}")
    except ValueError as e:
        return failure("BAD_ARGUMENT", str(e))
    except httpx.HTTPStatusError as e:
        body = e.response.text[:500]
        return failure("GITHUB_API_ERROR", f"HTTP {e.response.status_code}: {body}")
    except Exception as e:  # noqa: BLE001
        return failure("INTERNAL_ERROR", str(e))


async def _request(
    settings: Settings,
    path: str,
    *,
    params: dict[str, str] | None = None,
    text_match: bool = False,
) -> dict[str, Any]:
    accept = "application/vnd.github.text-match+json" if text_match else "application/vnd.github+json"
    headers = {"Accept": accept, "X-GitHub-Api-Version": "2022-11-28"}
    if settings.github_token:
        headers["Authorization"] = f"Bearer {settings.github_token}"
    async with httpx.AsyncClient(timeout=settings.github_timeout_seconds, trust_env=False) as client:
        resp = await client.get(f"{GITHUB_API_BASE}{path}", params=params, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    if not isinstance(data, dict):
        raise RuntimeError("GitHub returned non-object JSON")
    return data


def _repo(value: str) -> str:
    if not _REPO_RE.fullmatch(value):
        raise ValueError("repo must be owner/repo")
    return value


def _code_query(query: str, *, scope: str, language: str, path: str) -> str:
    if "/" in scope:
        _repo(scope)
        qualifier = f"repo:{scope}"
    else:
        if not _ORG_RE.fullmatch(scope):
            raise ValueError("scope must be an org or owner/repo")
        qualifier = f"org:{scope}"
    parts = [query, qualifier]
    if language:
        if not _ORG_RE.fullmatch(language):
            raise ValueError("language contains invalid characters")
        parts.append(f"language:{language}")
    if path:
        if path.startswith("/") or ".." in path.split("/"):
            raise ValueError("path must be a repository-relative path")
        parts.append(f"path:{path}")
    return " ".join(parts)
