from __future__ import annotations

import asyncio
import os
import re
import signal
import time
from contextlib import suppress
from pathlib import Path
from typing import Any, Optional

from .errors import SourceError
from .settings import Settings


_SSH_URL_RE = re.compile(r"^git@github\.com:(.+)$")
_COMPLEX_RE_PATTERN = re.compile(r"(\(\?[!=<]|\\[bBdDwWsS]|\{[\d,]+\}|[|()]{3,})")
_TAG_LIKE_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-_].*)?$")
_GREP_PATTERN_ERROR_MARKERS = (
    "fatal: -e option",
    "invalid regular expression",
    "unmatched",
    "invalid content of",
)
_MISSING_BLOB_ERROR_MARKERS = (
    "lazy fetching disabled",
    "promisor remote",
)
_FULL_REPO_SEARCH_SEMAPHORE: asyncio.Semaphore | None = None
_FULL_REPO_SEARCH_SEMAPHORE_LIMIT: int | None = None
_FULL_REPO_SEARCH_SEMAPHORE_LOOP: asyncio.AbstractEventLoop | None = None


def _full_repo_search_semaphore(settings: Settings) -> asyncio.Semaphore:
    global _FULL_REPO_SEARCH_SEMAPHORE
    global _FULL_REPO_SEARCH_SEMAPHORE_LIMIT
    global _FULL_REPO_SEARCH_SEMAPHORE_LOOP

    loop = asyncio.get_running_loop()
    limit = settings.full_repo_search_concurrency
    if (
        _FULL_REPO_SEARCH_SEMAPHORE is None
        or _FULL_REPO_SEARCH_SEMAPHORE_LIMIT != limit
        or _FULL_REPO_SEARCH_SEMAPHORE_LOOP is not loop
    ):
        _FULL_REPO_SEARCH_SEMAPHORE = asyncio.Semaphore(limit)
        _FULL_REPO_SEARCH_SEMAPHORE_LIMIT = limit
        _FULL_REPO_SEARCH_SEMAPHORE_LOOP = loop
    return _FULL_REPO_SEARCH_SEMAPHORE


def repo_path(settings: Settings, repo: str) -> Path:
    if repo not in settings.repo_names:
        raise SourceError("UNKNOWN_REPO", f"unknown repo: {repo}")
    base = Path(settings.repos_base_dir).resolve()
    path = (base / repo).resolve()
    if not str(path).startswith(str(base)):
        raise SourceError("BAD_REPO_PATH", f"repo escapes base dir: {repo}")
    return path


def is_git_repo(path: Path) -> bool:
    return path.exists() and ((path / "HEAD").is_file() or (path / ".git").exists())


async def run_git(
    path: Path,
    *args: str,
    settings: Settings,
    max_output: int = 30000,
    timeout_seconds: Optional[float] = None,
    use_github_token: bool = True,
    allow_lazy_fetch: bool = True,
) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(path), *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_git_env(
            settings,
            use_github_token=use_github_token,
            allow_lazy_fetch=allow_lazy_fetch,
        ),
        start_new_session=True,
    )
    communicate_task = asyncio.create_task(proc.communicate())
    try:
        out_b, err_b = await asyncio.wait_for(
            asyncio.shield(communicate_task),
            timeout=timeout_seconds or settings.git_timeout_seconds,
        )
    except asyncio.TimeoutError:
        out_b, err_b = await _kill_and_reap_process_group(proc, communicate_task)
        return 124, out_b.decode(errors="replace"), "git command timed out"
    except asyncio.CancelledError:
        await _kill_and_reap_process_group(proc, communicate_task)
        raise
    out = out_b.decode("utf-8", errors="replace")
    if len(out) > max_output:
        out = out[:max_output] + "\n\n... (output truncated)"
    return proc.returncode or 0, out, err_b.decode("utf-8", errors="replace")


async def _kill_and_reap_process_group(
    proc: asyncio.subprocess.Process,
    communicate_task: asyncio.Task[tuple[bytes, bytes]],
) -> tuple[bytes, bytes]:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        if proc.returncode is None:
            with suppress(ProcessLookupError):
                proc.kill()
    return await asyncio.shield(communicate_task)


async def ensure_repo(repo: str, settings: Settings) -> Path:
    path = repo_path(settings, repo)
    if is_git_repo(path):
        return path
    await _clone_repo(path, repo, settings)
    return path


