from __future__ import annotations

import asyncio
import base64
import json
import logging
from types import SimpleNamespace
from typing import Any, Callable

import httpx
import pytest
from fastapi.testclient import TestClient

from github_mcp import tools, write

READ_KEY = "read-secret"
WRITE_KEY = "write-secret"
READ_TOKEN = "synthetic-read-token"
WRITE_TOKEN = "synthetic-write-token"
HEAD = "a" * 40
NEW_COMMIT = "c" * 40


def settings(**overrides: Any) -> SimpleNamespace:
    base = dict(
        github_api_key=READ_KEY,
        github_write_api_key=WRITE_KEY,
        github_token=READ_TOKEN,
        github_write_token=WRITE_TOKEN,
        github_write_enabled=True,
        github_write_owners="david-yang-pixel",
        github_upstream_repos="Stardm0/MoonTV,someorg/*",
        github_write_rate_per_hour=30,
        github_timeout_seconds=5,
    )
    base.update(overrides)
    ns = SimpleNamespace(**base)
    ns.write_active = (
        ns.github_write_enabled
        and bool(ns.github_write_token)
        and bool(ns.github_write_api_key)
        and ns.github_write_api_key != ns.github_api_key
    )
    return ns


class FakeGitHub:
    """Route by (METHOD, path); record every request."""

    def __init__(self, routes: dict[tuple[str, str], Callable[[httpx.Request], httpx.Response] | Any]):
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        route = self.routes.get((request.method, request.url.path))
        if route is None:
            return httpx.Response(404, json={"message": "Not Found"})
        if callable(route):
            return route(request)
        return httpx.Response(200, json=route)

    def calls(self) -> list[tuple[str, str]]:
        return [(r.method, r.url.path) for r in self.requests]

    def body(self, method: str, path: str) -> dict[str, Any]:
        req = next(r for r in self.requests if (r.method, r.url.path) == (method, path))
        return json.loads(req.content)


@pytest.fixture(autouse=True)
def _reset_rate_limit():
    write.reset_rate_limit()
    yield
    write.reset_rate_limit()


@pytest.fixture
def github(monkeypatch):
    def install(routes):
        fake = FakeGitHub(routes)
        transport = httpx.MockTransport(fake)
        monkeypatch.setattr(write, "_TRANSPORT", transport)
        monkeypatch.setattr(tools, "_TRANSPORT", transport)
        return fake

    return install


def run(name: str, arguments: dict[str, Any], s: SimpleNamespace | None = None) -> dict[str, Any]:
    return asyncio.run(tools.handle(name, arguments, s or settings()))


# ------------------------------------------------------------------ app layer


@pytest.fixture
def app_client(monkeypatch):
    monkeypatch.setenv("GITHUB_MCP_API_KEY", READ_KEY)
    monkeypatch.setenv("GITHUB_MCP_WRITE_API_KEY", WRITE_KEY)
    monkeypatch.setenv("GITHUB_MCP_GITHUB_WRITE_TOKEN", WRITE_TOKEN)
    monkeypatch.setenv("GITHUB_MCP_WRITE_ENABLED", "true")
    from github_mcp.settings import get_settings

    get_settings.cache_clear()
    from github_mcp.app import create_service_app

    yield TestClient(create_service_app("github-mcp"))
    get_settings.cache_clear()


def _rpc(client: TestClient, key: str, method: str, params: dict | None = None):
    return client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {key}"},
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
    )


def test_read_key_lists_no_write_tools(app_client) -> None:
    tools_listed = _rpc(app_client, READ_KEY, "tools/list").json()["result"]["tools"]
    names = {t["name"] for t in tools_listed}
    assert not names & tools.WRITE_TOOL_NAMES
    assert all(t["annotations"]["readOnlyHint"] is True for t in tools_listed)


def test_write_key_lists_write_tools_with_write_annotations(app_client) -> None:
    listed = {t["name"]: t for t in _rpc(app_client, WRITE_KEY, "tools/list").json()["result"]["tools"]}
    assert tools.WRITE_TOOL_NAMES <= set(listed)
    for name in tools.WRITE_TOOL_NAMES:
        assert listed[name]["annotations"]["readOnlyHint"] is False, name
        assert "write" not in listed[name]  # internal flag is not published
    assert listed["github_commit_files"]["annotations"]["destructiveHint"] is True
    assert listed["github_search_prs"]["annotations"]["readOnlyHint"] is True


