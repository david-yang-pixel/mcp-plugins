from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any, Callable

import httpx
import pytest

from github_mcp import tools
from github_mcp.shaping import parse_fields, select_fields

SETTINGS = SimpleNamespace(github_token="synthetic-token", github_timeout_seconds=5)

USER = {
    "login": "octocat",
    "id": 1,
    "url": "https://api.github.com/users/octocat",
    "html_url": "https://github.com/octocat",
    "followers_url": "https://api.github.com/users/octocat/followers",
}
REPO_OBJ = {
    "full_name": "langgenius/dify",
    "private": False,
    "owner": USER,
    "url": "https://api.github.com/repos/langgenius/dify",
    "hooks_url": "https://api.github.com/repos/langgenius/dify/hooks",
}


class FakeGitHub:
    """Route mock HTTP requests by path; record every request."""

    def __init__(self, routes: dict[str, Callable[[httpx.Request], httpx.Response] | Any]):
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        route = self.routes.get(request.url.path)
        if route is None:
            return httpx.Response(404, json={"message": "Not Found"})
        if callable(route):
            return route(request)
        return httpx.Response(200, json=route)

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]


@pytest.fixture
def github(monkeypatch):
    def install(routes):
        fake = FakeGitHub(routes)
        monkeypatch.setattr(tools, "_TRANSPORT", httpx.MockTransport(fake))
        return fake

    return install


def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = asyncio.run(tools.handle(name, arguments, SETTINGS))
    json.dumps(result)  # must stay serialisable
    return result


def _pr_raw() -> dict[str, Any]:
    return {
        "url": "https://api.github.com/repos/langgenius/dify/pulls/42",
        "html_url": "https://github.com/langgenius/dify/pull/42",
        "diff_url": "https://github.com/langgenius/dify/pull/42.diff",
        "issue_url": "https://api.github.com/repos/langgenius/dify/issues/42",
        "number": 42,
        "state": "closed",
        "title": "Fix workflow timeout",
        "draft": False,
        "merged": True,
        "merged_at": "2026-10-01T00:00:00Z",
        "merged_by": USER,
        "user": USER,
        "body": "x" * 50,
        "labels": [{"name": "bug", "url": "https://api.github.com/labels/bug", "color": "f00"}],
        "assignees": [USER],
        "requested_reviewers": [{**USER, "login": "reviewer"}],
        "requested_teams": [{"slug": "core", "url": "https://api.github.com/teams/1"}],
        "milestone": None,
        "base": {"ref": "main", "sha": "aaa", "label": "langgenius:main", "repo": REPO_OBJ, "user": USER},
        "head": {"ref": "fix/timeout", "sha": "bbb", "label": "fork:fix/timeout", "repo": {**REPO_OBJ, "full_name": "fork/dify"}, "user": USER},
        "created_at": "2026-09-01T00:00:00Z",
        "updated_at": "2026-10-01T00:00:00Z",
        "closed_at": "2026-10-01T00:00:00Z",
        "additions": 10,
        "deletions": 2,
        "changed_files": 3,
        "commits": 2,
        "comments": 1,
        "review_comments": 4,
        "mergeable": None,
        "mergeable_state": "unknown",
        "_links": {"self": {"href": "https://api.github.com/x"}},
    }


# ---------------------------------------------------------------- shaping