async def refresh_repo(repo: str, settings: Settings) -> dict[str, Any]:
    path = repo_path(settings, repo)
    if not is_git_repo(path):
        await _clone_repo(path, repo, settings)
        message = "cloned"
    else:
        if not await _can_auto_fetch(path, repo, settings):
            message = "local-only"
        else:
            await _configure_origin(path, repo, settings)
            code, _, err = await run_git(
                path, "fetch", "--all", "--prune", "--tags", "--force",
                settings=settings,
                max_output=8000,
            )
            if code != 0:
                raise SourceError("GIT_FETCH_FAILED", err.strip())
            message = "fetched"
    return await status_for_repo(repo, settings, message=message)


async def refresh_repos(settings: Settings, repos: Optional[list[str]] = None) -> list[dict[str, Any]]:
    names = repos or settings.repo_names
    return [await refresh_repo(repo, settings) for repo in names]


async def status_for_repo(
    repo: str,
    settings: Settings,
    *,
    message: str = "",
) -> dict[str, Any]:
    path = repo_path(settings, repo)
    cloned = is_git_repo(path)
    data: dict[str, Any] = {
        "repo": repo,
        "path": str(path),
        "cloned": cloned,
        "message": message,
    }
    if not cloned:
        return data
    data["commit"] = await rev_parse(repo, "HEAD", settings)
    data["last_fetch_at"] = _fetch_head_mtime(path)
    data["default_branch_ref"] = await default_branch_ref(path, settings)
    code, tags, _ = await run_git(path, "tag", "-l", settings=settings, max_output=1_000_000)
    data["tag_count"] = len([line for line in tags.splitlines() if line.strip()]) if code == 0 else 0
    return data


async def default_branch_ref(path: Path, settings: Settings) -> str:
    code, out, _ = await run_git(
        path, "symbolic-ref", "refs/remotes/origin/HEAD",
        settings=settings,
        max_output=2000,
    )
    if code == 0 and out.strip().startswith("refs/remotes/"):
        return out.strip().removeprefix("refs/remotes/")
    return "origin/main"


async def rev_parse(
    repo: str,
    ref: str,
    settings: Settings,
) -> str:
    path = await ensure_repo(repo, settings)
    code, out, err = await run_git(
        path, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}",
        settings=settings,
        max_output=2000,
    )
    if code != 0:
        if await _can_auto_fetch(path, repo, settings):
            await _configure_origin(path, repo, settings)
            await _fetch_ref(path, ref, settings)
        code, out, err = await run_git(
            path, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}",
            settings=settings,
            max_output=2000,
        )
    if code != 0:
        raise SourceError("REF_NOT_FOUND", f"ref not found in {repo}: {ref}; {err.strip()}")
    return out.strip()


async def search_source(
    *,
    repo: str,
    ref: str,
    pattern: str,
    glob: Optional[str],
    limit: int,
    settings: Settings,
) -> dict[str, Any]:
    path = await ensure_repo(repo, settings)
    resolved = await _resolve_ref(repo, ref, settings)
    use_fixed = len(pattern) > 120 or _is_complex_regex(pattern)

    async def execute_search() -> tuple[int, str, str]:
        args = _grep_args(pattern=pattern, ref=resolved["ref"], glob=glob, fixed=use_fixed)
        result = await run_git(
            path,
            *args,
            settings=settings,
            max_output=limit * 1000,
            allow_lazy_fetch=False,
        )
        code, _, err = result
        if code not in (0, 1) and not use_fixed and _is_grep_pattern_error(err):
            args = _grep_args(pattern=pattern, ref=resolved["ref"], glob=glob, fixed=True)
            result = await run_git(
                path,
                *args,
                settings=settings,
                max_output=limit * 1000,
                allow_lazy_fetch=False,
            )
        return result

    if glob is None:
        async with _full_repo_search_semaphore(settings):
            code, out, err = await execute_search()
    else:
        code, out, err = await execute_search()
    if code == 1:
        out = f"no matches for {pattern!r}"
    elif code != 0:
        if _is_missing_blob_error(err):
            raise SourceError(
                "REPO_BLOBS_MISSING",
                f"{repo} is missing blobs required to search {resolved['ref']}",
            )
        raise SourceError("GIT_GREP_FAILED", err.strip())
    return {**resolved, "output": out}


async def read_source(
    *,
    repo: str,
    ref: str,
    filepath: str,
    settings: Settings,
) -> dict[str, Any]:
    _reject_bad_filepath(filepath)
    path = await ensure_repo(repo, settings)
    resolved = await _resolve_ref(repo, ref, settings)
    code, out, err = await run_git(
        path, "show", f"{resolved['ref']}:{filepath}",
        settings=settings,
        max_output=120_000,
    )
    if code != 0:
        raise SourceError("GIT_SHOW_FAILED", err.strip())
    return {**resolved, "filepath": filepath, "content": out}


