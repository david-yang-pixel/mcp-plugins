from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ("git", *args), cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(repo: Path, relpath: str, content: str, message: str) -> str:
    target = repo / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _git("add", ".", cwd=repo)
    _git("commit", "-q", "-m", message, cwd=repo)
    return _git("rev-parse", "HEAD", cwd=repo)


@pytest.fixture
def history(tmp_path: Path) -> dict[str, Any]:
    origin = tmp_path / "origin"
    origin.mkdir()
    _git("init", "-q", "-b", "main", cwd=origin)
    _git("config", "user.email", "test@example.com", cwd=origin)
    _git("config", "user.name", "Test User", cwd=origin)
    pinned = _commit(origin, "api/service.py", "def answer_icon():\n    return 'v1'\n", "v1")
    middle = _commit(origin, "api/other.py", "VALUE = 42\n", "v2")
    head = _commit(
        origin,
        "api/service.py",
        "".join(f"line {number}\n" for number in range(1, 11))
        + "def answer_icon():\n    return 'Runtime Source'\n",
        "v3",
    )
    base = tmp_path / "repos"
    base.mkdir()
    _git("clone", "-q", "--depth=1", origin.as_uri(), str(base / "dify"), cwd=tmp_path)
    return {"origin": origin, "base": base, "pinned": pinned, "middle": middle, "head": head}


@pytest.fixture
def client(monkeypatch, history: dict[str, Any]) -> TestClient:
    monkeypatch.setenv("SOURCE_MCP_API_KEY", "x" * 32)
    monkeypatch.setenv("SOURCE_MCP_REPOS_BASE_DIR", str(history["base"]))
    monkeypatch.setenv("SOURCE_MCP_REPOS", "dify")
    monkeypatch.setenv(
        "SOURCE_MCP_REPO_URLS", json.dumps({"dify": history["origin"].as_uri()})
    )
    monkeypatch.setenv("SOURCE_MCP_RUNTIME_REF_OVERRIDES", '{"dify":"HEAD"}')
    monkeypatch.delenv("SOURCE_MCP_GITHUB_TOKEN", raising=False)

    from source_mcp.settings import get_settings

    get_settings.cache_clear()
    from source_mcp.app import create_app

    return TestClient(create_app())