def test_get_pr_returns_compact_summary_without_api_urls(github) -> None:
    fake = github({"/repos/langgenius/dify/pulls/42": _pr_raw()})
    result = call("github_get_pr", {"repo": "langgenius/dify", "pr_number": 42})

    assert result["success"] is True
    pr = result["data"]
    assert fake.paths() == ["/repos/langgenius/dify/pulls/42"]
    assert fake.requests[0].headers["Authorization"] == "Bearer synthetic-token"
    assert pr["user"] == "octocat"
    assert pr["merged_by"] == "octocat"
    assert pr["base"] == {"ref": "main", "sha": "aaa", "repo": "langgenius/dify", "label": "langgenius:main"}
    assert pr["head"]["repo"] == "fork/dify"
    assert pr["labels"] == ["bug"]
    assert pr["assignees"] == ["octocat"]
    assert pr["requested_reviewers"] == ["reviewer"]
    assert pr["requested_teams"] == ["core"]
    assert pr["html_url"] == "https://github.com/langgenius/dify/pull/42"
    assert pr["review_comments"] == 4 and pr["changed_files"] == 3
    assert pr["mergeable_state"] == "unknown"
    text = json.dumps(pr)
    assert "api.github.com" not in text
    assert "diff_url" not in text and "_links" not in text
    assert len(text) < 1500


def test_get_pr_truncates_body(github) -> None:
    github({"/repos/langgenius/dify/pulls/42": _pr_raw()})
    pr = call("github_get_pr", {"repo": "langgenius/dify", "pr_number": 42, "max_body_chars": 10})["data"]
    assert pr["body"] == "x" * 10
    assert pr["body_truncated"] is True
    assert pr["body_chars"] == 50


def test_get_pr_fields_selects_dot_paths(github) -> None:
    github({"/repos/langgenius/dify/pulls/42": _pr_raw()})
    result = call(
        "github_get_pr",
        {"repo": "langgenius/dify", "pr_number": 42, "fields": ["head.sha", "labels.name", "title", "nope.x"]},
    )
    assert result["data"] == {
        "head.sha": "bbb",
        "labels.name": ["bug"],
        "title": "Fix workflow timeout",
        "nope.x": None,
    }


def test_get_pr_raw_returns_original_payload(github) -> None:
    github({"/repos/langgenius/dify/pulls/42": _pr_raw()})
    result = call("github_get_pr", {"repo": "langgenius/dify", "pr_number": 42, "raw": True})
    assert result["data"] == _pr_raw()


def test_fields_and_raw_together_are_rejected(github) -> None:
    github({})
    result = call("github_get_pr", {"repo": "langgenius/dify", "pr_number": 42, "raw": True, "fields": ["title"]})
    assert result["success"] is False
    assert result["code"] == "BAD_ARGUMENT"


def test_parse_fields_validates_and_accepts_csv() -> None:
    assert parse_fields("title, head.sha") == ["title", "head.sha"]
    assert parse_fields(None) == []
    with pytest.raises(ValueError):
        parse_fields(["head..sha"])
    with pytest.raises(ValueError):
        parse_fields([1])
    assert select_fields({"a": [{"b": 1}, {"b": 2}]}, ["a.b"]) == {"a.b": [1, 2]}


def test_search_issues_returns_compact_items(github) -> None:
    item = {
        "number": 7,
        "title": "Timeout",
        "state": "open",
        "user": USER,
        "labels": [{"name": "bug"}],
        "comments": 3,
        "repository_url": "https://api.github.com/repos/langgenius/dify",
        "comments_url": "https://api.github.com/repos/langgenius/dify/issues/7/comments",
        "html_url": "https://github.com/langgenius/dify/issues/7",
        "body": "b" * 1000,
    }
    github({"/search/issues": {"total_count": 1, "incomplete_results": False, "items": [item]}})
    data = call("github_search_issues", {"repo": "langgenius/dify", "query": "timeout"})["data"]
    assert data["total_count"] == 1
    assert data["query"] == "repo:langgenius/dify is:issue timeout"
    assert "repo_access" not in data
    out = data["items"][0]
    assert out["repository"] == "langgenius/dify"
    assert out["user"] == "octocat"
    assert out["is_pull_request"] is False
    assert len(out["body"]) == 300 and out["body_truncated"] is True
    assert "comments_url" not in out