def test_read_key_calling_write_tool_is_forbidden(app_client) -> None:
    body = _rpc(
        app_client,
        READ_KEY,
        "tools/call",
        {"name": "github_create_issue", "arguments": {"repo": "david-yang-pixel/x", "title": "t"}},
    ).json()
    assert body["result"]["structuredContent"]["code"] == "FORBIDDEN_SCOPE"


def test_wrong_key_is_rejected(app_client) -> None:
    assert _rpc(app_client, "nope", "tools/list").status_code == 401


def test_write_disabled_hides_and_blocks_write_tools(app_client, monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_MCP_WRITE_ENABLED", "false")
    from github_mcp.settings import get_settings

    get_settings.cache_clear()
    names = {t["name"] for t in _rpc(app_client, WRITE_KEY, "tools/list").json()["result"]["tools"]}
    assert not names & tools.WRITE_TOOL_NAMES
    body = _rpc(
        app_client,
        WRITE_KEY,
        "tools/call",
        {"name": "github_create_issue", "arguments": {"repo": "david-yang-pixel/x", "title": "t"}},
    ).json()
    assert body["result"]["structuredContent"]["code"] == "WRITE_DISABLED"


def test_write_key_equal_to_read_key_never_grants_write(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_MCP_API_KEY", READ_KEY)
    monkeypatch.setenv("GITHUB_MCP_WRITE_API_KEY", READ_KEY)
    monkeypatch.setenv("GITHUB_MCP_GITHUB_WRITE_TOKEN", WRITE_TOKEN)
    monkeypatch.setenv("GITHUB_MCP_WRITE_ENABLED", "true")
    from github_mcp.settings import get_settings

    get_settings.cache_clear()
    from github_mcp.app import create_service_app

    client = TestClient(create_service_app("github-mcp"))
    names = {t["name"] for t in _rpc(client, READ_KEY, "tools/list").json()["result"]["tools"]}
    assert not names & tools.WRITE_TOOL_NAMES
    assert get_settings().write_active is False
    get_settings.cache_clear()


def test_health_reports_write_flag_only(app_client) -> None:
    health = app_client.get("/health").json()
    assert health["write_enabled"] is True
    assert WRITE_TOKEN not in json.dumps(health)


def test_write_audit_line_has_result_but_no_body(app_client, github, caplog) -> None:
    caplog.set_level(logging.INFO, logger="github_mcp.audit")
    github(
        {
            ("POST", "/repos/david-yang-pixel/x/issues"): lambda r: httpx.Response(
                201, json={"number": 7, "html_url": "https://github.com/david-yang-pixel/x/issues/7", "state": "open"}
            )
        }
    )
    secret_body = "customer tenant 7f3c private detail"
    _rpc(
        app_client,
        WRITE_KEY,
        "tools/call",
        {
            "name": "github_create_issue",
            "arguments": {"repo": "david-yang-pixel/x", "title": "secret title", "body": secret_body, "labels": ["bug"]},
        },
    )
    records = [json.loads(r.getMessage()) for r in caplog.records if r.name == "github_mcp.audit"]
    assert len(records) == 1
    line = json.dumps(records[0])
    assert records[0]["write"] is True and records[0]["scope"] == "write"
    assert records[0]["result"] == {"html_url": "https://github.com/david-yang-pixel/x/issues/7", "number": 7}
    assert records[0]["args"]["values"]["labels_count"] == 1
    assert secret_body not in line and "secret title" not in line


# ------------------------------------------------------------------ gates


def test_handle_refuses_writes_when_inactive(github) -> None:
    fake = github({})
    result = run("github_create_issue", {"repo": "david-yang-pixel/x", "title": "t"}, settings(github_write_enabled=False))
    assert result["code"] == "WRITE_DISABLED"
    assert fake.requests == []


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("github_create_issue", {"repo": "langgenius/dify", "title": "t"}),
        ("github_add_comment", {"repo": "langgenius/dify", "issue_number": 1, "body": "b"}),
        ("github_create_pr", {"repo": "langgenius/dify", "title": "t", "head": "x", "base": "main"}),
        ("github_create_branch", {"repo": "Stardm0/MoonTV", "branch": "x", "from_ref": "main"}),
        ("github_commit_files", {"repo": "Stardm0/MoonTV", "branch": "main", "message": "m", "files": [{"path": "a", "content": "b"}]}),
        ("github_create_repo", {"name": "x", "org": "langgenius"}),
        ("github_fork_repo", {"repo": "Stardm0/MoonTV", "organization": "langgenius"}),
    ],
)
def test_allowlist_blocks_before_any_request(github, name, arguments) -> None:
    fake = github({})
    result = run(name, arguments)
    assert result["code"] == "REPO_NOT_ALLOWED", result
    assert fake.requests == []


