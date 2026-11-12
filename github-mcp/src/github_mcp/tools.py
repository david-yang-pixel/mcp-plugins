from __future__ import annotations

import re
from typing import Any, Awaitable, Callable
from urllib.parse import quote

import httpx

from . import shaping
from . import write as write_tools
from .mcp import (
    failure,
    optional_bool,
    optional_int,
    optional_str,
    required_int,
    required_str,
    success,
)
from .settings import Settings

GITHUB_API_BASE = "https://api.github.com"
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_ORG_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_GIT_REF_RE = re.compile(r"^[A-Za-z0-9_./:@^~+-]+$")
_VISIBILITIES = ("all", "public", "private")
GITHUB_COMPARE_FILE_CAP = 300

# Test hook: unit tests install an ``httpx.MockTransport`` here.
_TRANSPORT: httpx.AsyncBaseTransport | None = None

_FIELDS_SCHEMA = {
    "type": "array",
    "items": {"type": "string"},
    "description": (
        "Optional field paths to return from the raw GitHub object instead of the default "
        "summary, e.g. [\"head.sha\", \"labels.name\"]. Dot paths map over lists. "
        "Result is keyed by the requested paths."
    ),
}
_RAW_SCHEMA = {
    "type": "boolean",
    "default": False,
    "description": "Return the unmodified GitHub API payload (large; escape hatch only).",
}
_REPO_SCHEMA = {"type": "string", "description": "Repository as owner/name."}


def _page_props(default: int) -> dict[str, Any]:
    return {
        "page": {"type": "integer", "default": 1, "description": "1-based page number."},
        "per_page": {"type": "integer", "default": default, "description": "Items per page (max 100)."},
    }


def _body_prop(default: int, what: str = "body") -> dict[str, Any]:
    return {
        "type": "integer",
        "default": default,
        "description": f"Truncate {what} to this many characters; 0 omits it.",
    }