def test_search_prs_marks_merged_pull_requests(github) -> None:
    item = {
        "number": 9,
        "title": "Fix",
        "state": "closed",
        "draft": False,
        "pull_request": {"merged_at": "2026-10-02T00:00:00Z", "url": "https://api.github.com/x"},
        "repository_url": "https://api.github.com/repos/langgenius/dify",
    }
    fake = github({"/search/issues": {"total_count": 1, "items": [item]}})
    data = call("github_search_prs", {"repo": "langgenius/dify", "query": "fix", "limit": 3})["data"]
    assert fake.requests[0].url.params["q"] == "repo:langgenius/dify is:pr fix"
    assert fake.requests[0].url.params["per_page"] == "3"
    assert data["items"][0]["is_pull_request"] is True
    assert data["items"][0]["merged_at"] == "2026-10-02T00:00:00Z"


# ---------------------------------------------------------------- new PR tools


def _link(next_page: bool) -> dict[str, str]:
    if not next_page:
        return {}
    return {"Link": '<https://api.github.com/x?page=2>; rel="next", <https://api.github.com/x?page=5>; rel="last"'}


def test_get_pr_files_paginates_and_truncates_patch(github) -> None:
    files = [
        {
            "sha": "s1",
            "filename": "api/a.py",
            "status": "modified",
            "additions": 3,
            "deletions": 1,
            "changes": 4,
            "blob_url": "https://github.com/x/blob",
            "raw_url": "https://github.com/x/raw",
            "contents_url": "https://api.github.com/x",
            "patch": "@@ -1 +1 @@\n" + "+line\n" * 100,
        },
        {
            "filename": "api/new.py",
            "previous_filename": "api/old.py",
            "status": "renamed",
            "additions": 0,
            "deletions": 0,
            "changes": 0,
        },
    ]
    fake = github(
        {"/repos/langgenius/dify/pulls/42/files": lambda r: httpx.Response(200, json=files, headers=_link(True))}
    )
    data = call(
        "github_get_pr_files",
        {"repo": "langgenius/dify", "pr_number": 42, "page": 2, "per_page": 2, "include_patch": True, "max_patch_chars": 20},
    )["data"]
    assert fake.requests[0].url.params["page"] == "2"
    assert fake.requests[0].url.params["per_page"] == "2"
    assert data["has_next_page"] is True
    assert data["count"] == 2
    first, second = data["files"]
    assert first["filename"] == "api/a.py" and first["changes"] == 4
    assert len(first["patch"]) == 20 and first["patch_truncated"] is True
    assert "blob_url" not in first and "previous_filename" not in first
    assert second["previous_filename"] == "api/old.py"
    assert second["patch"] is None  # GitHub omitted patch (binary / too large)


def test_get_pr_files_without_patch_and_last_page(github) -> None:
    github({"/repos/langgenius/dify/pulls/42/files": [{"filename": "a", "status": "added", "patch": "+x"}]})
    data = call("github_get_pr_files", {"repo": "langgenius/dify", "pr_number": 42})["data"]
    assert data["has_next_page"] is False
    assert data["per_page"] == 30
    assert "patch" not in data["files"][0]


def _commit(sha: str, message: str) -> dict[str, Any]:
    return {
        "sha": sha,
        "url": "https://api.github.com/x",
        "html_url": f"https://github.com/langgenius/dify/commit/{sha}",
        "commit": {"message": message, "author": {"name": "Octo Cat", "date": "2026-10-01T00:00:00Z"}},
        "author": USER,
        "committer": USER,
        "parents": [{"sha": "p", "url": "https://api.github.com/x"}],
    }


def test_list_pr_commits_returns_first_message_line(github) -> None:
    fake = github({"/repos/langgenius/dify/pulls/42/commits": [_commit("c1", "fix: timeout\n\nlong body")]})
    data = call("github_list_pr_commits", {"repo": "langgenius/dify", "pr_number": 42, "per_page": 500})["data"]
    assert fake.requests[0].url.params["per_page"] == "100"  # clamped
    assert data["commits"] == [
        {
            "sha": "c1",
            "message": "fix: timeout",
            "author_login": "octocat",
            "author_name": "Octo Cat",
            "date": "2026-10-01T00:00:00Z",
            "html_url": "https://github.com/langgenius/dify/commit/c1",
        }
    ]
    assert data["has_next_page"] is False


