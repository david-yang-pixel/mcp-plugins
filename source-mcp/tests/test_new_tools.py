from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from source_mcp import runtime
from source_mcp.settings import Settings

AUTH = {"Authorization": "Bearer " + "x" * 32}
RUNTIME_TAGS = {"dify-api-oltp": "1.10.0-fix.1", "dify-web": "1.9.0"}


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ("git", *args), cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _write(repo: Path, relpath: str, content: str) -> None:
    target = repo / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    base = tmp_path / "repos"
    root = base / "dify"
    root.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=root)
    _git("config", "user.email", "test@example.com", cwd=root)
    _git("config", "user.name", "Test User", cwd=root)
    _write(root, "api/service.py", "def answer_icon():\n    return 'old'\n")
    _git("add", ".", cwd=root)
    _git("commit", "-q", "-m", "first", cwd=root)
    _git("tag", "1.2.0", cwd=root)
    _write(
        root,
        "api/many.py",
        "".join(f"before {n}\nneedle {n}\n" for n in range(1, 26)),
    )
    _write(root, "docs/a:b.md", "intro\nneedle in a colon path\n")
    _write(root, "web/app/page.tsx", "export const needle = 'web';\n")
    _write(root, "web/app/layout.tsx", "export default function Layout() {}\n")
    _git("add", ".", cwd=root)
    _git("commit", "-q", "-m", "second", cwd=root)
    for tag in ("1.9.0", "v1.10.0", "1.10.0-fix.1", "1.10.0-rc1", "redhat-chart-1.0.0", "release-x"):
        _git("tag", tag, cwd=root)
    return base


@pytest.fixture
def client(monkeypatch, repo: Path) -> TestClient:
    monkeypatch.setenv("SOURCE_MCP_API_KEY", "x" * 32)
    monkeypatch.setenv("SOURCE_MCP_REPOS_BASE_DIR", str(repo))
    monkeypatch.setenv("SOURCE_MCP_REPOS", "dify")
    monkeypatch.setenv("SOURCE_MCP_SIGNOZ_MCP_URL", "http://signoz.invalid/mcp")
    for name in (
        "SOURCE_MCP_RUNTIME_REF_OVERRIDES",
        "SOURCE_MCP_RUNTIME_DEPLOYMENTS",
        "SOURCE_MCP_RUNTIME_PATH_DEPLOYMENTS",
        "SOURCE_MCP_GITHUB_TOKEN",
        "SOURCE_MCP_REPO_URLS",
        "SOURCE_MCP_REPO_HINTS",
    ):
        monkeypatch.delenv(name, raising=False)
    queried: list[tuple[str, str | None]] = []

    async def query_runtime_image(
        deployment: str, _settings: Settings, environment: str | None = None
    ) -> dict[str, str] | None:
        queried.append((deployment, environment))
        tag = RUNTIME_TAGS.get(deployment)
        return {"image_tag": tag, "environment": "production"} if tag else None

    monkeypatch.setattr(runtime, "_query_runtime_image", query_runtime_image)
    from source_mcp.app import create_app

    test_client = TestClient(create_app())
    test_client.queried = queried  # type: ignore[attr-defined]
    return test_client