TOOLS = [
    {
        "name": "github_search_prs",
        "description": (
            "Search GitHub pull requests for known fixes. Returns compact items "
            "(number, title, state, merged_at, labels, body snippet, html_url). When nothing "
            "matches, repo_access tells whether the repository is accessible at all; an "
            "invisible repository fails with code REPO_NOT_ACCESSIBLE plus repo_access."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": _REPO_SCHEMA,
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 5},
                "max_body_chars": _body_prop(300, "each item's body"),
                "fields": _FIELDS_SCHEMA,
                "raw": _RAW_SCHEMA,
            },
            "required": ["repo", "query"],
        },
    },
    {
        "name": "github_get_pr",
        "description": (
            "Read a GitHub pull request by repo and PR number. Returns key fields only "
            "(state, draft/merged, base/head ref+sha+repo, labels, reviewers, size, body, "
            "html_url); use fields or raw for more. Use github_get_pr_files / "
            "github_list_pr_commits for the diff and commits."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": _REPO_SCHEMA,
                "pr_number": {"type": "integer"},
                "max_body_chars": _body_prop(8000),
                "fields": _FIELDS_SCHEMA,
                "raw": _RAW_SCHEMA,
            },
            "required": ["repo", "pr_number"],
        },
    },
    {
        "name": "github_get_pr_files",
        "description": (
            "List files changed by a GitHub pull request (filename, status, additions, "
            "deletions, changes, previous_filename). Optional truncated patch. Paginated; "
            "has_next_page tells whether more files exist (GitHub caps at 3000 files)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": _REPO_SCHEMA,
                "pr_number": {"type": "integer"},
                **_page_props(30),
                "include_patch": {"type": "boolean", "default": False},
                "max_patch_chars": {
                    "type": "integer",
                    "default": 2000,
                    "description": "Truncate each file's patch to this many characters.",
                },
                "fields": _FIELDS_SCHEMA,
                "raw": _RAW_SCHEMA,
            },
            "required": ["repo", "pr_number"],
        },
    },
    {
        "name": "github_list_pr_commits",
        "description": (
            "List commits of a GitHub pull request (sha, first message line, author, date, "
            "html_url). Paginated; GitHub returns at most 250 commits."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": _REPO_SCHEMA,
                "pr_number": {"type": "integer"},
                **_page_props(30),
                "fields": _FIELDS_SCHEMA,
                "raw": _RAW_SCHEMA,
            },
            "required": ["repo", "pr_number"],
        },
    },
    {
        "name": "github_compare",
        "description": (
            "Compare two refs (base...head: branches, tags or SHAs) in a GitHub repository. "
            "Returns status, ahead_by, behind_by, total_commits and truncated compact lists "
            "of commits and files."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": _REPO_SCHEMA,
                "base": {"type": "string", "description": "Base ref (branch, tag or SHA)."},
                "head": {
                    "type": "string",
                    "description": "Head ref (branch, tag, SHA, or owner:branch for forks).",
                },
                "max_commits": {"type": "integer", "default": 50},
                "max_files": {"type": "integer", "default": 100},
                "include_patch": {"type": "boolean", "default": False},
                "max_patch_chars": {"type": "integer", "default": 1000},
                "fields": _FIELDS_SCHEMA,
                "raw": _RAW_SCHEMA,
            },
            "required": ["repo", "base", "head"],
        },
    },
    {
        "name": "github_search_issues",
        "description": (
            "Search GitHub issues for bug reports, regressions, and known limitations. "
            "Returns compact items. When nothing matches, repo_access tells whether the "
            "repository is accessible and whether issues are enabled; an invisible "
            "repository fails with code REPO_NOT_ACCESSIBLE plus repo_access."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": _REPO_SCHEMA,
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 5},
                "max_body_chars": _body_prop(300, "each item's body"),
                "fields": _FIELDS_SCHEMA,
                "raw": _RAW_SCHEMA,
            },
            "required": ["repo", "query"],
        },
    },
    {
        "name": "github_get_issue",
        "description": (
            "Read a GitHub issue (or PR conversation) by repo and number. Returns "
            "data.issue with key fields and, by default, data.comments with the first page "
            "of comments (user, created_at, body, html_url)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": _REPO_SCHEMA,
                "issue_number": {"type": "integer"},
                "include_comments": {"type": "boolean", "default": True},
                "comments_page": {"type": "integer", "default": 1},
                "comments_per_page": {"type": "integer", "default": 30},
                "max_body_chars": _body_prop(8000, "the issue body"),
                "comment_max_body_chars": _body_prop(2000, "each comment body"),
                "fields": _FIELDS_SCHEMA,
                "raw": _RAW_SCHEMA,
            },
            "required": ["repo", "issue_number"],
        },
    },
    {
        "name": "github_list_issue_comments",
        "description": (
            "List comments on a GitHub issue or PR conversation (user, created_at, body, "
            "html_url), oldest first. Paginated. PR review (diff) comments are not included."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": _REPO_SCHEMA,
                "issue_number": {"type": "integer"},
                **_page_props(30),
                "max_body_chars": _body_prop(2000, "each comment body"),
                "fields": _FIELDS_SCHEMA,
                "raw": _RAW_SCHEMA,
            },
            "required": ["repo", "issue_number"],
        },
    },
    {
        "name": "github_list_releases",
        "description": (
            "List GitHub releases of a repository, newest first (tag_name, name, draft, "
            "prerelease, published_at, html_url, truncated body). Paginated."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": _REPO_SCHEMA,
                **_page_props(10),
                "max_body_chars": _body_prop(1000, "each release body"),
                "fields": _FIELDS_SCHEMA,
                "raw": _RAW_SCHEMA,
            },
            "required": ["repo"],
        },
    },
    {
        "name": "github_get_release",
        "description": (
            "Read one GitHub release by tag, or the latest published release when tag is "
            "omitted. Returns key fields, body and asset names/sizes."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": _REPO_SCHEMA,
                "tag": {"type": "string", "description": "Release tag, e.g. 1.9.0. Omit for latest."},
                "max_body_chars": _body_prop(8000),
                "fields": _FIELDS_SCHEMA,
                "raw": _RAW_SCHEMA,
            },
            "required": ["repo"],
        },
    },
    {
        "name": "github_code_search",
        "description": (
            "Search GitHub code without local clones (default branch only). Narrow with "
            "repo (owner/name, takes precedence over scope), scope (org or owner/repo), "
            "path and language. Results can include private repositories visible to the "
            "token: every item carries repository.full_name and repository.private; use "
            "visibility to filter. A repo-scoped search on an invisible repository fails "
            "with code REPO_NOT_ACCESSIBLE plus repo_access. For version- or tag-specific "
            "Dify source, keep using dify-source."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "repo": {
                    "type": "string",
                    "description": "Optional owner/name; adds a repo: qualifier instead of scope.",
                },
                "scope": {"type": "string", "default": "langgenius"},
                "language": {"type": "string"},
                "path": {"type": "string"},
                "visibility": {
                    "type": "string",
                    "enum": list(_VISIBILITIES),
                    "default": "all",
                    "description": "Filter results by repository visibility.",
                },
                "limit": {"type": "integer", "default": 10},
                "fields": _FIELDS_SCHEMA,
                "raw": _RAW_SCHEMA,
            },
            "required": ["query"],
        },
    },
    {
        "name": "github_get_file",
        "description": (
            "Read one file (or list one directory) from a repository at a branch, tag or SHA "
            "(default: the default branch). Text is returned decoded and truncated to "
            "max_chars; use sha with github_commit_files' expected_head_sha workflow."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": _REPO_SCHEMA,
                "path": {"type": "string", "description": "Repository-relative path; empty for the root."},
                "ref": {"type": "string", "description": "Branch, tag or commit SHA."},
                "max_chars": {
                    "type": "integer",
                    "default": 20000,
                    "description": "Truncate decoded text to this many characters.",
                },
            },
            "required": ["repo"],
        },
    },
]