def test_compare_summarises_and_truncates(github) -> None:
    payload = {
        "status": "ahead",
        "ahead_by": 3,
        "behind_by": 0,
        "total_commits": 3,
        "html_url": "https://github.com/langgenius/dify/compare/1.9.0...main",
        "permalink_url": "https://github.com/x",
        "merge_base_commit": _commit("base", "base"),
        "commits": [_commit("c1", "one"), _commit("c2", "two"), _commit("c3", "three")],
        "files": [{"filename": f"f{i}", "status": "modified", "additions": 1, "deletions": 0, "changes": 1, "patch": "+x"} for i in range(5)],
    }
    fake = github({"/repos/langgenius/dify/compare/1.9.0...release/1.10": payload})
    data = call(
        "github_compare",
        {"repo": "langgenius/dify", "base": "1.9.0", "head": "release/1.10", "max_commits": 2, "max_files": 2},
    )["data"]
    assert fake.paths() == ["/repos/langgenius/dify/compare/1.9.0...release/1.10"]
    assert data["status"] == "ahead" and data["ahead_by"] == 3 and data["total_commits"] == 3
    assert data["merge_base_sha"] == "base"
    assert [c["sha"] for c in data["commits"]] == ["c1", "c2"]
    assert data["commits_omitted"] == 1
    assert [f["filename"] for f in data["files"]] == ["f0", "f1"]
    assert data["files_omitted"] == 3
    assert "patch" not in data["files"][0]
    assert "permalink_url" not in json.dumps(data)


def test_compare_notes_github_commit_cap_and_rejects_bad_refs(github) -> None:
    github({"/repos/o/r/compare/a...b": {"status": "ahead", "total_commits": 400, "commits": [_commit("c", "m")], "files": []}})
    data = call("github_compare", {"repo": "o/r", "base": "a", "head": "b"})["data"]
    assert any("400" in note for note in data["notes"])
    bad = call("github_compare", {"repo": "o/r", "base": "a..b", "head": "c"})
    assert bad["success"] is False and bad["code"] == "BAD_ARGUMENT"
    bad = call("github_compare", {"repo": "o/r", "base": "a b", "head": "c"})
    assert bad["code"] == "BAD_ARGUMENT"


# ---------------------------------------------------------------- issues and comments


def _issue_raw(comments: int) -> dict[str, Any]:
    return {
        "number": 5,
        "title": "Broken",
        "state": "open",
        "user": USER,
        "labels": [{"name": "bug"}],
        "assignees": [],
        "comments": comments,
        "comments_url": "https://api.github.com/x",
        "events_url": "https://api.github.com/x",
        "html_url": "https://github.com/langgenius/dify/issues/5",
        "body": "issue body",
        "reactions": {"total_count": 2, "url": "https://api.github.com/x"},
    }


def _comment(i: int) -> dict[str, Any]:
    return {
        "id": i,
        "user": USER,
        "body": f"comment {i} " + "y" * 30,
        "created_at": "2026-10-01T00:00:00Z",
        "updated_at": "2026-10-01T00:00:00Z",
        "html_url": f"https://github.com/langgenius/dify/issues/5#issuecomment-{i}",
        "issue_url": "https://api.github.com/x",
        "author_association": "MEMBER",
    }