def _call(client: TestClient, name: str, **arguments: Any) -> dict[str, Any]:
    response = client.post(
        "/mcp",
        headers={"Authorization": "Bearer " + "x" * 32},
        json={
            "jsonrpc": "2.0",
            "id": name,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    assert response.status_code == 200
    return response.json()["result"]["structuredContent"]


def test_read_fetches_pinned_full_commit_missing_from_shallow_clone(client, history):
    payload = _call(
        client, "source_read", repo="dify", filepath="api/service.py", ref=history["pinned"]
    )

    assert payload["success"] is True
    assert payload["data"]["commit"] == history["pinned"]
    assert "'v1'" in payload["data"]["content"]
    assert "ref_defaulted" not in payload["data"]


def test_abbreviated_commit_in_shallow_clone_explains_shallow_history(client, history):
    payload = _call(
        client, "source_read", repo="dify", filepath="api/service.py", ref=history["pinned"][:8]
    )

    assert payload["success"] is False
    assert payload["code"] == "REF_NOT_FOUND"
    assert "shallow clone" in payload["error"]
    assert "40-character" in payload["error"]


def test_refresh_reports_unchanged_fetch_and_shallow_state(client):
    payload = _call(client, "refresh_repos", repos=["dify"])

    data = payload["data"][0]
    assert data["message"] == "fetched"
    assert data["shallow"] is True
    assert data["changed"] is False
    assert any("no local ref or history changed" in warning for warning in data["warnings"])
    assert any("shallow clone" in warning for warning in data["warnings"])


def test_refresh_fetches_requested_commit_and_reports_change(client, history):
    payload = _call(client, "refresh_repos", repos=["dify"], refs=[history["middle"]])

    data = payload["data"][0]
    assert data["refs"] == [
        {"ref": history["middle"], "status": "fetched", "commit": history["middle"]}
    ]
    assert data["changed"] is True
    assert data["changes"]["added_refs"] == 1


def test_refresh_deepen_and_unshallow_change_history(client, history):
    deepened = _call(client, "refresh_repos", repos=["dify"], deepen=1)["data"][0]
    assert deepened["changes"]["history_changed"] is True
    assert deepened["shallow"] is True

    full = _call(client, "refresh_repos", repos=["dify"], unshallow=True)["data"][0]
    assert full["shallow"] is False
    assert full["changes"]["history_changed"] is True

    log = _call(client, "source_log", repo="dify", ref="HEAD")["data"]
    assert log["shallow"] is False
    assert len(log["output"].splitlines()) == 3
    assert "warning" not in log


def test_refresh_history_options_require_single_repo(client, history):
    payload = _call(client, "refresh_repos", refs=[history["middle"]])

    assert payload["success"] is False
    assert payload["code"] == "BAD_ARGUMENT"


def test_log_marks_shallow_truncated_history(client):
    data = _call(client, "source_log", repo="dify", ref="HEAD")["data"]

    assert data["shallow"] is True
    assert "shallow clone" in data["warning"]


@pytest.mark.parametrize(
    ("arguments", "expected_mode", "expected_snippets"),
    [
        ({"pattern": "answer_icon|VALUE"}, "regex", ["service.py", "other.py"]),
        ({"pattern": "(?i)runtime source"}, "regex", ["Runtime Source"]),
        ({"pattern": r"VALUE = \d+"}, "perl", ["VALUE = 42"]),
    ],
)
def test_search_supports_alternation_inline_case_and_perl(
    client, arguments, expected_mode, expected_snippets
):
    data = _call(client, "source_search", repo="dify", ref="HEAD", **arguments)["data"]

    assert data["mode"] == expected_mode
    assert data["matched"] is True
    for snippet in expected_snippets:
        assert snippet in data["output"]


def test_search_no_match_reports_mode_and_case(client):
    data = _call(
        client,
        "source_search",
        repo="dify",
        ref="HEAD",
        pattern="answer_icon|VALUE",
        mode="fixed",
        case_sensitive=True,
    )["data"]

    assert data["matched"] is False
    assert "mode=fixed" in data["output"]
    assert "case-sensitive" in data["output"]


def test_search_returns_context_lines(client):
    data = _call(
        client, "source_search", repo="dify", ref="HEAD", pattern="def answer_icon", context=1
    )["data"]

    assert "10-line 10" in data["output"]
    assert "12-    return 'Runtime Source'" in data["output"]


def test_search_rejects_bad_explicit_regex(client):
    payload = _call(
        client, "source_search", repo="dify", ref="HEAD", pattern="(unclosed", mode="regex"
    )

    assert payload["success"] is False
    assert payload["code"] == "BAD_PATTERN"


def test_read_line_range(client):
    data = _call(
        client,
        "source_read",
        repo="dify",
        filepath="api/service.py",
        ref="HEAD",
        start_line=3,
        end_line=4,
    )["data"]

    assert data["content"] == "line 3\nline 4\n"
    assert data["total_lines"] == 12
    assert (data["start_line"], data["end_line"], data["truncated"]) == (3, 4, False)


def test_read_truncates_large_file_at_line_boundary(client, history, monkeypatch):
    from source_mcp import git_ops

    monkeypatch.setattr(git_ops, "_MAX_READ_BYTES", 21)
    data = _call(client, "source_read", repo="dify", filepath="api/service.py", ref="HEAD")["data"]

    assert data["content"] == "line 1\nline 2\nline 3\n"
    assert data["truncated"] is True
    assert "start_line=4" in data["hint"]


def test_default_runtime_ref_is_flagged(client):
    data = _call(client, "source_read", repo="dify", filepath="api/service.py")["data"]

    assert data["ref_defaulted"] is True
    assert "Enterprise" in data["ref_warning"]


@pytest.mark.parametrize(
    ("arguments", "expected", "unexpected"),
    [
        ({}, "api/service.py", "@@"),
        ({"mode": "name-status"}, "A\tapi/other.py", "@@"),
        ({"filepath": "api", "mode": "stat"}, "2 files changed", "@@"),
        ({"filepath": "api/other.py"}, "+VALUE = 42", None),
    ],
)
def test_diff_modes(client, history, arguments, expected, unexpected):
    data = _call(
        client, "source_diff", repo="dify", ref1=history["pinned"], ref2="HEAD", **arguments
    )["data"]

    assert expected in data["output"]
    if unexpected:
        assert unexpected not in data["output"]


def test_auto_search_falls_back_to_fixed_string_and_says_so(client):
    data = _call(client, "source_search", repo="dify", ref="HEAD", pattern="answer_icon(")["data"]

    assert data["mode"] == "fixed"
    assert "searched as fixed string" in data["fallback"]