def test_upstream_allowlist_permits_issue_and_wildcard(github) -> None:
    created = {"number": 1, "html_url": "u", "state": "open"}
    github(
        {
            ("POST", "/repos/Stardm0/MoonTV/issues"): lambda r: httpx.Response(201, json=created),
            ("POST", "/repos/someorg/anything/issues"): lambda r: httpx.Response(201, json=created),
        }
    )
    assert run("github_create_issue", {"repo": "stardm0/moontv", "title": "t"})["success"] is False  # path is case-sensitive in the fake
    assert run("github_create_issue", {"repo": "Stardm0/MoonTV", "title": "t"})["success"] is True
    assert run("github_create_issue", {"repo": "someorg/anything", "title": "t"})["success"] is True


def test_rate_limit(github) -> None:
    github({("POST", "/repos/david-yang-pixel/x/issues"): lambda r: httpx.Response(201, json={"number": 1})})
    s = settings(github_write_rate_per_hour=2)
    assert run("github_create_issue", {"repo": "david-yang-pixel/x", "title": "1"}, s)["success"]
    assert run("github_create_issue", {"repo": "david-yang-pixel/x", "title": "2"}, s)["success"]
    assert run("github_create_issue", {"repo": "david-yang-pixel/x", "title": "3"}, s)["code"] == "RATE_LIMITED"


def test_write_requests_use_write_token_and_reads_use_read_token(github) -> None:
    fake = github(
        {
            ("POST", "/repos/david-yang-pixel/x/issues"): lambda r: httpx.Response(201, json={"number": 1}),
            ("GET", "/repos/david-yang-pixel/x/contents/README.md"): {
                "type": "file",
                "path": "README.md",
                "encoding": "base64",
                "content": base64.b64encode(b"hello").decode(),
                "sha": "f" * 40,
            },
        }
    )
    run("github_create_issue", {"repo": "david-yang-pixel/x", "title": "t"})
    got = run("github_get_file", {"repo": "david-yang-pixel/x", "path": "README.md"})
    assert got["data"]["content"] == "hello"
    auth = {(r.method, r.url.path): r.headers["Authorization"] for r in fake.requests}
    assert auth[("POST", "/repos/david-yang-pixel/x/issues")] == f"Bearer {WRITE_TOKEN}"
    assert auth[("GET", "/repos/david-yang-pixel/x/contents/README.md")] == f"Bearer {READ_TOKEN}"


def test_missing_write_token_never_falls_back(github) -> None:
    fake = github({})
    s = settings(github_write_token="")
    assert run("github_create_issue", {"repo": "david-yang-pixel/x", "title": "t"}, s)["code"] == "WRITE_DISABLED"
    assert fake.requests == []


# ------------------------------------------------------------------ tools