def test_get_issue_includes_first_page_of_comments_by_default(github) -> None:
    fake = github(
        {
            "/repos/langgenius/dify/issues/5": _issue_raw(2),
            "/repos/langgenius/dify/issues/5/comments": lambda r: httpx.Response(
                200, json=[_comment(1), _comment(2)], headers=_link(True)
            ),
        }
    )
    data = call("github_get_issue", {"repo": "langgenius/dify", "issue_number": 5, "comment_max_body_chars": 9})["data"]
    assert fake.paths() == ["/repos/langgenius/dify/issues/5", "/repos/langgenius/dify/issues/5/comments"]
    assert fake.requests[1].url.params["per_page"] == "30"
    issue = data["issue"]
    assert issue["user"] == "octocat" and issue["labels"] == ["bug"] and issue["reactions"] == 2
    assert issue["is_pull_request"] is False
    assert "comments_url" not in issue
    comments = data["comments"]
    assert comments["total"] == 2 and comments["has_next_page"] is True
    assert comments["items"][0]["user"] == "octocat"
    assert comments["items"][0]["body"] == "comment 1"
    assert comments["items"][0]["body_truncated"] is True
    assert comments["items"][0]["html_url"].endswith("issuecomment-1")


def test_get_issue_skips_comment_call_when_disabled_or_empty(github) -> None:
    fake = github({"/repos/o/r/issues/5": _issue_raw(3)})
    data = call("github_get_issue", {"repo": "o/r", "issue_number": 5, "include_comments": False})["data"]
    assert "comments" not in data
    assert fake.paths() == ["/repos/o/r/issues/5"]

    fake = github({"/repos/o/r/issues/5": _issue_raw(0)})
    data = call("github_get_issue", {"repo": "o/r", "issue_number": 5})["data"]
    assert data["comments"]["items"] == [] and data["comments"]["total"] == 0
    assert fake.paths() == ["/repos/o/r/issues/5"]


def test_get_issue_comment_failure_does_not_fail_issue(github) -> None:
    github(
        {
            "/repos/o/r/issues/5": _issue_raw(1),
            "/repos/o/r/issues/5/comments": lambda r: httpx.Response(502, text="bad gateway"),
        }
    )
    result = call("github_get_issue", {"repo": "o/r", "issue_number": 5})
    assert result["success"] is True
    assert result["data"]["issue"]["number"] == 5
    assert "HTTP 502" in result["data"]["comments"]["error"]


def test_list_issue_comments_paginates(github) -> None:
    fake = github({"/repos/o/r/issues/5/comments": [_comment(3)]})
    data = call("github_list_issue_comments", {"repo": "o/r", "issue_number": 5, "page": 3, "per_page": 1})["data"]
    assert fake.requests[0].url.params["page"] == "3"
    assert data["page"] == 3 and data["has_next_page"] is False
    assert data["comments"][0]["id"] == 3
    assert "issue_url" not in data["comments"][0]


# ---------------------------------------------------------------- releases


def _release(tag: str) -> dict[str, Any]:
    return {
        "id": 1,
        "tag_name": tag,
        "name": f"v{tag}",
        "draft": False,
        "prerelease": False,
        "published_at": "2026-10-01T00:00:00Z",
        "created_at": "2026-10-01T00:00:00Z",
        "author": USER,
        "html_url": f"https://github.com/langgenius/dify/releases/tag/{tag}",
        "assets_url": "https://api.github.com/x",
        "upload_url": "https://uploads.github.com/x",
        "tarball_url": "https://api.github.com/x",
        "body": "r" * 2000,
        "assets": [{"name": "dify.tar.gz", "size": 10, "download_count": 4, "content_type": "application/gzip", "url": "https://api.github.com/x", "uploader": USER}],
    }


def test_list_releases_truncates_body_and_reports_next_page(github) -> None:
    fake = github(
        {"/repos/langgenius/dify/releases": lambda r: httpx.Response(200, json=[_release("1.9.0")], headers=_link(True))}
    )
    data = call("github_list_releases", {"repo": "langgenius/dify", "per_page": 1})["data"]
    assert fake.requests[0].url.params["per_page"] == "1"
    assert data["has_next_page"] is True
    rel = data["releases"][0]
    assert rel["tag_name"] == "1.9.0" and rel["author"] == "octocat"
    assert len(rel["body"]) == 1000 and rel["body_truncated"] is True
    assert "tarball_url" not in rel and "assets" not in rel