def _call(client: TestClient, name: str, **arguments: Any) -> dict[str, Any]:
    response = client.post(
        "/mcp",
        headers=AUTH,
        json={
            "jsonrpc": "2.0",
            "id": name,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    assert response.status_code == 200
    return response.json()["result"]["structuredContent"]


# --- runtime deployment selection -------------------------------------------------


def test_read_under_web_uses_dify_web_runtime(client):
    data = _call(client, "source_read", repo="dify", filepath="web/app/page.tsx")["data"]

    assert data["deployment"] == "dify-web"
    assert data["ref"] == "1.9.0"
    assert data["deployment_selection"] == "path"
    assert "dify-web" in data["deployment_warning"]
    assert "dify-api-oltp" in data["deployment_warning"]
    assert data["ref_defaulted"] is True


def test_read_under_api_uses_default_deployment_without_warning(client):
    data = _call(client, "source_read", repo="dify", filepath="api/service.py")["data"]

    assert data["deployment"] == "dify-api-oltp"
    assert data["ref"] == "1.10.0-fix.1"
    assert data["deployment_selection"] == "default"
    assert "deployment_warning" not in data


def test_full_repo_search_keeps_default_and_warns_about_web(client):
    data = _call(client, "source_search", repo="dify", pattern="needle", limit=200)["data"]

    assert data["deployment"] == "dify-api-oltp"
    assert data["deployment_selection"] == "default"
    assert "deployment=dify-web" in data["deployment_warning"]
    assert "web/" in data["deployment_warning"]


def test_search_with_web_glob_uses_dify_web(client):
    data = _call(client, "source_search", repo="dify", pattern="needle", glob="web/**")["data"]

    assert data["deployment"] == "dify-web"
    assert data["ref"] == "1.9.0"
    assert [m["file"] for m in data["matches"]] == ["web/app/page.tsx"]


def test_search_with_api_glob_has_no_web_warning(client):
    data = _call(client, "source_search", repo="dify", pattern="needle", glob="api/**")["data"]

    assert data["deployment"] == "dify-api-oltp"
    assert "deployment_warning" not in data


def test_explicit_deployment_wins_over_path(client):
    data = _call(
        client, "source_read", repo="dify", filepath="web/app/page.tsx", deployment="dify-api-oltp"
    )["data"]

    assert data["deployment"] == "dify-api-oltp"
    assert data["deployment_selection"] == "explicit"


def test_explicit_ref_ignores_deployment_with_note(client):
    data = _call(
        client, "source_read", repo="dify", filepath="web/app/page.tsx", ref="1.2.0", deployment="dify-web"
    )
    assert data["success"] is False  # web/ does not exist at 1.2.0
    data = _call(
        client, "source_read", repo="dify", filepath="api/service.py", ref="1.2.0", deployment="dify-web"
    )["data"]
    assert data["source"] == "explicit"
    assert "ignored" in data["deployment_warning"]
    assert "ref_defaulted" not in data


def test_tree_and_log_under_web_use_dify_web(client):
    tree = _call(client, "source_tree", repo="dify", path="web")["data"]
    log = _call(client, "source_log", repo="dify", filepath="web/app/page.tsx")["data"]

    assert tree["deployment"] == "dify-web"
    assert log["deployment"] == "dify-web"


def test_diff_runtime_side_accepts_deployment(client):
    data = _call(
        client, "source_diff", repo="dify", ref1="1.2.0", ref2="runtime", deployment="dify-web", mode="name-status"
    )["data"]

    assert data["ref2"] == "1.9.0"
    assert data["runtime"]["ref2"]["deployment"] == "dify-web"
    assert "web/app/page.tsx" in data["output"]


def test_select_path_deployment_rules():
    settings = Settings(_env_file=None)

    def kind(path, pattern=False):
        return runtime.select_path_deployment(settings, "dify", path, is_pattern=pattern)["kind"]

    assert kind("web/app/page.tsx") == "match"
    assert kind("web") == "match"
    assert kind("./web/app") == "match"
    assert kind("api/app.py") == "none"
    assert kind("webapp/x.ts") == "none"
    assert kind(None) == "spans"
    assert kind("web/**/*.tsx", True) == "match"
    assert kind("web", True) == "match"
    assert kind("**/*.tsx", True) == "spans"
    assert kind("we*", True) == "spans"
    assert kind(None, True) == "spans"
    assert kind("api/**", True) == "none"
    assert kind(":(icase)web/**", True) == "spans"
    assert runtime.select_path_deployment(settings, "dify-sandbox", "web/x")["kind"] == "none"


# --- search pagination ----------------------------------------------------------


def test_search_limit_counts_matching_lines_and_paginates(client):
    first = _call(client, "source_search", repo="dify", ref="HEAD", pattern="needle", limit=3)["data"]

    assert first["total_matches"] == 27
    assert first["total_exact"] is True
    assert first["returned"] == 3
    assert len(first["matches"]) == 3
    assert first["has_more"] is True
    assert first["next_offset"] == 3
    assert first["files_with_matches"] == 3
    assert "offset=3" in first["output"]

    seen = [(m["file"], m["line"]) for m in first["matches"]]
    offset = first["next_offset"]
    while offset is not None:
        page = _call(
            client, "source_search", repo="dify", ref="HEAD", pattern="needle", limit=10, offset=offset
        )["data"]
        seen.extend((m["file"], m["line"]) for m in page["matches"])
        offset = page["next_offset"]
    assert len(seen) == len(set(seen)) == 27
    assert page["has_more"] is False


def test_search_parses_paths_containing_colons(client):
    data = _call(
        client, "source_search", repo="dify", ref="HEAD", pattern="colon path", mode="fixed"
    )["data"]

    assert data["matches"] == [{"file": "docs/a:b.md", "line": 2, "text": "needle in a colon path"}]


def test_search_structured_context(client):
    data = _call(
        client, "source_search", repo="dify", ref="HEAD", pattern="needle 2$", context=1
    )["data"]

    match = data["matches"][0]
    assert (match["file"], match["line"], match["text"]) == ("api/many.py", 4, "needle 2")
    assert match["context_before"] == [{"line": 3, "text": "before 2"}]
    assert match["context_after"] == [{"line": 5, "text": "before 3"}]
    assert "3-before 2" in data["output"]
    assert "4:needle 2" in data["output"]


def test_search_limit_is_capped(client):
    data = _call(client, "source_search", repo="dify", ref="HEAD", pattern="needle", limit=5000)["data"]

    assert data["limit"] == 200
    assert data["returned"] == 27


def test_search_marks_inexact_total_when_output_is_capped(client, monkeypatch):
    from source_mcp import git_ops

    monkeypatch.setattr(git_ops, "_SEARCH_OUTPUT_CAP", 200)
    data = _call(client, "source_search", repo="dify", ref="HEAD", pattern="needle", limit=200)["data"]

    assert data["total_exact"] is False
    assert data["has_more"] is True
    assert 0 < data["total_matches"] < 27
    assert "lower bound" in data["total_note"]


def test_search_rejects_negative_offset(client):
    payload = _call(client, "source_search", repo="dify", ref="HEAD", pattern="needle", offset=-1)

    assert payload["code"] == "BAD_ARGUMENT"


# --- source_tree ----------------------------------------------------------------


def test_tree_lists_root_entries(client):
    data = _call(client, "source_tree", repo="dify", ref="HEAD")["data"]

    entries = {entry["path"]: entry["type"] for entry in data["entries"]}
    assert entries == {"api": "tree", "docs": "tree", "web": "tree"}
    assert data["total"] == 3
    assert data["has_more"] is False


def test_tree_recursive_with_sizes_and_pagination(client):
    data = _call(client, "source_tree", repo="dify", ref="HEAD", path="web/", recursive=True)["data"]

    paths = [entry["path"] for entry in data["entries"]]
    assert paths == ["web/app", "web/app/layout.tsx", "web/app/page.tsx"]
    page = next(entry for entry in data["entries"] if entry["path"] == "web/app/page.tsx")
    assert page["type"] == "blob"
    assert page["size"] == len("export const needle = 'web';\n")

    first = _call(client, "source_tree", repo="dify", ref="HEAD", path="api", limit=1)["data"]
    assert first["returned"] == 1 and first["has_more"] is True and first["next_offset"] == 1
    second = _call(client, "source_tree", repo="dify", ref="HEAD", path="api", limit=1, offset=1)["data"]
    assert second["has_more"] is False
    assert {first["entries"][0]["path"], second["entries"][0]["path"]} == {
        "api/many.py",
        "api/service.py",
    }


def test_tree_on_file_returns_single_blob(client):
    data = _call(client, "source_tree", repo="dify", ref="HEAD", path="api/service.py")["data"]

    assert data["object_type"] == "blob"
    assert [entry["path"] for entry in data["entries"]] == ["api/service.py"]


@pytest.mark.parametrize("bad_path", ["../etc", "/etc", "api/../.."])
def test_tree_rejects_escaping_paths(client, bad_path):
    payload = _call(client, "source_tree", repo="dify", ref="HEAD", path=bad_path)

    assert payload["code"] == "BAD_FILEPATH"


def test_tree_reports_missing_path(client):
    payload = _call(client, "source_tree", repo="dify", ref="HEAD", path="nope")

    assert payload["code"] == "PATH_NOT_FOUND"


# --- list_repo_tags -------------------------------------------------------------


def test_tags_sort_versions_first(client):
    data = _call(client, "list_repo_tags", repo="dify", limit=50)["data"]

    assert data["tags"] == [
        "1.10.0-fix.1",
        "v1.10.0",
        "1.10.0-rc1",
        "1.9.0",
        "1.2.0",
        "redhat-chart-1.0.0",
        "release-x",
    ]
    assert data["version_tags"] == 6
    assert data["non_version_tags"] == 1


def test_tags_sort_prefixed_versions_with_their_version():
    from source_mcp.git_ops import _version_sort_key

    tags = ["redhat-chart-3.9.8-run-3", "3.13.2", "dify-3.9.15", "dify-3.13.2", "backend/2.3.2", "main"]
    ordered = sorted((t for t in tags if _version_sort_key(t)), key=_version_sort_key, reverse=True)

    assert ordered == ["3.13.2", "dify-3.13.2", "dify-3.9.15", "redhat-chart-3.9.8-run-3", "backend/2.3.2"]
    assert _version_sort_key("main") is None


def test_tags_pattern_filters(client):
    data = _call(client, "list_repo_tags", repo="dify", pattern="1.*")["data"]

    assert data["tags"] == ["1.10.0-fix.1", "1.10.0-rc1", "1.9.0", "1.2.0"]
    assert data["total"] == 4


def test_tags_reject_option_like_pattern(client):
    payload = _call(client, "list_repo_tags", repo="dify", pattern="--contains")

    assert payload["code"] == "BAD_ARGUMENT"


# --- unknown repos and hints ----------------------------------------------------


def test_unknown_repo_lists_configured_repos(client):
    payload = _call(client, "source_read", repo="nope", filepath="a.py", ref="HEAD")

    assert payload["code"] == "UNKNOWN_REPO"
    assert "Configured repos: dify" in payload["error"]
    assert "SOURCE_MCP_REPOS" in payload["error"]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("gateway", ["dify-enterprise", "server/cmd/gateway/", "unconfirmed"]),
        ("dify-gateway", ["dify-enterprise", "server/pkg/gateway/"]),
        ("sandbox-gateway", ["server/cmd/sandbox-gateway/"]),
        ("ops-web", ["dify-marketplace", "ops/"]),
        ("billing", ["repo dify-billing", "not configured here"]),
    ],
)
def test_unknown_repo_suggests_hints(client, name, expected):
    payload = _call(client, "source_tree", repo=name, ref="HEAD")

    assert payload["code"] == "UNKNOWN_REPO"
    for snippet in expected:
        assert snippet in payload["error"]


def test_repo_hints_are_configurable(monkeypatch):
    from source_mcp import git_ops

    monkeypatch.setenv("SOURCE_MCP_REPOS", "dify")
    monkeypatch.setenv("SOURCE_MCP_REPO_HINTS", '{"foo": "foo lives in dify/api/foo"}')
    message = git_ops.unknown_repo_message(Settings(_env_file=None), "foo")

    assert "foo lives in dify/api/foo" in message