TOOLS = TOOLS + write_tools.TOOLS
WRITE_TOOL_NAMES = write_tools.TOOL_NAMES


class _Output:
    """How to render GitHub objects: default summary, selected fields, or raw."""

    def __init__(self, arguments: dict[str, Any]) -> None:
        self.raw = optional_bool(arguments, "raw", False)
        self.fields = shaping.parse_fields(arguments.get("fields"))
        if self.raw and self.fields:
            raise ValueError("use either fields or raw, not both")

    @property
    def mode(self) -> str:
        return "raw" if self.raw else ("fields" if self.fields else "summary")

    def render(self, obj: Any, shaper: Callable[[dict[str, Any]], dict[str, Any]]) -> Any:
        if self.raw or not isinstance(obj, dict):
            return obj
        if self.fields:
            return shaping.select_fields(obj, self.fields)
        return shaper(obj)

    def render_list(self, items: Any, shaper: Callable[[dict[str, Any]], dict[str, Any]]) -> list[Any]:
        if not isinstance(items, list):
            return []
        return [self.render(item, shaper) for item in items]


async def handle(name: str, arguments: dict[str, Any], settings: Settings) -> dict[str, Any]:
    handler = _HANDLERS.get(name)
    if handler is None:
        return failure("UNKNOWN_TOOL", f"unknown tool: {name}")
    if name in WRITE_TOOL_NAMES and not getattr(settings, "write_active", False):
        return failure("WRITE_DISABLED", "write tools are disabled on this server")
    try:
        return await handler(arguments, settings)
    except write_tools.WriteDenied as e:
        return failure(e.code, str(e))
    except ValueError as e:
        return failure("BAD_ARGUMENT", str(e))
    except httpx.HTTPStatusError as e:
        body = e.response.text[:500]
        return failure("GITHUB_API_ERROR", f"HTTP {e.response.status_code}: {body}")
    except Exception as e:  # noqa: BLE001
        return failure("INTERNAL_ERROR", str(e))


async def _search_prs(arguments: dict[str, Any], settings: Settings) -> dict[str, Any]:
    return await _search_issue_like(arguments, settings, kind="pr")


async def _search_issues(arguments: dict[str, Any], settings: Settings) -> dict[str, Any]:
    return await _search_issue_like(arguments, settings, kind="issue")