def test_list_releases_can_omit_body(github) -> None:
    github({"/repos/o/r/releases": [_release("1.0.0")]})
    rel = call("github_list_releases", {"repo": "o/r", "max_body_chars": 0})["data"]["releases"][0]
    assert "body" not in rel


def test_get_release_by_tag_and_latest(github) -> None:
    fake = github(
        {
            "/repos/o/r/releases/tags/1.9.0": _release("1.9.0"),
            "/repos/o/r/releases/latest": _release("2.0.0"),
        }
    )
    rel = call("github_get_release", {"repo": "o/r", "tag": "1.9.0"})["data"]
    assert rel["tag_name"] == "1.9.0"
    assert rel["body"] == "r" * 2000
    assert rel["assets"] == [{"name": "dify.tar.gz", "size": 10, "download_count": 4, "content_type": "application/gzip"}]
    latest = call("github_get_release", {"repo": "o/r"})["data"]
    assert latest["tag_name"] == "2.0.0"
    assert fake.paths() == ["/repos/o/r/releases/tags/1.9.0", "/repos/o/r/releases/latest"]


# ---------------------------------------------------------------- code search


def _code_item(repo: str, private: bool | None, path: str = "api/rbac.py") -> dict[str, Any]:
    repository: dict[str, Any] = {"full_name": repo, "owner": USER, "url": "https://api.github.com/x"}
    if private is not None:
        repository["private"] = private
    return {
        "name": path.rsplit("/", 1)[-1],
        "path": path,
        "sha": "s",
        "url": "https://api.github.com/x",
        "git_url": "https://api.github.com/x",
        "html_url": f"https://github.com/{repo}/blob/main/{path}",
        "repository": repository,
        "score": 1.0,
        "text_matches": [{"fragment": "def rbac():", "object_url": "https://api.github.com/x", "matches": []}],
    }


def test_code_search_repo_param_maps_to_repo_qualifier(github) -> None:
    fake = github({"/search/code": {"total_count": 1, "items": [_code_item("langgenius/dify", False)]}})
    data = call(
        "github_code_search",
        {"query": "rbac", "repo": "langgenius/dify", "path": "api", "language": "python"},
    )["data"]
    request = fake.requests[0]
    assert request.url.params["q"] == "rbac repo:langgenius/dify language:python path:api"
    assert request.headers["Accept"] == "application/vnd.github.text-match+json"
    item = data["items"][0]
    assert item["repository"] == {"full_name": "langgenius/dify", "private": False}
    assert item["fragments"] == ["def rbac():"]
    assert "git_url" not in item
    assert data["visibility"] == "all"
    assert "repo_access" not in data


def test_code_search_keeps_scope_behaviour() -> None:
    assert tools._code_query("x", scope="langgenius", language="", path="") == "x org:langgenius"
    assert tools._code_query("x", scope="o/r", language="", path="") == "x repo:o/r"
    assert tools._code_query("x", scope="langgenius", language="", path="", repo="a/b") == "x repo:a/b"
    with pytest.raises(ValueError):
        tools._code_query("x", scope="langgenius", language="", path="", repo="not-a-repo")