async def source_log(
    *,
    repo: str,
    ref: str,
    filepath: Optional[str],
    n: int,
    settings: Settings,
) -> dict[str, Any]:
    path = await ensure_repo(repo, settings)
    resolved = await _resolve_ref(repo, ref, settings)
    args = ["log", resolved["ref"], f"-{max(1, min(n, 100))}", "--oneline", "--decorate"]
    if filepath:
        _reject_bad_filepath(filepath)
        args.extend(["--", filepath])
    code, out, err = await run_git(path, *args, settings=settings, max_output=60_000)
    if code != 0:
        raise SourceError("GIT_LOG_FAILED", err.strip())
    return {**resolved, "output": out}


async def source_diff(
    *,
    repo: str,
    ref1: str,
    ref2: str,
    filepath: Optional[str],
    settings: Settings,
) -> dict[str, Any]:
    path = await ensure_repo(repo, settings)
    resolved1 = await _resolve_ref(repo, ref1, settings)
    resolved2 = await _resolve_ref(repo, ref2, settings)
    args = ["diff", resolved1["ref"], resolved2["ref"], "--stat"]
    if filepath:
        _reject_bad_filepath(filepath)
        args = ["diff", resolved1["ref"], resolved2["ref"], "--", filepath]
    code, out, err = await run_git(path, *args, settings=settings, max_output=80_000)
    if code != 0:
        raise SourceError("GIT_DIFF_FAILED", err.strip())
    return {
        "repo": repo,
        "ref1": resolved1["ref"],
        "commit1": resolved1["commit"],
        "ref2": resolved2["ref"],
        "commit2": resolved2["commit"],
        "output": out or "no diff",
    }


async def list_repo_tags(repo: str, limit: int, settings: Settings) -> dict[str, Any]:
    path = await ensure_repo(repo, settings)
    code, out, err = await run_git(
        path, "tag", "-l", "--sort=-version:refname",
        settings=settings,
        max_output=100_000,
    )
    if code != 0:
        raise SourceError("GIT_TAGS_FAILED", err.strip())
    tags = [line.strip() for line in out.splitlines() if line.strip()]
    return {"repo": repo, "tags": tags[: max(1, min(limit, 200))], "total": len(tags)}


async def _resolve_ref(repo: str, ref: str, settings: Settings) -> dict[str, Any]:
    if not ref or ref == "runtime":
        from .runtime import resolve_runtime_ref

        return await resolve_runtime_ref(
            repo=repo,
            deployment=None,
            settings=settings,
            rev_parse_func=rev_parse,
        )
    commit = await rev_parse(repo, ref, settings)
    return {"repo": repo, "ref": ref, "commit": commit, "source": "explicit"}


async def _fetch_if_stale(path: Path, repo: str, settings: Settings) -> None:
    last_fetch = _fetch_head_mtime(path)
    if last_fetch is not None and (time.time() - last_fetch) < settings.fetch_ttl_seconds:
        return
    if not await _can_auto_fetch(path, repo, settings):
        return
    await _configure_origin(path, repo, settings)
    code, _, err = await run_git(
        path, "fetch", "--all", "--prune", "--tags", "--force",
        settings=settings,
        max_output=8000,
    )
    if code != 0:
        raise SourceError("GIT_FETCH_FAILED", f"{repo}: {err.strip()}")


async def _fetch_ref(path: Path, ref: str, settings: Settings) -> bool:
    branch = ref.removeprefix("origin/")
    tag_ref = f"refs/tags/{ref}"
    branch_ref = f"refs/heads/{branch}"
    remote_refs = [tag_ref, branch_ref] if _TAG_LIKE_RE.match(ref) else [branch_ref, tag_ref]
    lookup_args = (
        "ls-remote",
        "--exit-code",
        "origin",
        *remote_refs,
    )
    lookup_with_token = True
    code, out, err = await run_git(
        path,
        *lookup_args,
        settings=settings,
        max_output=8000,
        timeout_seconds=min(settings.git_timeout_seconds, 20.0),
    )
    if code not in (0, 2) and settings.github_token and _is_git_auth_failure(err):
        lookup_with_token = False
        code, out, err = await run_git(
            path,
            *lookup_args,
            settings=settings,
            max_output=8000,
            timeout_seconds=min(settings.git_timeout_seconds, 20.0),
            use_github_token=False,
        )
    if code == 2:
        return False
    if code != 0:
        raise SourceError("GIT_FETCH_FAILED", err.strip() or f"git ls-remote exited {code}")

    found_refs = {
        line.split(None, 1)[1].removesuffix("^{}")
        for line in out.splitlines()
        if len(line.split(None, 1)) == 2
    }
    source_ref = next((candidate for candidate in remote_refs if candidate in found_refs), None)
    if source_ref is None:
        raise SourceError("GIT_FETCH_FAILED", f"git ls-remote returned no usable ref for {ref}")

    if source_ref == tag_ref:
        destination = tag_ref
    elif ref.startswith("origin/"):
        destination = f"refs/remotes/origin/{branch}"
    else:
        destination = branch_ref
    code, _, err = await run_git(
        path,
        "fetch",
        "--depth=1",
        "--force",
        "origin",
        f"+{source_ref}:{destination}",
        settings=settings,
        max_output=8000,
        use_github_token=lookup_with_token,
    )
    if code != 0:
        raise SourceError("GIT_FETCH_FAILED", err.strip() or f"git fetch exited {code}")
    return True