async def _search_issue_like(
    arguments: dict[str, Any], settings: Settings, *, kind: str
) -> dict[str, Any]:
    repo = _repo(required_str(arguments, "repo"))
    query = required_str(arguments, "query")
    limit = optional_int(arguments, "limit", 5, minimum=1, maximum=10)
    max_body = _body_limit(arguments, "max_body_chars", 300)
    out = _Output(arguments)
    full_query = f"repo:{repo} is:{kind} {query}"
    try:
        data = await _request(
            settings, "/search/issues", params={"q": full_query, "per_page": str(limit)}
        )
    except httpx.HTTPStatusError as e:
        explained = await _search_error_repo_access(e, settings, repo, purpose=kind)
        if explained is None:
            raise
        return explained
    if out.raw:
        return success(data)
    items = data.get("items") or []
    result: dict[str, Any] = {
        "query": full_query,
        "total_count": data.get("total_count"),
        "incomplete_results": data.get("incomplete_results"),
        "items": out.render_list(
            items, lambda item: shaping.shape_search_issue(item, max_body_chars=max_body)
        ),
    }
    if not items:
        result["repo_access"] = await _repo_access(settings, repo, purpose=kind)
    return success(result)


async def _get_pr(arguments: dict[str, Any], settings: Settings) -> dict[str, Any]:
    repo = _repo(required_str(arguments, "repo"))
    pr_number = required_int(arguments, "pr_number")
    max_body = _body_limit(arguments, "max_body_chars", 8000)
    out = _Output(arguments)
    data = await _request(settings, f"/repos/{repo}/pulls/{pr_number}")
    return success(out.render(data, lambda raw: shaping.shape_pr(raw, max_body_chars=max_body)))


async def _get_pr_files(arguments: dict[str, Any], settings: Settings) -> dict[str, Any]:
    repo = _repo(required_str(arguments, "repo"))
    pr_number = required_int(arguments, "pr_number")
    page, per_page = _pagination(arguments, 30)
    include_patch = optional_bool(arguments, "include_patch", False)
    max_patch = optional_int(arguments, "max_patch_chars", 2000, minimum=1, maximum=50_000)
    out = _Output(arguments)
    items, has_next = await _request_list(
        settings,
        f"/repos/{repo}/pulls/{pr_number}/files",
        params={"page": str(page), "per_page": str(per_page)},
    )
    files = out.render_list(
        items,
        lambda raw: shaping.shape_file(raw, include_patch=include_patch, max_patch_chars=max_patch),
    )
    return success(
        {
            "pr_number": pr_number,
            "page": page,
            "per_page": per_page,
            "has_next_page": has_next,
            "count": len(files),
            "files": files,
        }
    )


async def _list_pr_commits(arguments: dict[str, Any], settings: Settings) -> dict[str, Any]:
    repo = _repo(required_str(arguments, "repo"))
    pr_number = required_int(arguments, "pr_number")
    page, per_page = _pagination(arguments, 30)
    out = _Output(arguments)
    items, has_next = await _request_list(
        settings,
        f"/repos/{repo}/pulls/{pr_number}/commits",
        params={"page": str(page), "per_page": str(per_page)},
    )
    commits = out.render_list(items, shaping.shape_commit)
    return success(
        {
            "pr_number": pr_number,
            "page": page,
            "per_page": per_page,
            "has_next_page": has_next,
            "count": len(commits),
            "commits": commits,
        }
    )


