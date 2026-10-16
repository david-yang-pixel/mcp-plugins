from __future__ import annotations

import asyncio
import signal
from pathlib import Path

import anyio
import pytest

from source_mcp import git_ops
from source_mcp.git_ops import SourceError
from source_mcp.settings import Settings


def test_run_git_cancellation_kills_and_reaps_process_group(monkeypatch, tmp_path):
    monkeypatch.setenv("SOURCE_MCP_REPOS_BASE_DIR", str(tmp_path))
    settings = Settings()
    killed: list[tuple[int, signal.Signals]] = []
    subprocess_kwargs: dict[str, object] = {}

    class FakeProcess:
        def __init__(self) -> None:
            self.pid = 4321
            self.returncode: int | None = None
            self.started = asyncio.Event()
            self.finished = asyncio.Event()
            self.communicate_calls = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            self.communicate_calls += 1
            self.started.set()
            await self.finished.wait()
            return b"", b""

        def kill(self) -> None:
            self.returncode = -signal.SIGKILL
            self.finished.set()

    async def exercise() -> None:
        fake_process = FakeProcess()

        async def create_subprocess_exec(*_args: object, **kwargs: object) -> FakeProcess:
            subprocess_kwargs.update(kwargs)
            return fake_process

        def killpg(pid: int, sig: signal.Signals) -> None:
            killed.append((pid, sig))
            fake_process.returncode = -sig
            fake_process.finished.set()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", create_subprocess_exec)
        monkeypatch.setattr(git_ops.os, "killpg", killpg)

        task = asyncio.create_task(
            git_ops.run_git(tmp_path, "status", settings=settings)
        )
        await fake_process.started.wait()
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        assert killed == [(fake_process.pid, signal.SIGKILL)]
        assert fake_process.communicate_calls == 1

    anyio.run(exercise)

    assert subprocess_kwargs["start_new_session"] is True


