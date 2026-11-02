"""Reduce raw GitHub REST payloads to the fields an agent actually needs.

Raw GitHub objects are dominated by ``*_url`` links and nested full ``user`` /
``repo`` objects. The shapers below keep the meaningful fields, flatten users to
logins and repositories to ``full_name``, keep ``html_url`` and truncate long
text. ``select_fields`` serves the explicit ``fields`` escape hatch.
"""

from __future__ import annotations

import re
from typing import Any, Callable

_FIELD_RE = re.compile(r"^[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)*$")
MAX_FIELDS = 50

Shaper = Callable[[dict[str, Any]], dict[str, Any]]


def parse_fields(value: Any) -> list[str]:
    """Accept a list of dot paths (or a comma-separated string)."""
    if value is None:
        return []
    if isinstance(value, str):
        items = [part.strip() for part in value.split(",")]
    elif isinstance(value, list):
        items = []
        for part in value:
            if not isinstance(part, str):
                raise ValueError("fields must be a list of strings")
            items.append(part.strip())
    else:
        raise ValueError("fields must be a list of strings")
    fields = [item for item in items if item]
    if len(fields) > MAX_FIELDS:
        raise ValueError(f"fields accepts at most {MAX_FIELDS} entries")
    for field in fields:
        if not _FIELD_RE.fullmatch(field):
            raise ValueError(f"invalid field path: {field!r} (use names like 'head.sha')")
    return list(dict.fromkeys(fields))


def pick(value: Any, parts: list[str]) -> Any:
    current = value
    for index, part in enumerate(parts):
        if isinstance(current, list):
            return [pick(item, parts[index:]) for item in current]
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def select_fields(obj: dict[str, Any], fields: list[str]) -> dict[str, Any]:
    """Return ``{path: value}`` for each requested dot path.

    A path that crosses a list maps over it, so ``labels.name`` yields the list
    of label names. Missing paths yield ``None``.
    """
    return {field: pick(obj, field.split(".")) for field in fields}


def put_text(out: dict[str, Any], key: str, text: Any, limit: int) -> None:
    """Store ``text`` under ``key``; ``limit`` 0 omits it, otherwise truncates."""
    if limit == 0:
        return
    if not isinstance(text, str):
        out[key] = text
        return
    if limit > 0 and len(text) > limit:
        out[key] = text[:limit]
        out[f"{key}_truncated"] = True
        out[f"{key}_chars"] = len(text)
    else:
        out[key] = text


def login(user: Any) -> str | None:
    if isinstance(user, dict):
        value = user.get("login")
        return value if isinstance(value, str) else None
    return None


def logins(users: Any) -> list[str]:
    if not isinstance(users, list):
        return []
    return [name for name in (login(user) for user in users) if name]


def label_names(labels: Any) -> list[str]:
    if not isinstance(labels, list):
        return []
    names: list[str] = []
    for label in labels:
        if isinstance(label, dict) and isinstance(label.get("name"), str):
            names.append(label["name"])
        elif isinstance(label, str):
            names.append(label)
    return names


def _get(obj: Any, *path: str) -> Any:
    current = obj
    for part in path:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def repo_from_api_url(url: Any) -> str | None:
    """``https://api.github.com/repos/o/r`` -> ``o/r``."""
    if not isinstance(url, str) or "/repos/" not in url:
        return None
    tail = url.split("/repos/", 1)[1].split("/")
    return "/".join(tail[:2]) if len(tail) >= 2 else None


def _ref(branch: Any) -> dict[str, Any] | None:
    if not isinstance(branch, dict):
        return None
    return {
        "ref": branch.get("ref"),
        "sha": branch.get("sha"),
        "repo": _get(branch, "repo", "full_name"),
        "label": branch.get("label"),
    }


def shape_pr(raw: dict[str, Any], *, max_body_chars: int = 8000) -> dict[str, Any]:
    out: dict[str, Any] = {
        "number": raw.get("number"),
        "title": raw.get("title"),
        "state": raw.get("state"),
        "draft": raw.get("draft"),
        "merged": raw.get("merged"),
        "merged_at": raw.get("merged_at"),
        "merged_by": login(raw.get("merged_by")),
        "merge_commit_sha": raw.get("merge_commit_sha"),
        "user": login(raw.get("user")),
        "author_association": raw.get("author_association"),
        "base": _ref(raw.get("base")),
        "head": _ref(raw.get("head")),
        "labels": label_names(raw.get("labels")),
        "assignees": logins(raw.get("assignees")),
        "requested_reviewers": logins(raw.get("requested_reviewers")),
        "requested_teams": [
            team.get("slug") for team in raw.get("requested_teams") or [] if isinstance(team, dict)
        ],
        "milestone": _get(raw, "milestone", "title"),
        "created_at": raw.get("created_at"),
        "updated_at": raw.get("updated_at"),
        "closed_at": raw.get("closed_at"),
        "html_url": raw.get("html_url"),
        "additions": raw.get("additions"),
        "deletions": raw.get("deletions"),
        "changed_files": raw.get("changed_files"),
        "commits": raw.get("commits"),
        "comments": raw.get("comments"),
        "review_comments": raw.get("review_comments"),
        "mergeable": raw.get("mergeable"),
        "mergeable_state": raw.get("mergeable_state"),
        "locked": raw.get("locked"),
    }
    put_text(out, "body", raw.get("body"), max_body_chars)
    return out