def _commit_routes(patch_status: int = 200) -> dict:
    repo = "/repos/david-yang-pixel/x"
    blobs = iter(["b1", "b2"])
    return {
        ("GET", f"{repo}/git/ref/heads/feat"): {"object": {"sha": HEAD}},
        ("GET", f"{repo}/git/commits/{HEAD}"): {"tree": {"sha": "t0"}},
        ("POST", f"{repo}/git/blobs"): lambda r: httpx.Response(201, json={"sha": next(blobs)}),
        ("POST", f"{repo}/git/trees"): lambda r: httpx.Response(201, json={"sha": "t1"}),
        ("POST", f"{repo}/git/commits"): lambda r: httpx.Response(201, json={"sha": NEW_COMMIT}),
        ("PATCH", f"{repo}/git/refs/heads/feat"): lambda r: httpx.Response(
            patch_status, json={} if patch_status == 200 else {"message": "Update is not a fast forward"}
        ),
    }


def test_commit_files_order_and_no_force(github) -> None:
    fake = github(_commit_routes())
    result = run(
        "github_commit_files",
        {
            "repo": "david-yang-pixel/x",
            "branch": "feat",
            "message": "fix title matching",
            "files": [
                {"path": "src/a.ts", "content": "export const a = 1\n"},
                {"path": "bin/run", "content": base64.b64encode(b"#!/bin/sh\n").decode(), "encoding": "base64", "executable": True},
            ],
            "delete_paths": ["old.txt"],
        },
    )
    assert result["success"] is True, result
    assert result["data"]["sha"] == NEW_COMMIT and result["data"]["deleted"] == 1
    repo = "/repos/david-yang-pixel/x"
    assert fake.calls() == [
        ("GET", f"{repo}/git/ref/heads/feat"),
        ("GET", f"{repo}/git/commits/{HEAD}"),
        ("POST", f"{repo}/git/blobs"),
        ("POST", f"{repo}/git/blobs"),
        ("POST", f"{repo}/git/trees"),
        ("POST", f"{repo}/git/commits"),
        ("PATCH", f"{repo}/git/refs/heads/feat"),
    ]
    tree = fake.body("POST", f"{repo}/git/trees")
    assert tree["base_tree"] == "t0"
    assert {"path": "old.txt", "mode": "100644", "type": "blob", "sha": None} in tree["tree"]
    assert any(e["path"] == "bin/run" and e["mode"] == "100755" for e in tree["tree"])
    assert fake.body("POST", f"{repo}/git/commits")["parents"] == [HEAD]
    assert fake.body("PATCH", f"{repo}/git/refs/heads/feat") == {"sha": NEW_COMMIT, "force": False}


def test_commit_files_head_moved_on_non_fast_forward(github) -> None:
    github(_commit_routes(patch_status=422))
    result = run(
        "github_commit_files",
        {"repo": "david-yang-pixel/x", "branch": "feat", "message": "m", "files": [{"path": "a", "content": "b"}]},
    )
    assert result["code"] == "HEAD_MOVED"


def test_commit_files_expected_head_mismatch_commits_nothing(github) -> None:
    fake = github(_commit_routes())
    result = run(
        "github_commit_files",
        {
            "repo": "david-yang-pixel/x",
            "branch": "feat",
            "message": "m",
            "files": [{"path": "a", "content": "b"}],
            "expected_head_sha": "b" * 40,
        },
    )
    assert result["code"] == "HEAD_MOVED"
    assert all(method == "GET" for method, _ in fake.calls())


@pytest.mark.parametrize(
    "files,deletes",
    [
        ([{"path": "../etc/passwd", "content": "x"}], []),
        ([{"path": "/abs", "content": "x"}], []),
        ([{"path": ".git/config", "content": "x"}], []),
        ([{"path": "a", "content": "x"}], ["a"]),
        ([], []),
        ([{"path": "a", "content": "not base64!", "encoding": "base64"}], []),
        ([{"path": "big", "content": "x" * (write.MAX_FILE_BYTES + 1)}], []),
    ],
)
def test_commit_files_rejects_bad_input_without_requests(github, files, deletes) -> None:
    fake = github({})
    result = run(
        "github_commit_files",
        {"repo": "david-yang-pixel/x", "branch": "feat", "message": "m", "files": files, "delete_paths": deletes},
    )
    assert result["code"] == "BAD_ARGUMENT", result
    assert fake.requests == []