def test_search_source_disables_lazy_fetch_and_reports_missing_blobs(monkeypatch, tmp_path):
    monkeypatch.setenv("SOURCE_MCP_REPOS_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("SOURCE_MCP_REPOS", "dify")
    settings = Settings()
    lazy_fetch_modes: list[bool] = []

    async def ensure_repo(_repo: str, _settings: Settings) -> Path:
        return tmp_path

    async def resolve_ref(_repo: str, _ref: str, _settings: Settings) -> dict[str, str]:
        return {"repo": "dify", "ref": "runtime-ref", "commit": "a" * 40}

    async def run_git(
        _path: Path,
        *_args: str,
        settings: Settings,
        max_output: int,
        timeout_seconds: float | None = None,
        use_github_token: bool = True,
        allow_lazy_fetch: bool = True,
    ) -> tuple[int, str, str]:
        del settings, max_output, timeout_seconds, use_github_token
        lazy_fetch_modes.append(allow_lazy_fetch)
        return 128, "", "warning: lazy fetching disabled; some objects may not be available"

    monkeypatch.setattr(git_ops, "ensure_repo", ensure_repo)
    monkeypatch.setattr(git_ops, "_resolve_ref", resolve_ref)
    monkeypatch.setattr(git_ops, "run_git", run_git)

    async def search() -> None:
        await git_ops.search_source(
            repo="dify",
            ref="runtime",
            pattern="needle",
            glob=None,
            limit=10,
            settings=settings,
        )

    with pytest.raises(SourceError) as exc_info:
        anyio.run(search)

    assert lazy_fetch_modes == [False]
    assert exc_info.value.code == "REPO_BLOBS_MISSING"


@pytest.mark.parametrize(
    ("configured_concurrency", "expected_max_active"),
    [(None, 1), ("2", 2)],
)
def test_full_repo_searches_respect_configured_concurrency(
    monkeypatch,
    tmp_path,
    configured_concurrency,
    expected_max_active,
):
    monkeypatch.setenv("SOURCE_MCP_REPOS_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("SOURCE_MCP_REPOS", "dify")
    if configured_concurrency is None:
        monkeypatch.delenv("SOURCE_MCP_FULL_REPO_SEARCH_CONCURRENCY", raising=False)
    else:
        monkeypatch.setenv(
            "SOURCE_MCP_FULL_REPO_SEARCH_CONCURRENCY",
            configured_concurrency,
        )
    settings = Settings()

    async def ensure_repo(_repo: str, _settings: Settings) -> Path:
        return tmp_path

    async def resolve_ref(_repo: str, _ref: str, _settings: Settings) -> dict[str, str]:
        return {"repo": "dify", "ref": "runtime-ref", "commit": "a" * 40}

    async def exercise() -> int:
        active = 0
        max_active = 0

        async def run_git(
            _path: Path,
            *_args: str,
            settings: Settings,
            max_output: int,
            timeout_seconds: float | None = None,
            use_github_token: bool = True,
            allow_lazy_fetch: bool = True,
        ) -> tuple[int, str, str]:
            nonlocal active, max_active
            del settings, max_output, timeout_seconds, use_github_token, allow_lazy_fetch
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.02)
            active -= 1
            return 0, "match", ""

        monkeypatch.setattr(git_ops, "ensure_repo", ensure_repo)
        monkeypatch.setattr(git_ops, "_resolve_ref", resolve_ref)
        monkeypatch.setattr(git_ops, "run_git", run_git)

        await asyncio.gather(
            git_ops.search_source(
                repo="dify",
                ref="runtime",
                pattern="first",
                glob=None,
                limit=10,
                settings=settings,
            ),
            git_ops.search_source(
                repo="dify",
                ref="runtime",
                pattern="second",
                glob=None,
                limit=10,
                settings=settings,
            ),
        )
        return max_active

    assert anyio.run(exercise) == expected_max_active


def test_rev_parse_preserves_remote_lookup_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("SOURCE_MCP_REPOS_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("SOURCE_MCP_REPOS", "dify-billing")
    settings = Settings()
    calls: list[tuple[str, ...]] = []

    async def ensure_repo(_repo: str, _settings: Settings) -> Path:
        return tmp_path

    async def can_auto_fetch(_path: Path, _repo: str, _settings: Settings) -> bool:
        return True

    async def configure_origin(_path: Path, _repo: str, _settings: Settings) -> None:
        return None

    async def run_git(
        _path: Path,
        *args: str,
        settings: Settings,
        max_output: int,
        timeout_seconds: float | None = None,
        use_github_token: bool = True,
    ) -> tuple[int, str, str]:
        del settings, max_output, timeout_seconds, use_github_token
        calls.append(args)
        if args[0] == "rev-parse":
            return 1, "", ""
        return 128, "", "remote transport failed"

    monkeypatch.setattr(git_ops, "ensure_repo", ensure_repo)
    monkeypatch.setattr(git_ops, "_can_auto_fetch", can_auto_fetch)
    monkeypatch.setattr(git_ops, "_configure_origin", configure_origin)
    monkeypatch.setattr(git_ops, "run_git", run_git)

    async def resolve() -> None:
        await git_ops.rev_parse(
            "dify-billing",
            "release-20260708.1",
            settings,
        )

    with pytest.raises(SourceError) as exc_info:
        anyio.run(resolve)

    assert exc_info.value.code == "GIT_FETCH_FAILED"
    assert any(call[0] == "ls-remote" for call in calls)


def test_rev_parse_uses_one_remote_lookup_to_confirm_missing_ref(monkeypatch, tmp_path):
    monkeypatch.setenv("SOURCE_MCP_REPOS_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("SOURCE_MCP_REPOS", "dify-billing")
    settings = Settings()
    calls: list[tuple[str, ...]] = []
    timeouts: list[float | None] = []

    async def ensure_repo(_repo: str, _settings: Settings) -> Path:
        return tmp_path

    async def can_auto_fetch(_path: Path, _repo: str, _settings: Settings) -> bool:
        return True

    async def configure_origin(_path: Path, _repo: str, _settings: Settings) -> None:
        return None

    async def run_git(
        _path: Path,
        *args: str,
        settings: Settings,
        max_output: int,
        timeout_seconds: float | None = None,
        use_github_token: bool = True,
    ) -> tuple[int, str, str]:
        del settings, max_output, use_github_token
        calls.append(args)
        timeouts.append(timeout_seconds)
        if args[0] == "rev-parse":
            return 1, "", ""
        if args[0] == "ls-remote":
            return 2, "", ""
        raise AssertionError(f"unexpected Git command: {args}")

    monkeypatch.setattr(git_ops, "ensure_repo", ensure_repo)
    monkeypatch.setattr(git_ops, "_can_auto_fetch", can_auto_fetch)
    monkeypatch.setattr(git_ops, "_configure_origin", configure_origin)
    monkeypatch.setattr(git_ops, "run_git", run_git)

    async def resolve() -> None:
        await git_ops.rev_parse(
            "dify-billing",
            "release-20260708.1",
            settings,
        )

    with pytest.raises(SourceError) as exc_info:
        anyio.run(resolve)

    assert exc_info.value.code == "REF_NOT_FOUND"
    assert [call[0] for call in calls].count("ls-remote") == 1
    assert all(call[0] != "fetch" for call in calls)
    ls_remote_index = next(index for index, call in enumerate(calls) if call[0] == "ls-remote")
    assert timeouts[ls_remote_index] == 20.0


def test_rev_parse_retries_default_git_credentials_after_rejected_token(monkeypatch, tmp_path):
    monkeypatch.setenv("SOURCE_MCP_REPOS_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("SOURCE_MCP_REPOS", "dify-billing")
    monkeypatch.setenv("SOURCE_MCP_GITHUB_TOKEN", "rejected-token")
    settings = Settings()
    auth_modes: list[bool] = []

    async def ensure_repo(_repo: str, _settings: Settings) -> Path:
        return tmp_path

    async def can_auto_fetch(_path: Path, _repo: str, _settings: Settings) -> bool:
        return True

    async def configure_origin(_path: Path, _repo: str, _settings: Settings) -> None:
        return None

    async def run_git(
        _path: Path,
        *args: str,
        settings: Settings,
        max_output: int,
        timeout_seconds: float | None = None,
        use_github_token: bool = True,
    ) -> tuple[int, str, str]:
        del settings, max_output, timeout_seconds
        if args[0] == "rev-parse":
            return 1, "", ""
        assert args[0] == "ls-remote"
        auth_modes.append(use_github_token)
        if use_github_token:
            return 128, "", "The requested URL returned error: 403"
        return 2, "", ""

    monkeypatch.setattr(git_ops, "ensure_repo", ensure_repo)
    monkeypatch.setattr(git_ops, "_can_auto_fetch", can_auto_fetch)
    monkeypatch.setattr(git_ops, "_configure_origin", configure_origin)
    monkeypatch.setattr(git_ops, "run_git", run_git)

    async def resolve() -> None:
        await git_ops.rev_parse("dify-billing", "release-20260708.1", settings)

    with pytest.raises(SourceError) as exc_info:
        anyio.run(resolve)

    assert exc_info.value.code == "REF_NOT_FOUND"
    assert auth_modes == [True, False]


def test_git_env_restores_home_without_rejected_token(monkeypatch):
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.setenv("SOURCE_MCP_GITHUB_TOKEN", "rejected-token")
    monkeypatch.setenv("GIT_ASKPASS", "/tmp/rejected-askpass")
    settings = Settings()

    env = git_ops._git_env(settings, use_github_token=False)

    assert env["HOME"] == str(Path.home())
    assert "SOURCE_MCP_GITHUB_TOKEN" not in env
    assert "GIT_ASKPASS" not in env


def test_git_env_can_disable_lazy_blob_fetch(monkeypatch):
    monkeypatch.delenv("GIT_NO_LAZY_FETCH", raising=False)
    settings = Settings()

    env = git_ops._git_env(settings, allow_lazy_fetch=False)

    assert env["GIT_NO_LAZY_FETCH"] == "1"
