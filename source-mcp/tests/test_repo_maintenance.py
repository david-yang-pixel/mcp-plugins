from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import anyio
import pytest
from fastapi.testclient import TestClient

from source_mcp import git_ops
from source_mcp.settings import Settings


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ("git", *args), cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _make_origin(path: Path, commits: int = 3) -> Path:
    path.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=path)
    _git("config", "user.email", "test@example.com", cwd=path)
    _git("config", "user.name", "Test User", cwd=path)
    for number in range(commits):
        (path / "file.txt").write_text(f"v{number}\n", encoding="utf-8")
        _git("add", ".", cwd=path)
        _git("commit", "-q", "-m", f"c{number}", cwd=path)
    return path


@pytest.fixture
def env(monkeypatch, tmp_path: Path) -> dict[str, Any]:
    origin = _make_origin(tmp_path / "origin")
    base = tmp_path / "repos"
    base.mkdir()
    _git("clone", "-q", "--depth=1", origin.as_uri(), str(base / "dify"), cwd=tmp_path)
    monkeypatch.setenv("SOURCE_MCP_API_KEY", "x" * 32)
    monkeypatch.setenv("SOURCE_MCP_REPOS_BASE_DIR", str(base))
    monkeypatch.setenv("SOURCE_MCP_REPOS", "dify,other,missing")
    monkeypatch.setenv(
        "SOURCE_MCP_REPO_URLS",
        json.dumps(
            {
                "dify": origin.as_uri(),
                "other": origin.as_uri(),
                "missing": (tmp_path / "does-not-exist").as_uri(),
            }
        ),
    )
    monkeypatch.setenv("SOURCE_MCP_RUNTIME_REF_OVERRIDES", '{"dify":"HEAD","other":"HEAD"}')
    monkeypatch.delenv("SOURCE_MCP_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("SOURCE_MCP_FULL_HISTORY_REPOS", raising=False)
    return {"origin": origin, "base": base, "tmp": tmp_path}


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


def _client() -> TestClient:
    from source_mcp.app import create_app
    from source_mcp.settings import get_settings

    get_settings.cache_clear()

    return TestClient(create_app())


def _shallow(path: Path) -> bool:
    return _git("rev-parse", "--is-shallow-repository", cwd=path) == "true"


def test_refresh_isolates_failing_repo(env):
    payload = _call(_client(), "refresh_repos")

    assert payload["success"] is True
    by_repo = {item["repo"]: item for item in payload["data"]}
    assert by_repo["dify"]["ok"] is True
    assert by_repo["other"]["ok"] is True
    assert by_repo["other"]["message"] == "cloned"
    assert by_repo["missing"]["ok"] is False
    assert by_repo["missing"]["code"] == "GIT_CLONE_FAILED"
    assert not (env["base"] / "missing").exists()


def test_refresh_single_failing_repo_still_errors(env):
    payload = _call(_client(), "refresh_repos", repos=["missing"])

    assert payload["success"] is False
    assert payload["code"] == "GIT_CLONE_FAILED"


def test_refresh_unknown_repo_in_list_is_reported_per_repo(env):
    payload = _call(_client(), "refresh_repos", repos=["dify", "rbac"])

    by_repo = {item["repo"]: item for item in payload["data"]}
    assert by_repo["dify"]["ok"] is True
    assert by_repo["rbac"]["code"] == "UNKNOWN_REPO"
    assert "Configured repos: dify, other, missing" in by_repo["rbac"]["error"]


def test_repo_status_isolates_broken_repo(env):
    broken = env["base"] / "other"
    broken.mkdir()
    (broken / "HEAD").write_text("garbage\n", encoding="utf-8")

    payload = _call(_client(), "repo_status")

    assert payload["success"] is True
    by_repo = {item["repo"]: item for item in payload["data"]}
    assert by_repo["dify"]["cloned"] is True
    assert by_repo["other"]["ok"] is False
    assert by_repo["other"]["error"]
    assert by_repo["missing"]["cloned"] is False
    assert by_repo["missing"]["full_history"] is True


def test_new_clone_respects_full_history_setting(env, monkeypatch):
    monkeypatch.setenv("SOURCE_MCP_FULL_HISTORY_REPOS", "dify")
    monkeypatch.setenv("SOURCE_MCP_REPOS", "dify,other")
    _call(_client(), "refresh_repos", repos=["other"])

    assert _shallow(env["base"] / "other") is True
    status = {item["repo"]: item for item in _call(_client(), "repo_status")["data"]}
    assert status["other"]["full_history"] is False
    assert status["dify"]["full_history"] is True

    monkeypatch.setenv("SOURCE_MCP_FULL_HISTORY_REPOS", "*")
    full = env["base"] / "full"
    monkeypatch.setenv("SOURCE_MCP_REPOS", "full")
    monkeypatch.setenv("SOURCE_MCP_REPO_URLS", json.dumps({"full": env["origin"].as_uri()}))
    _call(_client(), "refresh_repos", repos=["full"])
    assert _shallow(full) is False


def test_settings_full_history_parsing(monkeypatch):
    monkeypatch.setenv("SOURCE_MCP_FULL_HISTORY_REPOS", "dify, saas-deploy")
    settings = Settings(_env_file=None)
    assert settings.is_full_history_repo("dify") is True
    assert settings.is_full_history_repo("saas-deploy") is True
    assert settings.is_full_history_repo("dify-helm") is False
    monkeypatch.delenv("SOURCE_MCP_FULL_HISTORY_REPOS")
    assert Settings(_env_file=None).is_full_history_repo("anything") is True


def test_background_refresh_unshallows_full_history_repo(env, monkeypatch):
    monkeypatch.setenv("SOURCE_MCP_REPOS", "dify")
    monkeypatch.setenv("SOURCE_MCP_BACKGROUND_REFRESH_SECONDS", "3600")
    settings = Settings(_env_file=None)
    path = env["base"] / "dify"
    assert _shallow(path) is True

    result = anyio.run(git_ops.background_refresh_repo, "dify", settings)

    assert result["error"] is None
    # `clone --depth=1` implies --single-branch; the narrow refspec is repaired first.
    assert result["actions"] == ["refspec-repaired", "fetched", "unshallowed"]
    assert _shallow(path) is False
    assert len(_git("log", "--oneline", "HEAD", cwd=path).splitlines()) == 3

    status = anyio.run(git_ops.status_for_repo, "dify", settings)
    assert status["stale"] is False
    assert status["stale_after_seconds"] == 7200
    assert status["background_refresh"]["enabled"] is True
    assert status["background_refresh"]["last_error"] is None
    assert status["background_refresh"]["last_success_at"]

    # Fetched within SOURCE_MCP_FETCH_TTL_SECONDS: nothing to do on the next run.
    again = anyio.run(git_ops.background_refresh_repo, "dify", settings)
    assert again["actions"] == ["fresh"]


def test_background_refresh_keeps_non_full_history_repo_shallow(env, monkeypatch):
    monkeypatch.setenv("SOURCE_MCP_REPOS", "dify")
    monkeypatch.setenv("SOURCE_MCP_FULL_HISTORY_REPOS", "other")
    settings = Settings(_env_file=None)

    result = anyio.run(git_ops.background_refresh_repo, "dify", settings)

    assert result["actions"] == ["refspec-repaired", "fetched"]
    assert _shallow(env["base"] / "dify") is True


def test_background_refresh_isolates_errors(env, monkeypatch):
    monkeypatch.setenv("SOURCE_MCP_BACKGROUND_REFRESH_SECONDS", "3600")
    settings = Settings(_env_file=None)

    results = anyio.run(git_ops.background_refresh_all, settings)

    by_repo = {item["repo"]: item for item in results}
    assert by_repo["dify"]["error"] is None
    assert by_repo["other"]["actions"] == ["cloned"]
    assert "missing" in by_repo["missing"]["error"]
    status = anyio.run(git_ops.status_for_repo, "missing", settings)
    assert status["cloned"] is False
    assert status["background_refresh"]["last_error"]
    assert status["background_refresh"]["last_error_at"]


def test_repo_status_marks_stale_fetch(env, monkeypatch):
    monkeypatch.setenv("SOURCE_MCP_REPOS", "dify")
    monkeypatch.setenv("SOURCE_MCP_BACKGROUND_REFRESH_SECONDS", "3600")
    settings = Settings(_env_file=None)
    fetch_head = env["base"] / "dify" / ".git" / "FETCH_HEAD"
    fetch_head.write_text("", encoding="utf-8")
    old = time.time() - 3 * 3600
    os.utime(fetch_head, (old, old))

    status = anyio.run(git_ops.status_for_repo, "dify", settings)

    assert status["stale"] is True
    assert status["last_fetch_at_iso"].endswith("Z")


def test_repo_status_stale_is_null_when_background_disabled(env):
    settings = Settings(_env_file=None)

    status = anyio.run(git_ops.status_for_repo, "dify", settings)

    assert status["stale"] is None
    assert status["background_refresh"]["enabled"] is False


def test_background_loop_disabled_returns_immediately(monkeypatch):
    monkeypatch.setenv("SOURCE_MCP_BACKGROUND_REFRESH_SECONDS", "0")

    async def run() -> None:
        await asyncio.wait_for(git_ops.background_refresh_loop(Settings(_env_file=None)), 1)

    anyio.run(run)


def test_background_loop_survives_failing_run(monkeypatch):
    monkeypatch.setenv("SOURCE_MCP_BACKGROUND_REFRESH_SECONDS", "3600")
    calls: list[int] = []

    async def failing_run(_settings: Settings) -> list[dict[str, Any]]:
        calls.append(1)
        raise RuntimeError("boom")

    monkeypatch.setattr(git_ops, "background_refresh_all", failing_run)

    async def run() -> None:
        task = asyncio.create_task(
            git_ops.background_refresh_loop(Settings(_env_file=None), initial_delay=0)
        )
        for _ in range(100):
            if calls:
                break
            await asyncio.sleep(0.01)
        assert not task.done()  # the error was swallowed and the loop is sleeping
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    anyio.run(run)
    assert calls == [1]


def test_lifespan_starts_and_stops_background_loop(env, monkeypatch):
    from source_mcp import app as app_module

    events: list[str] = []

    async def fake_loop(settings: Settings) -> None:
        events.append(f"start:{settings.background_refresh_seconds}")
        try:
            await asyncio.Event().wait()
        finally:
            events.append("stop")

    monkeypatch.setattr(app_module, "background_refresh_loop", fake_loop)
    monkeypatch.setenv("SOURCE_MCP_BACKGROUND_REFRESH_SECONDS", "120")
    with TestClient(app_module.create_app()) as client:
        assert client.get("/health").json()["background_refresh_seconds"] == 120
    assert events == ["start:120", "stop"]

    events.clear()
    monkeypatch.setenv("SOURCE_MCP_BACKGROUND_REFRESH_SECONDS", "0")
    from source_mcp.settings import get_settings

    get_settings.cache_clear()
    with TestClient(app_module.create_app()):
        pass
    assert events == []


def test_single_branch_clone_gets_origin_main_updated(env, monkeypatch, tmp_path):
    """Regression: a clone made on a hotfix branch never updated origin/main."""
    origin = env["origin"]
    _git("checkout", "-q", "-b", "hotfix/1.15.0-fix.17", cwd=origin)
    _git("checkout", "-q", "main", cwd=origin)
    single = env["base"] / "other"
    _git(
        "clone", "-q", "--single-branch", "--branch", "hotfix/1.15.0-fix.17",
        origin.as_uri(), str(single), cwd=tmp_path,
    )
    # A stale local main left behind by an old on-demand fetch.
    _git("branch", "main", "HEAD", cwd=single)
    for number in range(2):
        (origin / "file.txt").write_text(f"main {number}\n", encoding="utf-8")
        _git("commit", "-qam", f"main {number}", cwd=origin)
    tip = _git("rev-parse", "main", cwd=origin)
    monkeypatch.setenv("SOURCE_MCP_REPOS", "other")
    settings = Settings(_env_file=None)

    before = anyio.run(git_ops.status_for_repo, "other", settings)
    assert before["fetch_refspec_ok"] is False

    result = anyio.run(git_ops.background_refresh_repo, "other", settings)

    assert result["error"] is None
    assert result["actions"][:2] == ["refspec-repaired", "fetched"]
    assert _git("rev-parse", "refs/remotes/origin/main", cwd=single) == tip
    status = anyio.run(git_ops.status_for_repo, "other", settings)
    assert status["fetch_refspec_ok"] is True
    assert status["commit"] == tip  # default branch, not the checked-out hotfix HEAD
    assert status["head_ref"] == "hotfix/1.15.0-fix.17"
    assert status["head_commit"] != tip
    assert status["default_branch_committed_at"]
    assert status["last_fetch_kind"] == "full"
    # A bare branch name resolves to the fetched remote-tracking branch, not the
    # stale local refs/heads/main.
    assert anyio.run(git_ops.rev_parse, "other", "main", settings) == tip


def test_single_ref_fetch_does_not_make_background_refresh_skip(env, monkeypatch):
    monkeypatch.setenv("SOURCE_MCP_REPOS", "dify")
    monkeypatch.setenv("SOURCE_MCP_FULL_HISTORY_REPOS", "other")
    settings = Settings(_env_file=None)
    path = env["base"] / "dify"
    # On-demand fetches of one ref rewrite FETCH_HEAD; that is not a full fetch.
    (path / ".git" / "FETCH_HEAD").write_text("", encoding="utf-8")
    anyio.run(git_ops._configure_origin, path, "dify", settings)

    result = anyio.run(git_ops.background_refresh_repo, "dify", settings)

    assert "fetched" in result["actions"]
    assert "fresh" not in result["actions"]