async def _clone_repo(path: Path, repo: str, settings: Settings) -> None:
    url = _repo_url(repo, settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
        "git", "clone", url, str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_git_env(settings),
    )
    _, err_b = await proc.communicate()
    if proc.returncode != 0:
        raise SourceError("GIT_CLONE_FAILED", err_b.decode("utf-8", errors="replace").strip())


async def _configure_origin(path: Path, repo: str, settings: Settings) -> None:
    url = _repo_url(repo, settings)
    code, out, _ = await run_git(
        path, "remote", "get-url", "origin",
        settings=settings,
        max_output=2000,
    )
    if code == 0 and out.strip() == url:
        return
    if code == 0:
        args = ("remote", "set-url", "origin", url)
    else:
        args = ("remote", "add", "origin", url)
    code, _, err = await run_git(path, *args, settings=settings, max_output=4000)
    if code != 0:
        raise SourceError("GIT_REMOTE_FAILED", f"{repo}: {err.strip()}")


async def _can_auto_fetch(path: Path, repo: str, settings: Settings) -> bool:
    if settings.github_token or repo in settings.repo_urls:
        return True
    code, out, _ = await run_git(
        path, "remote", "get-url", "origin",
        settings=settings,
        max_output=2000,
    )
    return code == 0 and bool(out.strip())


def _repo_url(repo: str, settings: Settings) -> str:
    url = settings.repo_urls.get(repo) or f"https://github.com/langgenius/{repo}.git"
    return _with_github_token(url, settings)


def _with_github_token(url: str, settings: Settings) -> str:
    m = _SSH_URL_RE.match(url)
    if m:
        return f"https://github.com/{m.group(1)}"
    return url


def _git_env(
    settings: Settings,
    *,
    use_github_token: bool = True,
    allow_lazy_fetch: bool = True,
) -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.pop("SOURCE_MCP_GITHUB_TOKEN", None)
    env.pop("GIT_ASKPASS", None)
    if not allow_lazy_fetch:
        env["GIT_NO_LAZY_FETCH"] = "1"
    if settings.github_token and use_github_token:
        env["SOURCE_MCP_GITHUB_TOKEN"] = settings.github_token
        askpass = Path(__file__).resolve().parents[2] / "scripts" / "github_askpass.sh"
        env["GIT_ASKPASS"] = str(askpass)
    elif not use_github_token:
        env.setdefault("HOME", str(Path.home()))
    return env


def _fetch_head_mtime(path: Path) -> Optional[float]:
    for candidate in (path / "FETCH_HEAD", path / ".git" / "FETCH_HEAD"):
        if candidate.exists():
            return candidate.stat().st_mtime
    return None


def _is_complex_regex(pattern: str) -> bool:
    if _COMPLEX_RE_PATTERN.search(pattern):
        return True
    special = sum(1 for c in pattern if c in r"()[]{}|+*?^$\\")
    return special > 8


def _is_grep_pattern_error(stderr: str) -> bool:
    lower = stderr.lower()
    return any(marker in lower for marker in _GREP_PATTERN_ERROR_MARKERS)


def _is_missing_blob_error(stderr: str) -> bool:
    lower = stderr.lower()
    return any(marker in lower for marker in _MISSING_BLOB_ERROR_MARKERS)


def _is_git_auth_failure(stderr: str) -> bool:
    lower = stderr.lower()
    return any(
        marker in lower
        for marker in (
            "authentication failed",
            "write access to repository not granted",
            "requested url returned error: 401",
            "requested url returned error: 403",
        )
    )


def _grep_args(*, pattern: str, ref: str, glob: Optional[str], fixed: bool) -> list[str]:
    grep_flag = "-F" if fixed else "-e"
    args = ["grep", "-n", "--break", "--heading", "-i", grep_flag, pattern, ref]
    if glob:
        args.extend(["--", glob])
    return args


def _reject_bad_filepath(filepath: str) -> None:
    if filepath.startswith("/") or ".." in Path(filepath).parts:
        raise SourceError("BAD_FILEPATH", f"bad filepath: {filepath}")
