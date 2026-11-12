"""Write tools: fork, create repository, create branch, commit, open PR/issue, comment.

Every write goes through the same gates, in this order, before any HTTP request:

1. ``app.py`` only lists/allows these tools for the write API key and when
   ``settings.write_active`` is true (``handle`` re-checks ``write_active``).
2. Argument validation.
3. Repository allowlists: code (repos, branches, commits, fork targets) only ever
   lands under ``GITHUB_MCP_WRITE_OWNERS``; issues, PRs and comments may also target
   repos listed in ``GITHUB_MCP_UPSTREAM_REPOS``.
4. A per-process hourly write budget (``GITHUB_MCP_WRITE_RATE_PER_HOUR``).

Requests use the write token only and never fall back to the read token. No tool
deletes repositories or branches, and commits never force-push.
"""
from __future__ import annotations

import base64
import binascii
import re
import time
from collections import deque
from typing import Any, Awaitable, Callable

import httpx

from .mcp import (
    failure,
    optional_bool,
    optional_str,
    required_int,
    required_str,
    success,
)

GITHUB_API_BASE = "https://api.github.com"

# Test hook: unit tests install an ``httpx.MockTransport`` here.
_TRANSPORT: httpx.AsyncBaseTransport | None = None

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9_./-]{1,200}$")
_REF_RE = re.compile(r"^[A-Za-z0-9_./@^~+-]{1,200}$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

MAX_FILES = 50
MAX_FILE_BYTES = 1_000_000
MAX_TOTAL_BYTES = 5_000_000
MAX_BODY_CHARS = 65_536
MAX_TITLE_CHARS = 256
MAX_MESSAGE_CHARS = 10_000
MAX_LIST_ITEMS = 20


class WriteDenied(Exception):
    """A gate refused the write before anything was sent to GitHub."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------- schemas

_REPO = {"type": "string", "description": "Repository as owner/name."}
_BODY = {"type": "string", "description": f"Markdown body (max {MAX_BODY_CHARS} characters)."}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "github_fork_repo",
        "write": True,
        "description": (
            "Fork a repository into your account (or an allowed organization). Forking is "
            "asynchronous: the result has ready=false and the fork may take a few seconds "
            "before branches or commits can be created. Forking an already-forked repo "
            "returns the existing fork. The destination owner must be in GITHUB_MCP_WRITE_OWNERS."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {**_REPO, "description": "Source repository to fork, as owner/name."},
                "organization": {
                    "type": "string",
                    "description": "Fork into this organization instead of your own account.",
                },
                "name": {"type": "string", "description": "New name for the fork."},
                "default_branch_only": {"type": "boolean", "default": False},
            },
            "required": ["repo"],
        },
        "annotations": {"idempotentHint": True},
    },
    {
        "name": "github_create_repo",
        "write": True,
        "description": (
            "Create a new repository in your account, or in an organization via org. The "
            "owner must be in GITHUB_MCP_WRITE_OWNERS. Public by default."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "org": {"type": "string", "description": "Organization login; omit for your own account."},
                "description": {"type": "string"},
                "private": {"type": "boolean", "default": False},
                "auto_init": {
                    "type": "boolean",
                    "default": False,
                    "description": "Create an initial commit with a README so branches and commits work immediately.",
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "github_create_branch",
        "write": True,
        "description": (
            "Create a branch from a branch, tag or commit SHA (default: the repository's "
            "default branch). The repository owner must be in GITHUB_MCP_WRITE_OWNERS. "
            "Fails with BRANCH_EXISTS if the branch already exists."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": _REPO,
                "branch": {"type": "string", "description": "New branch name."},
                "from_ref": {"type": "string", "description": "Branch, tag or SHA to start from."},
            },
            "required": ["repo", "branch"],
        },
    },
    {
        "name": "github_commit_files",
        "write": True,
        "description": (
            "Create one commit on an existing branch that adds/updates files and/or deletes "
            "paths, atomically, via the Git Data API. Never force-pushes: if the branch moved "
            "(or does not match expected_head_sha) it fails with HEAD_MOVED and nothing is "
            f"overwritten. Limits: {MAX_FILES} paths, {MAX_FILE_BYTES} bytes per file, "
            f"{MAX_TOTAL_BYTES} bytes total. The repository owner must be in GITHUB_MCP_WRITE_OWNERS."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": _REPO,
                "branch": {"type": "string"},
                "message": {"type": "string", "description": "Commit message."},
                "files": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "Repository-relative path."},
                            "content": {"type": "string"},
                            "encoding": {"type": "string", "enum": ["utf-8", "base64"], "default": "utf-8"},
                            "executable": {"type": "boolean", "default": False},
                        },
                        "required": ["path", "content"],
                    },
                },
                "delete_paths": {"type": "array", "items": {"type": "string"}},
                "expected_head_sha": {
                    "type": "string",
                    "description": "Optional: only commit if the branch is still at this SHA.",
                },
            },
            "required": ["repo", "branch", "message"],
        },
        "annotations": {"destructiveHint": True},
    },
    {
        "name": "github_create_pr",
        "write": True,
        "description": (
            "Open a pull request. repo is the target (base) repository; head is a branch "
            "in it, or owner:branch for a branch in a fork. base defaults to the target's "
            "default branch. The target must be your own (GITHUB_MCP_WRITE_OWNERS) or listed "
            "in GITHUB_MCP_UPSTREAM_REPOS."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": _REPO,
                "title": {"type": "string"},
                "head": {"type": "string", "description": "branch, or owner:branch for a fork."},
                "base": {"type": "string"},
                "body": _BODY,
                "draft": {"type": "boolean", "default": False},
                "maintainer_can_modify": {"type": "boolean", "default": True},
            },
            "required": ["repo", "title", "head"],
        },
    },
    {
        "name": "github_create_issue",
        "write": True,
        "description": (
            "Open an issue. The repository must be your own (GITHUB_MCP_WRITE_OWNERS) or "
            "listed in GITHUB_MCP_UPSTREAM_REPOS."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": _REPO,
                "title": {"type": "string"},
                "body": _BODY,
                "labels": {"type": "array", "items": {"type": "string"}},
                "assignees": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["repo", "title"],
        },
    },
    {
        "name": "github_add_comment",
        "write": True,
        "description": (
            "Comment on an issue or on a pull request's conversation (not inline review "
            "comments). The repository must be your own (GITHUB_MCP_WRITE_OWNERS) or listed "
            "in GITHUB_MCP_UPSTREAM_REPOS."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": _REPO,
                "issue_number": {"type": "integer", "description": "Issue or PR number."},
                "body": _BODY,
            },
            "required": ["repo", "issue_number", "body"],
        },
    },
]

TOOL_NAMES = frozenset(tool["name"] for tool in TOOLS)


# ---------------------------------------------------------------- gates


def _csv(value: str) -> list[str]:
    return [item.strip().lower() for item in (value or "").split(",") if item.strip()]


def _write_owners(settings: Any) -> list[str]:
    return _csv(getattr(settings, "github_write_owners", ""))


def _check_owner(settings: Any, owner: str) -> None:
    """Repositories, branches, commits and fork targets: own accounts only."""
    if owner.lower() not in _write_owners(settings):
        raise WriteDenied(
            "REPO_NOT_ALLOWED",
            f"{owner} is not in GITHUB_MCP_WRITE_OWNERS; code is only written under those owners",
        )


def _check_interact(settings: Any, repo: str) -> None:
    """Issues, PRs and comments: own accounts plus the upstream allowlist."""
    owner = repo.split("/", 1)[0].lower()
    if owner in _write_owners(settings):
        return
    for rule in _csv(getattr(settings, "github_upstream_repos", "")):
        if rule == repo.lower() or (rule.endswith("/*") and rule[:-2] == owner):
            return
    raise WriteDenied(
        "REPO_NOT_ALLOWED",
        f"{repo} is not in GITHUB_MCP_WRITE_OWNERS or GITHUB_MCP_UPSTREAM_REPOS",
    )


_WRITE_TIMES: deque[float] = deque()


def _take_rate_slot(settings: Any) -> None:
    limit = int(getattr(settings, "github_write_rate_per_hour", 30))
    now = time.monotonic()
    while _WRITE_TIMES and now - _WRITE_TIMES[0] > 3600:
        _WRITE_TIMES.popleft()
    if len(_WRITE_TIMES) >= limit:
        raise WriteDenied("RATE_LIMITED", f"write limit of {limit} per hour reached")
    _WRITE_TIMES.append(now)


def reset_rate_limit() -> None:
    """Test helper."""
    _WRITE_TIMES.clear()


# ---------------------------------------------------------------- validation


def _repo(value: str) -> str:
    if not _REPO_RE.fullmatch(value):
        raise ValueError("repo must be owner/repo")
    return value


def _name(value: str, field: str) -> str:
    if not _NAME_RE.fullmatch(value):
        raise ValueError(f"{field} contains invalid characters")
    return value


def _branch(value: str, field: str = "branch") -> str:
    if (
        not _BRANCH_RE.fullmatch(value)
        or ".." in value
        or value.startswith(("/", "-", "."))
        or value.endswith(("/", ".lock", "."))
        or "//" in value
    ):
        raise ValueError(f"{field} is not a valid branch name")
    return value


def _ref(value: str, field: str) -> str:
    if not _REF_RE.fullmatch(value) or ".." in value:
        raise ValueError(f"{field} must be a branch, tag or commit SHA")
    return value


def _path(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise ValueError("file path must be a non-empty string")
    parts = value.split("/")
    if value.startswith("/") or any(p in ("", ".", "..") for p in parts) or parts[0] == ".git":
        raise ValueError(f"invalid repository path: {value}")
    if any(c in value for c in "\0\r\n"):
        raise ValueError(f"invalid repository path: {value}")
    return value


def _text(arguments: dict[str, Any], name: str, *, required: bool, limit: int) -> str:
    value = required_str(arguments, name) if required else optional_str(arguments, name)
    if len(value) > limit:
        raise ValueError(f"{name} exceeds {limit} characters")
    return value


def _str_list(arguments: dict[str, Any], name: str) -> list[str]:
    value = arguments.get(name) or []
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise ValueError(f"{name} must be a list of strings")
    if len(value) > MAX_LIST_ITEMS:
        raise ValueError(f"{name} accepts at most {MAX_LIST_ITEMS} items")
    return [v.strip() for v in value]


# ---------------------------------------------------------------- HTTP


async def _send(
    settings: Any,
    method: str,
    path: str,
    *,
    json: dict[str, Any] | None = None,
) -> Any:
    """Call the GitHub API with the WRITE token. Never falls back to the read token."""
    token = getattr(settings, "github_write_token", "")
    if not token:
        raise WriteDenied("WRITE_DISABLED", "write token is not configured")
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Authorization": f"Bearer {token}",
    }
    async with httpx.AsyncClient(
        timeout=getattr(settings, "github_timeout_seconds", 30.0),
        trust_env=False,
        transport=_TRANSPORT,
    ) as client:
        resp = await client.request(method, f"{GITHUB_API_BASE}{path}", json=json, headers=headers)
        resp.raise_for_status()
        return resp.json() if resp.content else {}


async def _login(settings: Any) -> str:
    """Login of the account that owns the write token."""
    data = await _send(settings, "GET", "/user")
    login = data.get("login") if isinstance(data, dict) else None
    if not login:
        raise RuntimeError("could not resolve the write token's account")
    return str(login)


async def _default_branch(settings: Any, repo: str) -> str:
    data = await _send(settings, "GET", f"/repos/{repo}")
    branch = data.get("default_branch") if isinstance(data, dict) else None
    if not branch:
        raise RuntimeError(f"could not resolve the default branch of {repo}")
    return str(branch)


def _status(e: httpx.HTTPStatusError) -> int:
    return e.response.status_code


# ---------------------------------------------------------------- handlers


async def _fork_repo(arguments: dict[str, Any], settings: Any) -> dict[str, Any]:
    repo = _repo(required_str(arguments, "repo"))
    organization = optional_str(arguments, "organization")
    name = optional_str(arguments, "name")
    if organization:
        _name(organization, "organization")
    if name:
        _name(name, "name")
    default_branch_only = optional_bool(arguments, "default_branch_only", False)

    destination = organization or await _login(settings)
    _check_owner(settings, destination)
    _take_rate_slot(settings)

    body: dict[str, Any] = {"default_branch_only": default_branch_only}
    if organization:
        body["organization"] = organization
    if name:
        body["name"] = name
    data = await _send(settings, "POST", f"/repos/{repo}/forks", json=body)
    return success(
        {
            "full_name": data.get("full_name"),
            "html_url": data.get("html_url"),
            "default_branch": data.get("default_branch"),
            "source": repo,
            "ready": False,
            "note": "GitHub creates forks asynchronously; wait a few seconds before branching or committing.",
        }
    )


async def _create_repo(arguments: dict[str, Any], settings: Any) -> dict[str, Any]:
    name = _name(required_str(arguments, "name"), "name")
    org = optional_str(arguments, "org")
    if org:
        _name(org, "org")
    description = _text(arguments, "description", required=False, limit=350)
    private = optional_bool(arguments, "private", False)
    auto_init = optional_bool(arguments, "auto_init", False)

    owner = org or await _login(settings)
    _check_owner(settings, owner)
    _take_rate_slot(settings)

    body = {"name": name, "description": description, "private": private, "auto_init": auto_init}
    path = f"/orgs/{org}/repos" if org else "/user/repos"
    try:
        data = await _send(settings, "POST", path, json=body)
    except httpx.HTTPStatusError as e:
        if _status(e) == 422:
            return failure("REPO_EXISTS", f"{owner}/{name} already exists or the name is invalid")
        raise
    return success(
        {
            "full_name": data.get("full_name"),
            "html_url": data.get("html_url"),
            "private": data.get("private"),
            "default_branch": data.get("default_branch"),
        }
    )


async def _create_branch(arguments: dict[str, Any], settings: Any) -> dict[str, Any]:
    repo = _repo(required_str(arguments, "repo"))
    branch = _branch(required_str(arguments, "branch"))
    from_ref = optional_str(arguments, "from_ref")
    if from_ref:
        _ref(from_ref, "from_ref")
    _check_owner(settings, repo.split("/", 1)[0])
    _take_rate_slot(settings)

    if not from_ref:
        from_ref = await _default_branch(settings, repo)
    try:
        commit = await _send(settings, "GET", f"/repos/{repo}/commits/{from_ref}")
    except httpx.HTTPStatusError as e:
        if _status(e) in (404, 422):
            return failure("REF_NOT_FOUND", f"{from_ref} does not exist in {repo}")
        raise
    sha = commit.get("sha")
    try:
        await _send(settings, "POST", f"/repos/{repo}/git/refs", json={"ref": f"refs/heads/{branch}", "sha": sha})
    except httpx.HTTPStatusError as e:
        if _status(e) == 422:
            return failure("BRANCH_EXISTS", f"{branch} already exists in {repo}")
        raise
    return success(
        {
            "repo": repo,
            "branch": branch,
            "sha": sha,
            "from_ref": from_ref,
            "html_url": f"https://github.com/{repo}/tree/{branch}",
        }
    )


def _decode_files(arguments: dict[str, Any]) -> tuple[list[tuple[str, bytes, bool]], list[str]]:
    files = arguments.get("files") or []
    deletes = arguments.get("delete_paths") or []
    if not isinstance(files, list) or not isinstance(deletes, list):
        raise ValueError("files and delete_paths must be lists")
    if not files and not deletes:
        raise ValueError("files or delete_paths is required")
    if len(files) + len(deletes) > MAX_FILES:
        raise ValueError(f"at most {MAX_FILES} paths per commit")

    decoded: list[tuple[str, bytes, bool]] = []
    total = 0
    for item in files:
        if not isinstance(item, dict):
            raise ValueError("each file must be an object with path and content")
        path = _path(item.get("path"))
        content = item.get("content")
        if not isinstance(content, str):
            raise ValueError(f"{path}: content must be a string")
        encoding = item.get("encoding") or "utf-8"
        if encoding == "utf-8":
            raw = content.encode("utf-8")
        elif encoding == "base64":
            try:
                raw = base64.b64decode(content, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ValueError(f"{path}: content is not valid base64") from exc
        else:
            raise ValueError(f"{path}: encoding must be utf-8 or base64")
        if len(raw) > MAX_FILE_BYTES:
            raise ValueError(f"{path} exceeds {MAX_FILE_BYTES} bytes")
        total += len(raw)
        decoded.append((path, raw, bool(item.get("executable"))))
    if total > MAX_TOTAL_BYTES:
        raise ValueError(f"commit exceeds {MAX_TOTAL_BYTES} bytes")

    delete_paths = [_path(p) for p in deletes]
    paths = [p for p, _, _ in decoded] + delete_paths
    if len(set(paths)) != len(paths):
        raise ValueError("each path may appear only once per commit")
    return decoded, delete_paths


async def _commit_files(arguments: dict[str, Any], settings: Any) -> dict[str, Any]:
    repo = _repo(required_str(arguments, "repo"))
    branch = _branch(required_str(arguments, "branch"))
    message = _text(arguments, "message", required=True, limit=MAX_MESSAGE_CHARS)
    expected = optional_str(arguments, "expected_head_sha")
    if expected and not _SHA_RE.fullmatch(expected):
        raise ValueError("expected_head_sha must be a 40-character commit SHA")
    files, deletes = _decode_files(arguments)
    _check_owner(settings, repo.split("/", 1)[0])
    _take_rate_slot(settings)

    try:
        ref = await _send(settings, "GET", f"/repos/{repo}/git/ref/heads/{branch}")
    except httpx.HTTPStatusError as e:
        if _status(e) == 404:
            return failure("BRANCH_NOT_FOUND", f"{branch} does not exist in {repo}; create it first")
        if _status(e) == 409:
            return failure("EMPTY_REPOSITORY", f"{repo} has no commits; create it with auto_init=true")
        raise
    head_sha = ref["object"]["sha"]
    if expected and expected != head_sha:
        return failure("HEAD_MOVED", f"{branch} is at {head_sha}, not {expected}; nothing was committed")
    base_tree = (await _send(settings, "GET", f"/repos/{repo}/git/commits/{head_sha}"))["tree"]["sha"]

    tree: list[dict[str, Any]] = []
    for path, raw, executable in files:
        blob = await _send(
            settings,
            "POST",
            f"/repos/{repo}/git/blobs",
            json={"content": base64.b64encode(raw).decode("ascii"), "encoding": "base64"},
        )
        tree.append(
            {"path": path, "mode": "100755" if executable else "100644", "type": "blob", "sha": blob["sha"]}
        )
    # A null sha removes the path from the new tree.
    tree += [{"path": path, "mode": "100644", "type": "blob", "sha": None} for path in deletes]

    new_tree = await _send(
        settings, "POST", f"/repos/{repo}/git/trees", json={"base_tree": base_tree, "tree": tree}
    )
    commit = await _send(
        settings,
        "POST",
        f"/repos/{repo}/git/commits",
        json={"message": message, "tree": new_tree["sha"], "parents": [head_sha]},
    )
    try:
        await _send(
            settings,
            "PATCH",
            f"/repos/{repo}/git/refs/heads/{branch}",
            json={"sha": commit["sha"], "force": False},
        )
    except httpx.HTTPStatusError as e:
        if _status(e) == 422:
            return failure("HEAD_MOVED", f"{branch} moved during the commit; nothing was overwritten")
        raise
    return success(
        {
            "repo": repo,
            "branch": branch,
            "sha": commit["sha"],
            "parent": head_sha,
            "files": len(files),
            "deleted": len(deletes),
            "bytes": sum(len(raw) for _, raw, _ in files),
            "html_url": f"https://github.com/{repo}/commit/{commit['sha']}",
        }
    )


async def _create_pr(arguments: dict[str, Any], settings: Any) -> dict[str, Any]:
    repo = _repo(required_str(arguments, "repo"))
    title = _text(arguments, "title", required=True, limit=MAX_TITLE_CHARS)
    head = required_str(arguments, "head")
    head_owner, _, head_branch = head.rpartition(":")
    if head_owner:
        _name(head_owner, "head owner")
    _branch(head_branch, "head")
    base = optional_str(arguments, "base")
    if base:
        _branch(base, "base")
    body = _text(arguments, "body", required=False, limit=MAX_BODY_CHARS)
    draft = optional_bool(arguments, "draft", False)
    maintainer_can_modify = optional_bool(arguments, "maintainer_can_modify", True)
    _check_interact(settings, repo)
    _take_rate_slot(settings)

    if not base:
        base = await _default_branch(settings, repo)
    try:
        data = await _send(
            settings,
            "POST",
            f"/repos/{repo}/pulls",
            json={
                "title": title,
                "head": head,
                "base": base,
                "body": body,
                "draft": draft,
                "maintainer_can_modify": maintainer_can_modify,
            },
        )
    except httpx.HTTPStatusError as e:
        if _status(e) == 422:
            return failure("PR_INVALID", f"GitHub rejected the PR: {e.response.text[:300]}")
        raise
    return success(
        {
            "number": data.get("number"),
            "html_url": data.get("html_url"),
            "state": data.get("state"),
            "draft": data.get("draft"),
            "head": head,
            "base": base,
        }
    )


async def _create_issue(arguments: dict[str, Any], settings: Any) -> dict[str, Any]:
    repo = _repo(required_str(arguments, "repo"))
    title = _text(arguments, "title", required=True, limit=MAX_TITLE_CHARS)
    body = _text(arguments, "body", required=False, limit=MAX_BODY_CHARS)
    labels = _str_list(arguments, "labels")
    assignees = _str_list(arguments, "assignees")
    _check_interact(settings, repo)
    _take_rate_slot(settings)

    payload: dict[str, Any] = {"title": title, "body": body}
    if labels:
        payload["labels"] = labels
    if assignees:
        payload["assignees"] = assignees
    data = await _send(settings, "POST", f"/repos/{repo}/issues", json=payload)
    return success({"number": data.get("number"), "html_url": data.get("html_url"), "state": data.get("state")})


async def _add_comment(arguments: dict[str, Any], settings: Any) -> dict[str, Any]:
    repo = _repo(required_str(arguments, "repo"))
    issue_number = required_int(arguments, "issue_number")
    body = _text(arguments, "body", required=True, limit=MAX_BODY_CHARS)
    _check_interact(settings, repo)
    _take_rate_slot(settings)

    data = await _send(settings, "POST", f"/repos/{repo}/issues/{issue_number}/comments", json={"body": body})
    return success({"id": data.get("id"), "html_url": data.get("html_url"), "issue_number": issue_number})


HANDLERS: dict[str, Callable[[dict[str, Any], Any], Awaitable[dict[str, Any]]]] = {
    "github_fork_repo": _fork_repo,
    "github_create_repo": _create_repo,
    "github_create_branch": _create_branch,
    "github_commit_files": _commit_files,
    "github_create_pr": _create_pr,
    "github_create_issue": _create_issue,
    "github_add_comment": _add_comment,
}