async def _compare(arguments: dict[str, Any], settings: Settings) -> dict[str, Any]:
    repo = _repo(required_str(arguments, "repo"))
    base = _git_ref(required_str(arguments, "base"), "base")
    head = _git_ref(required_str(arguments, "head"), "head")
    max_commits = optional_int(arguments, "max_commits", 50, minimum=0, maximum=250)
    max_files = optional_int(arguments, "max_files", 100, minimum=0, maximum=GITHUB_COMPARE_FILE_CAP)
    include_patch = optional_bool(arguments, "include_patch", False)
    max_patch = optional_int(arguments, "max_patch_chars", 1000, minimum=1, maximum=50_000)
    out = _Output(arguments)
    basehead = quote(f"{base}...{head}", safe="/:@^~+")
    data = await _request(settings, f"/repos/{repo}/compare/{basehead}")
    if out.mode != "summary":
        return success(out.render(data, lambda raw: raw))

    commits = data.get("commits") if isinstance(data.get("commits"), list) else []
    files = data.get("files") if isinstance(data.get("files"), list) else []
    result: dict[str, Any] = {
        "base": base,
        "head": head,
        "status": data.get("status"),
        "ahead_by": data.get("ahead_by"),
        "behind_by": data.get("behind_by"),
        "total_commits": data.get("total_commits"),
        "merge_base_sha": (data.get("merge_base_commit") or {}).get("sha"),
        "html_url": data.get("html_url"),
        "commits": [shaping.shape_commit(c) for c in commits[:max_commits] if isinstance(c, dict)],
        "commits_returned_by_github": len(commits),
        "commits_omitted": max(0, len(commits) - max_commits),
        "files": [
            shaping.shape_file(f, include_patch=include_patch, max_patch_chars=max_patch)
            for f in files[:max_files]
            if isinstance(f, dict)
        ],
        "files_returned_by_github": len(files),
        "files_omitted": max(0, len(files) - max_files),
    }
    notes = []
    total_commits = data.get("total_commits")
    if isinstance(total_commits, int) and total_commits > len(commits):
        notes.append(
            f"GitHub listed {len(commits)} of {total_commits} commits (oldest first); "
            "use github_list_pr_commits or narrower refs for the rest."
        )
    if len(files) >= GITHUB_COMPARE_FILE_CAP:
        notes.append(f"GitHub returns at most {GITHUB_COMPARE_FILE_CAP} files for a comparison.")
    if notes:
        result["notes"] = notes
    return success(result)


async def _get_issue(arguments: dict[str, Any], settings: Settings) -> dict[str, Any]:
    repo = _repo(required_str(arguments, "repo"))
    issue_number = required_int(arguments, "issue_number")
    include_comments = optional_bool(arguments, "include_comments", True)
    comments_page = optional_int(arguments, "comments_page", 1, minimum=1, maximum=1000)
    comments_per_page = optional_int(arguments, "comments_per_page", 30, minimum=1, maximum=100)
    max_body = _body_limit(arguments, "max_body_chars", 8000)
    comment_max_body = _body_limit(arguments, "comment_max_body_chars", 2000)
    out = _Output(arguments)
    data = await _request(settings, f"/repos/{repo}/issues/{issue_number}")
    result: dict[str, Any] = {
        "issue": out.render(data, lambda raw: shaping.shape_issue(raw, max_body_chars=max_body)),
    }
    if include_comments:
        total = data.get("comments") if isinstance(data.get("comments"), int) else 0
        comments: dict[str, Any] = {
            "total": total,
            "page": comments_page,
            "per_page": comments_per_page,
            "has_next_page": False,
            "items": [],
        }
        if total:
            try:
                items, has_next = await _request_list(
                    settings,
                    f"/repos/{repo}/issues/{issue_number}/comments",
                    params={"page": str(comments_page), "per_page": str(comments_per_page)},
                )
            except Exception as e:  # noqa: BLE001 - comments must not break the issue read
                comments["error"] = _describe_error(e)
            else:
                comments["has_next_page"] = has_next
                comments["items"] = _render_comments(out, items, comment_max_body)
        result["comments"] = comments
    return success(result)


async def _list_issue_comments(arguments: dict[str, Any], settings: Settings) -> dict[str, Any]:
    repo = _repo(required_str(arguments, "repo"))
    issue_number = required_int(arguments, "issue_number")
    page, per_page = _pagination(arguments, 30)
    max_body = _body_limit(arguments, "max_body_chars", 2000)
    out = _Output(arguments)
    items, has_next = await _request_list(
        settings,
        f"/repos/{repo}/issues/{issue_number}/comments",
        params={"page": str(page), "per_page": str(per_page)},
    )
    rendered = _render_comments(out, items, max_body)
    return success(
        {
            "issue_number": issue_number,
            "page": page,
            "per_page": per_page,
            "has_next_page": has_next,
            "count": len(rendered),
            "comments": rendered,
        }
    )


def _render_comments(out: _Output, items: list[Any], max_body: int) -> list[Any]:
    return out.render_list(items, lambda raw: shaping.shape_comment(raw, max_body_chars=max_body))