def test_code_search_marks_and_filters_private_results(github) -> None:
    items = [
        _code_item("langgenius/dify", False),
        _code_item("langgenius/dify-docs-registry", True),
        _code_item("langgenius/unknown", None),
    ]
    github({"/search/code": {"total_count": 3, "items": items}})

    all_data = call("github_code_search", {"query": "rbac"})["data"]
    assert [i["repository"]["private"] for i in all_data["items"]] == [False, True, None]
    assert "filtered_out" not in all_data

    public = call("github_code_search", {"query": "rbac", "visibility": "public"})["data"]
    assert [i["repository"]["full_name"] for i in public["items"]] == ["langgenius/dify"]
    assert public["filtered_out"] == 2
    assert "2 of 3" in public["filter_note"]

    private = call("github_code_search", {"query": "rbac", "visibility": "private"})["data"]
    assert [i["repository"]["full_name"] for i in private["items"]] == ["langgenius/dify-docs-registry"]
    assert private["filtered_out"] == 2

    bad = call("github_code_search", {"query": "rbac", "visibility": "internal"})
    assert bad["code"] == "BAD_ARGUMENT"


def test_code_search_fields_still_mark_visibility(github) -> None:
    github({"/search/code": {"total_count": 1, "items": [_code_item("o/r", True)]}})
    item = call("github_code_search", {"query": "x", "fields": ["path"]})["data"]["items"][0]
    assert item == {"path": "api/rbac.py", "repository.full_name": "o/r", "repository.private": True}


# ---------------------------------------------------------------- zero-result repo probe


def test_code_search_zero_results_probes_inaccessible_repo(github) -> None:
    fake = github({"/search/code": {"total_count": 0, "items": []}})  # /repos/... -> 404
    data = call("github_code_search", {"query": "rbac", "repo": "langgenius/dify-enterprise"})["data"]
    assert fake.paths() == ["/search/code", "/repos/langgenius/dify-enterprise"]
    access = data["repo_access"]
    assert access["accessible"] is False and access["http_status"] == 404
    assert "no access" in access["note"]


def test_code_search_zero_results_probes_accessible_private_repo(github) -> None:
    github(
        {
            "/search/code": {"total_count": 0, "items": []},
            "/repos/o/r": {"full_name": "o/r", "private": True, "visibility": "private", "has_issues": True, "archived": False},
        }
    )
    data = call("github_code_search", {"query": "rbac", "scope": "o/r"})["data"]
    access = data["repo_access"]
    assert access["accessible"] is True and access["http_status"] == 200
    assert access["private"] is True and access["archived"] is False
    assert "default branch" in access["note"]


def test_code_search_zero_results_without_repo_does_not_probe(github) -> None:
    fake = github({"/search/code": {"total_count": 0, "items": []}})
    data = call("github_code_search", {"query": "rbac"})["data"]
    assert "repo_access" not in data
    assert fake.paths() == ["/search/code"]


def test_issue_search_zero_results_reports_issues_disabled(github) -> None:
    github(
        {
            "/search/issues": {"total_count": 0, "items": []},
            "/repos/o/r": {"full_name": "o/r", "private": False, "has_issues": False, "archived": True},
        }
    )
    access = call("github_search_issues", {"repo": "o/r", "query": "rbac"})["data"]["repo_access"]
    assert access["issues_enabled"] is False
    assert "disabled" in access["note"] and "archived" in access["note"]


def test_repo_probe_failure_does_not_fail_search(github) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network down", request=request)

    github({"/search/issues": {"total_count": 0, "items": []}, "/repos/o/r": boom})
    result = call("github_search_issues", {"repo": "o/r", "query": "rbac"})
    assert result["success"] is True
    access = result["data"]["repo_access"]
    assert access["accessible"] is None
    assert "ConnectError" in access["error"]


def test_search_api_error_is_reported(github) -> None:
    # 422 on an accessible repo (e.g. bad query syntax) keeps the original error.
    fake = github(
        {
            "/search/issues": lambda r: httpx.Response(422, json={"message": "Validation Failed"}),
            "/repos/o/r": {"full_name": "o/r", "private": False, "has_issues": True},
        }
    )
    result = call("github_search_issues", {"repo": "o/r", "query": "x"})
    assert result["success"] is False
    assert result["code"] == "GITHUB_API_ERROR"
    assert "HTTP 422" in result["error"]
    assert "repo_access" not in result
    assert fake.paths() == ["/search/issues", "/repos/o/r"]