def test_fork_resolves_login_and_checks_destination(github) -> None:
    fake = github(
        {
            ("GET", "/user"): {"login": "david-yang-pixel"},
            ("POST", "/repos/Stardm0/MoonTV/forks"): lambda r: httpx.Response(
                202, json={"full_name": "david-yang-pixel/MoonTV", "html_url": "https://github.com/david-yang-pixel/MoonTV", "default_branch": "main"}
            ),
        }
    )
    result = run("github_fork_repo", {"repo": "Stardm0/MoonTV"})
    assert result["success"] is True
    assert result["data"]["full_name"] == "david-yang-pixel/MoonTV" and result["data"]["ready"] is False
    assert fake.body("POST", "/repos/Stardm0/MoonTV/forks") == {"default_branch_only": False}


def test_fork_refused_when_token_account_not_allowed(github) -> None:
    fake = github({("GET", "/user"): {"login": "someone-else"}})
    result = run("github_fork_repo", {"repo": "Stardm0/MoonTV"})
    assert result["code"] == "REPO_NOT_ALLOWED"
    assert fake.calls() == [("GET", "/user")]


def test_create_repo_user_and_exists(github) -> None:
    fake = github(
        {
            ("GET", "/user"): {"login": "david-yang-pixel"},
            ("POST", "/user/repos"): lambda r: httpx.Response(
                201, json={"full_name": "david-yang-pixel/new", "html_url": "u", "private": False, "default_branch": "main"}
            ),
        }
    )
    result = run("github_create_repo", {"name": "new", "auto_init": True})
    assert result["data"]["full_name"] == "david-yang-pixel/new"
    assert fake.body("POST", "/user/repos") == {"name": "new", "description": "", "private": False, "auto_init": True}

    github({("GET", "/user"): {"login": "david-yang-pixel"}, ("POST", "/user/repos"): lambda r: httpx.Response(422, json={})})
    assert run("github_create_repo", {"name": "new"})["code"] == "REPO_EXISTS"


def test_create_branch_from_default_and_exists(github) -> None:
    repo = "/repos/david-yang-pixel/x"
    fake = github(
        {
            ("GET", repo): {"default_branch": "main"},
            ("GET", f"{repo}/commits/main"): {"sha": HEAD},
            ("POST", f"{repo}/git/refs"): lambda r: httpx.Response(201, json={}),
        }
    )
    result = run("github_create_branch", {"repo": "david-yang-pixel/x", "branch": "fix/title-match"})
    assert result["data"]["sha"] == HEAD and result["data"]["from_ref"] == "main"
    assert fake.body("POST", f"{repo}/git/refs") == {"ref": "refs/heads/fix/title-match", "sha": HEAD}

    github(
        {
            ("GET", f"{repo}/commits/main"): {"sha": HEAD},
            ("POST", f"{repo}/git/refs"): lambda r: httpx.Response(422, json={"message": "Reference already exists"}),
        }
    )
    assert run("github_create_branch", {"repo": "david-yang-pixel/x", "branch": "b", "from_ref": "main"})["code"] == "BRANCH_EXISTS"


@pytest.mark.parametrize("branch", ["../x", "-x", "a..b", "x.lock", "a//b", "x/", "with space"])
def test_create_branch_rejects_bad_names(github, branch) -> None:
    fake = github({})
    assert run("github_create_branch", {"repo": "david-yang-pixel/x", "branch": branch})["code"] == "BAD_ARGUMENT"
    assert fake.requests == []


def test_create_pr_from_fork_to_upstream(github) -> None:
    fake = github(
        {
            ("GET", "/repos/Stardm0/MoonTV"): {"default_branch": "main"},
            ("POST", "/repos/Stardm0/MoonTV/pulls"): lambda r: httpx.Response(
                201, json={"number": 42, "html_url": "https://github.com/Stardm0/MoonTV/pull/42", "state": "open", "draft": True}
            ),
        }
    )
    result = run(
        "github_create_pr",
        {"repo": "Stardm0/MoonTV", "title": "Fix season title matching", "head": "david-yang-pixel:fix/title-match", "draft": True},
    )
    assert result["data"]["number"] == 42 and result["data"]["base"] == "main"
    sent = fake.body("POST", "/repos/Stardm0/MoonTV/pulls")
    assert sent["head"] == "david-yang-pixel:fix/title-match" and sent["draft"] is True