async def _list_releases(arguments: dict[str, Any], settings: Settings) -> dict[str, Any]:
    repo = _repo(required_str(arguments, "repo"))
    page, per_page = _pagination(arguments, 10)
    max_body = _body_limit(arguments, "max_body_chars", 1000)
    out = _Output(arguments)
    items, has_next = await _request_list(
        settings,
        f"/repos/{repo}/releases",
        params={"page": str(page), "per_page": str(per_page)},
    )
    releases = out.render_list(items, lambda raw: shaping.shape_release(raw, max_body_chars=max_body))
    return success(
        {
            "page": page,
            "per_page": per_page,
            "has_next_page": has_next,
            "count": len(releases),
            "releases": releases,
        }
    )


async def _get_release(arguments: dict[str, Any], settings: Settings) -> dict[str, Any]:
    repo = _repo(required_str(arguments, "repo"))
    tag = optional_str(arguments, "tag")
    max_body = _body_limit(arguments, "max_body_chars", 8000)
    out = _Output(arguments)
    if tag:
        path = f"/repos/{repo}/releases/tags/{quote(_git_ref(tag, 'tag'), safe='')}"
    else:
        path = f"/repos/{repo}/releases/latest"
    data = await _request(settings, path)
    return success(
        out.render(data, lambda raw: shaping.shape_release_detail(raw, max_body_chars=max_body))
    )


async def _code_search(arguments: dict[str, Any], settings: Settings) -> dict[str, Any]:
    query = required_str(arguments, "query")
    repo = optional_str(arguments, "repo")
    if repo:
        _repo(repo)
    scope = optional_str(arguments, "scope", "langgenius") or "langgenius"
    language = optional_str(arguments, "language")
    path = optional_str(arguments, "path")
    visibility = (optional_str(arguments, "visibility", "all") or "all").lower()
    if visibility not in _VISIBILITIES:
        raise ValueError("visibility must be one of: all, public, private")
    limit = optional_int(arguments, "limit", 10, minimum=1, maximum=30)
    out = _Output(arguments)
    full_query = _code_query(query, scope=scope, language=language, path=path, repo=repo)
    probe_repo = repo or (scope if "/" in scope else "")
    try:
        data = await _request(
            settings,
            "/search/code",
            params={"q": full_query, "per_page": str(limit)},
            text_match=True,
        )
    except httpx.HTTPStatusError as e:
        explained = await _search_error_repo_access(e, settings, probe_repo, purpose="code")
        if explained is None:
            raise
        return explained
    raw_items = [item for item in data.get("items") or [] if isinstance(item, dict)]
    kept = [item for item in raw_items if _visibility_matches(item, visibility)]
    if out.raw:
        result: dict[str, Any] = dict(data)
        result["items"] = kept
    else:
        result = {
            "query": full_query,
            "total_count": data.get("total_count"),
            "incomplete_results": data.get("incomplete_results"),
            "items": [_code_item(item, out) for item in kept],
        }
    result["visibility"] = visibility
    if visibility != "all":
        result["filtered_out"] = len(raw_items) - len(kept)
        if result["filtered_out"]:
            result["filter_note"] = (
                f"{result['filtered_out']} of {len(raw_items)} returned results were hidden "
                f"by visibility={visibility}."
            )
    if probe_repo and not raw_items:
        result["repo_access"] = await _repo_access(settings, probe_repo, purpose="code")
    return success(result)


def _code_item(item: dict[str, Any], out: _Output) -> dict[str, Any]:
    if not out.fields:
        return shaping.shape_code_item(item)
    # Keep the visibility marker even when explicit fields are requested.
    selected = shaping.select_fields(item, out.fields)
    selected.setdefault("repository.full_name", shaping.pick(item, ["repository", "full_name"]))
    selected.setdefault("repository.private", shaping.pick(item, ["repository", "private"]))
    return selected


def _visibility_matches(item: dict[str, Any], visibility: str) -> bool:
    if visibility == "all":
        return True
    private = (item.get("repository") or {}).get("private")
    if not isinstance(private, bool):
        return False
    return private if visibility == "private" else not private