_SEARCH_422 = {
    "message": "Validation Failed",
    "errors": [
        {
            "message": "The listed users and repositories cannot be searched either because "
            "the resources do not exist or you do not have permission to view them.",
            "resource": "Search",
            "field": "q",
            "code": "invalid",
        }
    ],
}


def test_issue_search_on_inaccessible_repo_reports_repo_not_accessible(github) -> None:
    fake = github({"/search/issues": lambda r: httpx.Response(422, json=_SEARCH_422)})  # repo -> 404
    result = call("github_search_issues", {"repo": "langgenius/dify-saas", "query": "billing"})
    assert result["success"] is False
    assert result["code"] == "REPO_NOT_ACCESSIBLE"
    assert "langgenius/dify-saas" in result["error"]
    access = result["repo_access"]
    assert access["repo"] == "langgenius/dify-saas"
    assert access["accessible"] is False and access["http_status"] == 404
    assert fake.paths() == ["/search/issues", "/repos/langgenius/dify-saas"]


def test_pr_search_on_forbidden_repo_reports_repo_not_accessible(github) -> None:
    github(
        {
            "/search/issues": lambda r: httpx.Response(422, json=_SEARCH_422),
            "/repos/o/r": lambda r: httpx.Response(403, json={"message": "Forbidden"}),
        }
    )
    result = call("github_search_prs", {"repo": "o/r", "query": "x"})
    assert result["code"] == "REPO_NOT_ACCESSIBLE"
    assert result["repo_access"]["http_status"] == 403


def test_code_search_on_inaccessible_repo_reports_repo_not_accessible(github) -> None:
    fake = github({"/search/code": lambda r: httpx.Response(422, json=_SEARCH_422)})
    result = call("github_code_search", {"query": "rbac", "scope": "langgenius/dify-saas"})
    assert result["success"] is False
    assert result["code"] == "REPO_NOT_ACCESSIBLE"
    assert result["repo_access"]["accessible"] is False
    assert fake.paths() == ["/search/code", "/repos/langgenius/dify-saas"]


def test_code_search_422_without_repo_scope_keeps_original_error(github) -> None:
    fake = github({"/search/code": lambda r: httpx.Response(422, json={"message": "Validation Failed"})})
    result = call("github_code_search", {"query": "rbac", "scope": "langgenius"})
    assert result["code"] == "GITHUB_API_ERROR" and "HTTP 422" in result["error"]
    assert fake.paths() == ["/search/code"]


def test_search_422_with_inconclusive_probe_keeps_original_error(github) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network down", request=request)

    github({"/search/issues": lambda r: httpx.Response(422, json=_SEARCH_422), "/repos/o/r": boom})
    result = call("github_search_issues", {"repo": "o/r", "query": "x"})
    assert result["code"] == "GITHUB_API_ERROR" and "HTTP 422" in result["error"]


def test_search_non_422_error_does_not_probe(github) -> None:
    fake = github({"/search/issues": lambda r: httpx.Response(500, json={"message": "boom"})})
    result = call("github_search_issues", {"repo": "o/r", "query": "x"})
    assert result["code"] == "GITHUB_API_ERROR" and "HTTP 500" in result["error"]
    assert fake.paths() == ["/search/issues"]


def test_every_tool_schema_documents_its_arguments() -> None:
    for tool in tools.TOOLS:
        assert tool["description"]
        props = tool["inputSchema"]["properties"]
        for required in tool["inputSchema"].get("required", []):
            assert required in props
        assert "fields" in props and "raw" in props
    code = next(t for t in tools.TOOLS if t["name"] == "github_code_search")
    assert "dify-source" in code["description"]
    assert code["inputSchema"]["properties"]["visibility"]["enum"] == ["all", "public", "private"]