# --------------------------------------------- PRs from own forks (opt-in)

_PR_CREATED = {"number": 7, "html_url": "https://github.com/langgenius/dify/pull/7", "state": "open", "draft": False}


def _pr_routes(repo: str = "langgenius/dify") -> dict:
    return {
        ("GET", f"/repos/{repo}"): {"default_branch": "main"},
        ("POST", f"/repos/{repo}/pulls"): lambda r: httpx.Response(201, json=_PR_CREATED),
    }


def test_pr_from_own_fork_to_unlisted_repo_allowed_when_enabled(github) -> None:
    fake = github(_pr_routes())
    result = run(
        "github_create_pr",
        {"repo": "langgenius/dify", "title": "t", "head": "david-yang-pixel:fix/x"},
        settings(github_pr_from_own_fork=True),
    )
    assert result["success"] is True and result["data"]["number"] == 7, result
    assert fake.body("POST", "/repos/langgenius/dify/pulls")["head"] == "david-yang-pixel:fix/x"


def test_pr_from_own_fork_head_owner_is_case_insensitive(github) -> None:
    github(_pr_routes())
    result = run(
        "github_create_pr",
        {"repo": "langgenius/dify", "title": "t", "head": "David-Yang-Pixel:fix/x", "base": "main"},
        settings(github_pr_from_own_fork=True),
    )
    assert result["success"] is True, result


@pytest.mark.parametrize("overrides", [{}, {"github_pr_from_own_fork": False}])
def test_pr_from_own_fork_to_unlisted_repo_denied_when_disabled(github, overrides) -> None:
    # ``{}``: the setting is absent altogether, i.e. the default is off.
    fake = github(_pr_routes())
    result = run(
        "github_create_pr",
        {"repo": "langgenius/dify", "title": "t", "head": "david-yang-pixel:fix/x", "base": "main"},
        settings(**overrides),
    )
    assert result["code"] == "REPO_NOT_ALLOWED", result
    assert fake.requests == []


@pytest.mark.parametrize(
    "head",
    [
        "someone-else:fix/x",  # fork owned by another account
        "langgenius:fix/x",  # branch owner is the target's owner, still not ours
        "fix/x",  # no owner: the branch lives in the target repo
    ],
)
def test_pr_to_unlisted_repo_denied_unless_head_is_own_fork(github, head) -> None:
    fake = github(_pr_routes())
    result = run(
        "github_create_pr",
        {"repo": "langgenius/dify", "title": "t", "head": head, "base": "main"},
        settings(github_pr_from_own_fork=True),
    )
    assert result["code"] == "REPO_NOT_ALLOWED", result
    assert fake.requests == []


def test_pr_with_malformed_head_owner_is_rejected(github) -> None:
    fake = github(_pr_routes())
    for head in (":fix/x", "david-yang-pixel:other:fix/x", "david-yang-pixel/dify:fix/x"):
        result = run(
            "github_create_pr",
            {"repo": "langgenius/dify", "title": "t", "head": head, "base": "main"},
            settings(github_pr_from_own_fork=True),
        )
        assert result["success"] is False, (head, result)
    assert fake.requests == []


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("github_create_issue", {"repo": "langgenius/dify", "title": "t"}),
        ("github_add_comment", {"repo": "langgenius/dify", "issue_number": 1, "body": "b"}),
        ("github_create_branch", {"repo": "langgenius/dify", "branch": "x", "from_ref": "main"}),
        ("github_commit_files", {"repo": "langgenius/dify", "branch": "main", "message": "m", "files": [{"path": "a", "content": "b"}]}),
        ("github_fork_repo", {"repo": "Stardm0/MoonTV", "organization": "langgenius"}),
    ],
)
def test_own_fork_switch_does_not_open_other_writes(github, name, arguments) -> None:
    fake = github({})
    result = run(name, arguments, settings(github_pr_from_own_fork=True))
    assert result["code"] == "REPO_NOT_ALLOWED", result
    assert fake.requests == []