async def _search_error_repo_access(
    error: httpx.HTTPStatusError, settings: Settings, repo: str, *, purpose: str
) -> dict[str, Any] | None:
    """Turn a repo-scoped search HTTP 422 into REPO_NOT_ACCESSIBLE when the repo is invisible.

    GitHub search answers 422 (not an empty result) when a ``repo:`` qualifier names a
    repository that does not exist or the token cannot see. Returns None when the error
    has another cause (e.g. invalid query syntax on an accessible repo, or an
    inconclusive probe); the caller then keeps the original error.
    """
    if not repo or error.response.status_code != 422:
        return None
    access = await _repo_access(settings, repo, purpose=purpose)
    if access.get("accessible") is not False:
        return None
    payload = failure(
        "REPO_NOT_ACCESSIBLE",
        f"Repository {repo} cannot be searched: it does not exist or is not accessible to "
        f"the configured token (search returned HTTP 422; repository probe returned HTTP "
        f"{access.get('http_status')}). See repo_access.",
    )
    payload["repo_access"] = access
    return payload


async def _get_file(arguments: dict[str, Any], settings: Settings) -> dict[str, Any]:
    repo = _repo(required_str(arguments, "repo"))
    path = optional_str(arguments, "path").strip("/")
    if path and (".." in path.split("/") or any(c in path for c in "\0\r\n")):
        raise ValueError("path must be a repository-relative path")
    ref = optional_str(arguments, "ref")
    if ref:
        _git_ref(ref, "ref")
    max_chars = optional_int(arguments, "max_chars", 20000, minimum=1, maximum=200_000)
    params = {"ref": ref} if ref else None
    resp = await _get(settings, f"/repos/{repo}/contents/{quote(path)}", params=params)
    data = resp.json()
    if isinstance(data, list):
        entries = [
            {"name": e.get("name"), "path": e.get("path"), "type": e.get("type"), "size": e.get("size"), "sha": e.get("sha")}
            for e in data
            if isinstance(e, dict)
        ]
        return success({"type": "dir", "path": path, "ref": ref or None, "entries": entries})
    if not isinstance(data, dict):
        raise RuntimeError("GitHub returned unexpected JSON")
    result: dict[str, Any] = {
        "type": data.get("type"),
        "path": data.get("path"),
        "ref": ref or None,
        "sha": data.get("sha"),
        "size": data.get("size"),
        "html_url": data.get("html_url"),
    }
    if data.get("type") == "file" and data.get("encoding") == "base64":
        import base64

        raw = base64.b64decode(data.get("content") or "")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            result["binary"] = True
            return success(result)
        result["content"] = text[:max_chars]
        if len(text) > max_chars:
            result["content_truncated"] = True
            result["content_chars"] = len(text)
    elif data.get("type") == "file":
        result["note"] = "File too large for the contents API (over 1 MB); content not returned."
    return success(result)


def _rate_limited(response: httpx.Response) -> bool:
    """GitHub signals primary and secondary rate limits on 403 with these headers."""
    return response.headers.get("x-ratelimit-remaining") == "0" or "retry-after" in response.headers


async def _repo_access(settings: Settings, repo: str, *, purpose: str) -> dict[str, Any]:
    """Explain a zero-result search: missing/inaccessible repo vs. genuinely no match.

    Never raises; a failed probe is reported inside the result.
    """
    try:
        data = await _request(settings, f"/repos/{repo}")
    except httpx.HTTPStatusError as e:
        status = e.response.status_code
        if status == 404:
            note = (
                "Repository not found or not visible to the configured token; zero results "
                "may mean no access rather than no matches."
            )
        elif status in (401, 403) and not _rate_limited(e.response):
            note = "Token was rejected or lacks permission for this repository."
        else:
            # 5xx, 429 or a rate-limited 403 says nothing about access.
            return {
                "repo": repo,
                "accessible": None,
                "http_status": status,
                "note": "Repository probe failed temporarily; results are inconclusive.",
            }
        return {"repo": repo, "accessible": False, "http_status": status, "note": note}
    except Exception as e:  # noqa: BLE001
        return {
            "repo": repo,
            "accessible": None,
            "http_status": None,
            "error": _describe_error(e),
            "note": "Repository probe failed; zero results are inconclusive.",
        }

    has_issues = data.get("has_issues")
    archived = data.get("archived")
    access: dict[str, Any] = {
        "repo": data.get("full_name") or repo,
        "accessible": True,
        "http_status": 200,
        "private": data.get("private"),
        "visibility": data.get("visibility"),
        "archived": archived,
        "has_issues": has_issues,
        "default_branch": data.get("default_branch"),
    }
    if purpose == "issue":
        access["issues_enabled"] = has_issues
        if has_issues is False:
            access["note"] = (
                "Issues are disabled for this repository; zero results do not mean the "
                "problem was never reported elsewhere."
            )
        else:
            access["note"] = "Repository is accessible and issues are enabled; no issue matched."
    elif purpose == "code":
        access["note"] = (
            "Repository is accessible; no indexed match. GitHub code search only covers the "
            "default branch and skips large or vendored files."
        )
    else:
        access["note"] = "Repository is accessible; no pull request matched."
    if archived:
        access["note"] += " Repository is archived."
    return access