def shape_issue(raw: dict[str, Any], *, max_body_chars: int = 8000) -> dict[str, Any]:
    out: dict[str, Any] = {
        "number": raw.get("number"),
        "title": raw.get("title"),
        "state": raw.get("state"),
        "state_reason": raw.get("state_reason"),
        "is_pull_request": "pull_request" in raw,
        "user": login(raw.get("user")),
        "author_association": raw.get("author_association"),
        "labels": label_names(raw.get("labels")),
        "assignees": logins(raw.get("assignees")),
        "milestone": _get(raw, "milestone", "title"),
        "comments": raw.get("comments"),
        "reactions": _get(raw, "reactions", "total_count"),
        "locked": raw.get("locked"),
        "created_at": raw.get("created_at"),
        "updated_at": raw.get("updated_at"),
        "closed_at": raw.get("closed_at"),
        "closed_by": login(raw.get("closed_by")),
        "html_url": raw.get("html_url"),
    }
    put_text(out, "body", raw.get("body"), max_body_chars)
    return out


def shape_search_issue(raw: dict[str, Any], *, max_body_chars: int = 300) -> dict[str, Any]:
    pull = raw.get("pull_request")
    out: dict[str, Any] = {
        "number": raw.get("number"),
        "title": raw.get("title"),
        "state": raw.get("state"),
        "state_reason": raw.get("state_reason"),
        "repository": repo_from_api_url(raw.get("repository_url")),
        "is_pull_request": isinstance(pull, dict),
        "user": login(raw.get("user")),
        "labels": label_names(raw.get("labels")),
        "comments": raw.get("comments"),
        "created_at": raw.get("created_at"),
        "updated_at": raw.get("updated_at"),
        "closed_at": raw.get("closed_at"),
        "html_url": raw.get("html_url"),
    }
    if isinstance(pull, dict):
        out["draft"] = raw.get("draft")
        out["merged_at"] = pull.get("merged_at")
    put_text(out, "body", raw.get("body"), max_body_chars)
    return out


def shape_code_item(raw: dict[str, Any]) -> dict[str, Any]:
    repository = raw.get("repository") if isinstance(raw.get("repository"), dict) else {}
    private = repository.get("private")
    out: dict[str, Any] = {
        "name": raw.get("name"),
        "path": raw.get("path"),
        "sha": raw.get("sha"),
        "html_url": raw.get("html_url"),
        "repository": {
            "full_name": repository.get("full_name"),
            "private": private if isinstance(private, bool) else None,
        },
    }
    fragments = []
    for match in raw.get("text_matches") or []:
        if isinstance(match, dict) and isinstance(match.get("fragment"), str):
            fragments.append(match["fragment"])
    if fragments:
        out["fragments"] = fragments
    return out


def shape_comment(raw: dict[str, Any], *, max_body_chars: int = 2000) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": raw.get("id"),
        "user": login(raw.get("user")),
        "author_association": raw.get("author_association"),
        "created_at": raw.get("created_at"),
        "updated_at": raw.get("updated_at"),
        "html_url": raw.get("html_url"),
    }
    put_text(out, "body", raw.get("body"), max_body_chars)
    return out


def shape_file(
    raw: dict[str, Any], *, include_patch: bool = False, max_patch_chars: int = 2000
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "filename": raw.get("filename"),
        "status": raw.get("status"),
        "additions": raw.get("additions"),
        "deletions": raw.get("deletions"),
        "changes": raw.get("changes"),
    }
    if raw.get("previous_filename"):
        out["previous_filename"] = raw.get("previous_filename")
    if include_patch:
        # GitHub omits ``patch`` for binary or very large diffs.
        put_text(out, "patch", raw.get("patch"), max_patch_chars)
    return out


def first_line(text: Any) -> str | None:
    if not isinstance(text, str):
        return None
    return text.split("\n", 1)[0].strip()


def shape_commit(raw: dict[str, Any]) -> dict[str, Any]:
    commit = raw.get("commit") if isinstance(raw.get("commit"), dict) else {}
    author = commit.get("author") if isinstance(commit.get("author"), dict) else {}
    return {
        "sha": raw.get("sha"),
        "message": first_line(commit.get("message")),
        "author_login": login(raw.get("author")),
        "author_name": author.get("name"),
        "date": author.get("date"),
        "html_url": raw.get("html_url"),
    }


def shape_release(raw: dict[str, Any], *, max_body_chars: int = 1000) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": raw.get("id"),
        "tag_name": raw.get("tag_name"),
        "name": raw.get("name"),
        "draft": raw.get("draft"),
        "prerelease": raw.get("prerelease"),
        "target_commitish": raw.get("target_commitish"),
        "author": login(raw.get("author")),
        "created_at": raw.get("created_at"),
        "published_at": raw.get("published_at"),
        "html_url": raw.get("html_url"),
    }
    put_text(out, "body", raw.get("body"), max_body_chars)
    return out


def shape_release_detail(raw: dict[str, Any], *, max_body_chars: int = 8000) -> dict[str, Any]:
    out = shape_release(raw, max_body_chars=max_body_chars)
    out["assets"] = [
        {
            "name": asset.get("name"),
            "size": asset.get("size"),
            "download_count": asset.get("download_count"),
            "content_type": asset.get("content_type"),
        }
        for asset in raw.get("assets") or []
        if isinstance(asset, dict)
    ]
    return out