@pytest.mark.parametrize("enabled", [False, True])
def test_allowlist_rules_for_prs_unchanged_by_switch(github, enabled) -> None:
    s = settings(github_pr_from_own_fork=enabled)
    github({**_pr_routes("Stardm0/MoonTV"), **_pr_routes("someorg/anything"), **_pr_routes("david-yang-pixel/x")})
    # Exact rule, owner/* rule and own repo: allowed with or without a head owner.
    for repo in ("Stardm0/MoonTV", "someorg/anything", "david-yang-pixel/x"):
        for head in ("fix/x", "someone-else:fix/x"):
            result = run("github_create_pr", {"repo": repo, "title": "t", "head": head, "base": "main"}, s)
            assert result["success"] is True, (repo, head, result)
    # A repo that only shares the owner of an exact rule is still not listed.
    result = run("github_create_pr", {"repo": "Stardm0/Other", "title": "t", "head": "fix/x", "base": "main"}, s)
    assert result["code"] == "REPO_NOT_ALLOWED", result


def test_pr_from_own_fork_still_counts_against_rate_limit(github) -> None:
    github(_pr_routes())
    s = settings(github_pr_from_own_fork=True, github_write_rate_per_hour=1)
    args = {"repo": "langgenius/dify", "title": "t", "head": "david-yang-pixel:fix/x", "base": "main"}
    assert run("github_create_pr", args, s)["success"] is True
    assert run("github_create_pr", args, s)["code"] == "RATE_LIMITED"


def test_pr_from_own_fork_setting_defaults_off_and_reads_env(monkeypatch) -> None:
    from github_mcp.settings import Settings

    monkeypatch.delenv("GITHUB_MCP_PR_FROM_OWN_FORK", raising=False)
    assert Settings(_env_file=None).github_pr_from_own_fork is False
    monkeypatch.setenv("GITHUB_MCP_PR_FROM_OWN_FORK", "true")
    assert Settings(_env_file=None).github_pr_from_own_fork is True


def test_create_pr_description_mentions_own_fork_rule() -> None:
    description = next(t for t in write.TOOLS if t["name"] == "github_create_pr")["description"]
    assert "GITHUB_MCP_PR_FROM_OWN_FORK" in description
    assert "GITHUB_MCP_UPSTREAM_REPOS" in description


def test_add_comment_and_body_limit(github) -> None:
    fake = github(
        {
            ("POST", "/repos/Stardm0/MoonTV/issues/5/comments"): lambda r: httpx.Response(
                201, json={"id": 99, "html_url": "https://github.com/Stardm0/MoonTV/issues/5#issuecomment-99"}
            )
        }
    )
    assert run("github_add_comment", {"repo": "Stardm0/MoonTV", "issue_number": 5, "body": "thanks"})["data"]["id"] == 99
    too_long = "x" * (write.MAX_BODY_CHARS + 1)
    assert run("github_add_comment", {"repo": "Stardm0/MoonTV", "issue_number": 5, "body": too_long})["code"] == "BAD_ARGUMENT"
    assert len(fake.requests) == 1


def test_get_file_lists_directory_and_truncates(github) -> None:
    github(
        {
            ("GET", "/repos/david-yang-pixel/x/contents/src"): lambda r: httpx.Response(
                200, json=[{"name": "a.ts", "path": "src/a.ts", "type": "file", "size": 3, "sha": "s"}]
            ),
            ("GET", "/repos/david-yang-pixel/x/contents/big.txt"): {
                "type": "file",
                "path": "big.txt",
                "encoding": "base64",
                "content": base64.b64encode(b"abcdef").decode(),
            },
        }
    )
    listing = run("github_get_file", {"repo": "david-yang-pixel/x", "path": "src"})
    assert listing["data"]["type"] == "dir" and listing["data"]["entries"][0]["path"] == "src/a.ts"
    big = run("github_get_file", {"repo": "david-yang-pixel/x", "path": "big.txt", "max_chars": 3})
    assert big["data"]["content"] == "abc" and big["data"]["content_truncated"] is True