def _describe_error(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        return f"HTTP {e.response.status_code}: {e.response.text[:200]}"
    return f"{type(e).__name__}: {str(e)[:200]}"


async def _get(
    settings: Settings,
    path: str,
    *,
    params: dict[str, str] | None = None,
    text_match: bool = False,
) -> httpx.Response:
    accept = "application/vnd.github.text-match+json" if text_match else "application/vnd.github+json"
    headers = {"Accept": accept, "X-GitHub-Api-Version": "2022-11-28"}
    if settings.github_token:
        headers["Authorization"] = f"Bearer {settings.github_token}"
    async with httpx.AsyncClient(
        timeout=settings.github_timeout_seconds, trust_env=False, transport=_TRANSPORT
    ) as client:
        resp = await client.get(f"{GITHUB_API_BASE}{path}", params=params, headers=headers)
        resp.raise_for_status()
        return resp


async def _request(
    settings: Settings,
    path: str,
    *,
    params: dict[str, str] | None = None,
    text_match: bool = False,
) -> dict[str, Any]:
    resp = await _get(settings, path, params=params, text_match=text_match)
    data = resp.json()
    if not isinstance(data, dict):
        raise RuntimeError("GitHub returned non-object JSON")
    return data


async def _request_list(
    settings: Settings,
    path: str,
    *,
    params: dict[str, str] | None = None,
) -> tuple[list[Any], bool]:
    """GET a paginated list endpoint; returns (items, has_next_page)."""
    resp = await _get(settings, path, params=params)
    data = resp.json()
    if not isinstance(data, list):
        raise RuntimeError("GitHub returned non-list JSON")
    return data, "next" in resp.links


def _pagination(arguments: dict[str, Any], default_per_page: int) -> tuple[int, int]:
    page = optional_int(arguments, "page", 1, minimum=1, maximum=1000)
    per_page = optional_int(arguments, "per_page", default_per_page, minimum=1, maximum=100)
    return page, per_page


def _body_limit(arguments: dict[str, Any], name: str, default: int) -> int:
    return optional_int(arguments, name, default, minimum=0, maximum=100_000)


def _repo(value: str) -> str:
    if not _REPO_RE.fullmatch(value):
        raise ValueError("repo must be owner/repo")
    return value


def _git_ref(value: str, name: str) -> str:
    if not _GIT_REF_RE.fullmatch(value) or ".." in value:
        raise ValueError(f"{name} must be a branch, tag or commit SHA")
    return value


def _code_query(query: str, *, scope: str, language: str, path: str, repo: str = "") -> str:
    if repo:
        qualifier = f"repo:{_repo(repo)}"
    elif "/" in scope:
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


_HANDLERS: dict[str, Callable[[dict[str, Any], Settings], Awaitable[dict[str, Any]]]] = {
    "github_search_prs": _search_prs,
    "github_get_pr": _get_pr,
    "github_get_pr_files": _get_pr_files,
    "github_list_pr_commits": _list_pr_commits,
    "github_compare": _compare,
    "github_search_issues": _search_issues,
    "github_get_issue": _get_issue,
    "github_list_issue_comments": _list_issue_comments,
    "github_list_releases": _list_releases,
    "github_get_release": _get_release,
    "github_code_search": _code_search,
    "github_get_file": _get_file,
    **write_tools.HANDLERS,
}
