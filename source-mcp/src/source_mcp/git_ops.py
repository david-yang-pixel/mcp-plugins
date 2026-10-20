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
_TAG_LIKE_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-_].*)?$")
_COMMIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
_FULL_COMMIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_PERL_ONLY_RE_PATTERN = re.compile(r"(\(\?|\\[dD]|[*+?}]\?)")
_INLINE_CASE_FLAG = "(?i)"
SEARCH_MODES = ("auto", "fixed", "regex", "perl")
DIFF_MODES = ("stat", "name-status", "patch")
_MAX_SEARCH_CONTEXT = 10
_MAX_READ_BYTES = 120_000
_MAX_REFRESH_REFS = 20
_MAX_CHANGED_REF_SAMPLE = 20
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
_PCRE_UNAVAILABLE_MARKERS = (
    "perl-compatible regexes",
    "use_libpcre",
)
_MISSING_REMOTE_COMMIT_MARKERS = (
    "not our ref",
    "couldn't find remote ref",
    "no such remote ref",
    "unadvertised object",
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


async def refresh_repo(
    repo: str,
    settings: Settings,
    *,
    refs: Optional[list[str]] = None,
    deepen: Optional[int] = None,
    unshallow: bool = False,
) -> dict[str, Any]:
    path = repo_path(settings, repo)
    if not is_git_repo(path):
        await _clone_repo(path, repo, settings)
        message = "cloned"
    elif await _can_auto_fetch(path, repo, settings):
        message = "fetched"
    elif refs or deepen or unshallow:
        raise SourceError(
            "GIT_FETCH_UNAVAILABLE",
            f"{repo} has no fetchable origin; refs/deepen/unshallow cannot be applied",
        )
    else:
        return await status_for_repo(repo, settings, message="local-only")

    await _configure_origin(path, repo, settings)
    before = await _repo_snapshot(path, settings)
    shallow_before = await is_shallow_repo(path, settings)
    history_args: list[str] = []
    if shallow_before and unshallow:
        history_args = ["--unshallow"]
    elif shallow_before and deepen:
        history_args = [f"--deepen={deepen}"]
    if message == "fetched" or history_args:
        code, _, err = await run_git(
            path, "fetch", "--all", "--prune", "--tags", "--force", *history_args,
            settings=settings,
            max_output=8000,
            timeout_seconds=settings.git_history_timeout_seconds if history_args else None,
        )
        if code != 0:
            raise SourceError("GIT_FETCH_FAILED", f"{repo}: {err.strip()}")

    ref_results = [await _ensure_ref_present(path, repo, ref, settings) for ref in refs or []]
    data = await status_for_repo(repo, settings, message=message)
    data["changes"] = _snapshot_changes(before, await _repo_snapshot(path, settings))
    data["changed"] = data["changes"]["total"] > 0
    if ref_results:
        data["refs"] = ref_results
    warnings: list[str] = []
    if data["shallow"]:
        warnings.append(
            "repository is still a shallow clone: only commits reachable from fetched "
            "branch/tag tips are available and source_log history is truncated; pass "
            "refs=[<full commit SHA>] to fetch a pinned commit, or deepen/unshallow for history"
        )
    if (deepen or unshallow) and not shallow_before:
        warnings.append("repository already has full history; deepen/unshallow had no effect")
    if message == "fetched" and not data["changed"]:
        warnings.append("fetch succeeded but no local ref or history changed")
    if any(item["status"] == "not_found" for item in ref_results):
        warnings.append("some requested refs were not found on origin")
    data["warnings"] = warnings
    return data


async def refresh_repos(
    settings: Settings,
    repos: Optional[list[str]] = None,
    *,
    refs: Optional[list[str]] = None,
    deepen: Optional[int] = None,
    unshallow: bool = False,
) -> list[dict[str, Any]]:
    names = repos or settings.repo_names
    if refs:
        if len(refs) > _MAX_REFRESH_REFS:
            raise SourceError("BAD_ARGUMENT", f"refs accepts at most {_MAX_REFRESH_REFS} items")
        for ref in refs:
            _reject_bad_ref(ref)
    if (refs or deepen or unshallow) and (not repos or len(repos) != 1):
        raise SourceError(
            "BAD_ARGUMENT",
            "refs, deepen and unshallow require exactly one repo in repos",
        )
    if deepen is not None and not 1 <= deepen <= 100_000:
        raise SourceError("BAD_ARGUMENT", "deepen must be between 1 and 100000")
    return [
        await refresh_repo(repo, settings, refs=refs, deepen=deepen, unshallow=unshallow)
        for repo in names
    ]


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
    data["shallow"] = await is_shallow_repo(path, settings)
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
            if _FULL_COMMIT_SHA_RE.match(ref):
                await _fetch_commit(path, ref, settings)
            else:
                await _fetch_ref(path, ref, settings)
        code, out, err = await run_git(
            path, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}",
            settings=settings,
            max_output=2000,
        )
    if code != 0:
        raise SourceError("REF_NOT_FOUND", await _ref_not_found_message(path, repo, ref, err, settings))
    return out.strip()


async def is_shallow_repo(path: Path, settings: Settings) -> bool:
    code, out, _ = await run_git(
        path, "rev-parse", "--is-shallow-repository",
        settings=settings,
        max_output=2000,
    )
    return code == 0 and out.strip() == "true"


async def _ref_not_found_message(
    path: Path,
    repo: str,
    ref: str,
    err: str,
    settings: Settings,
) -> str:
    message = f"ref not found in {repo}: {ref}"
    if err.strip():
        message += f"; {err.strip()}"
    if not await is_shallow_repo(path, settings):
        return message
    if _FULL_COMMIT_SHA_RE.match(ref):
        hint = "the commit was also not fetchable from origin"
    elif _COMMIT_SHA_RE.match(ref):
        hint = (
            "abbreviated commit SHAs cannot be fetched from origin; pass the full "
            "40-character SHA, or call refresh_repos with unshallow=true"
        )
    else:
        hint = (
            "pass a full commit SHA, or call refresh_repos with refs=[...], deepen or "
            "unshallow=true"
        )
    return (
        f"{message}; {repo} is a shallow clone, so commits that are not the tip of a "
        f"fetched branch or tag are not available locally; {hint}"
    )


async def search_source(
    *,
    repo: str,
    ref: str,
    pattern: str,
    glob: Optional[str],
    limit: int,
    settings: Settings,
    mode: str = "auto",
    case_sensitive: bool = False,
    context: int = 0,
) -> dict[str, Any]:
    if mode not in SEARCH_MODES:
        raise SourceError("BAD_ARGUMENT", f"mode must be one of: {', '.join(SEARCH_MODES)}")
    if not 0 <= context <= _MAX_SEARCH_CONTEXT:
        raise SourceError("BAD_ARGUMENT", f"context must be between 0 and {_MAX_SEARCH_CONTEXT}")
    path = await ensure_repo(repo, settings)
    resolved = await _resolve_ref(repo, ref, settings)
    effective_pattern = pattern
    ignore_case = not case_sensitive
    if mode != "fixed" and pattern.startswith(_INLINE_CASE_FLAG):
        effective_pattern = pattern[len(_INLINE_CASE_FLAG):]
        ignore_case = True
    effective_mode, fallback = _select_search_mode(mode, effective_pattern)

    async def grep(grep_mode: str) -> tuple[int, str, str]:
        args = _grep_args(
            pattern=effective_pattern,
            ref=resolved["ref"],
            glob=glob,
            mode=grep_mode,
            ignore_case=ignore_case,
            context=context,
        )
        return await run_git(
            path,
            *args,
            settings=settings,
            max_output=limit * 1000,
            allow_lazy_fetch=False,
        )

    async def execute_search() -> tuple[int, str, str]:
        nonlocal effective_mode, fallback
        result = await grep(effective_mode)
        code, _, err = result
        if code not in (0, 1) and mode == "auto" and effective_mode != "fixed":
            if _is_grep_pattern_error(err) or _is_pcre_unavailable(err):
                fallback = (
                    f"{effective_mode} pattern rejected by git grep "
                    f"({_first_line(err)}); searched as fixed string"
                )
                effective_mode = "fixed"
                result = await grep(effective_mode)
        return result

    if glob is None:
        async with _full_repo_search_semaphore(settings):
            code, out, err = await execute_search()
    else:
        code, out, err = await execute_search()
    if code == 1:
        out = (
            f"no matches for {effective_pattern!r} (mode={effective_mode}, "
            f"{'case-insensitive' if ignore_case else 'case-sensitive'})"
        )
    elif code != 0:
        if _is_missing_blob_error(err):
            raise SourceError(
                "REPO_BLOBS_MISSING",
                f"{repo} is missing blobs required to search {resolved['ref']}",
            )
        if _is_grep_pattern_error(err) or _is_pcre_unavailable(err):
            raise SourceError("BAD_PATTERN", f"{effective_mode} pattern rejected: {err.strip()}")
        raise SourceError("GIT_GREP_FAILED", err.strip())
    data = {
        **resolved,
        "pattern": effective_pattern,
        "mode": effective_mode,
        "case_sensitive": not ignore_case,
        "context": context,
        "matched": code == 0,
        "output": out,
    }
    if fallback:
        data["fallback"] = fallback
    return data


async def read_source(
    *,
    repo: str,
    ref: str,
    filepath: str,
    settings: Settings,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
) -> dict[str, Any]:
    _reject_bad_filepath(filepath)
    if start_line is not None and start_line < 1:
        raise SourceError("BAD_ARGUMENT", "start_line must be >= 1")
    if end_line is not None and end_line < 1:
        raise SourceError("BAD_ARGUMENT", "end_line must be >= 1")
    if start_line is not None and end_line is not None and end_line < start_line:
        raise SourceError("BAD_ARGUMENT", "end_line must be >= start_line")
    path = await ensure_repo(repo, settings)
    resolved = await _resolve_ref(repo, ref, settings)
    code, out, err = await run_git(
        path, "show", f"{resolved['ref']}:{filepath}",
        settings=settings,
        max_output=settings.read_max_file_bytes,
    )
    if code != 0:
        raise SourceError("GIT_SHOW_FAILED", err.strip())
    file_truncated = len(out) > settings.read_max_file_bytes
    if file_truncated:
        out = out[:settings.read_max_file_bytes]
    lines = out.splitlines(keepends=True)
    first = start_line or 1
    last = min(end_line or len(lines), len(lines))
    selected: list[str] = []
    size = 0
    line_cut = False
    for line in lines[first - 1:last]:
        if selected and size + len(line) > _MAX_READ_BYTES:
            break
        line_cut = len(line) > _MAX_READ_BYTES
        selected.append(line[:_MAX_READ_BYTES])
        size += len(selected[-1])
    returned_end = first + len(selected) - 1
    truncated = returned_end < last or line_cut
    data: dict[str, Any] = {
        **resolved,
        "filepath": filepath,
        "total_lines": len(lines),
        "start_line": first,
        "end_line": returned_end,
        "truncated": truncated,
        "content": "".join(selected),
    }
    if truncated:
        data["hint"] = (
            f"output limited to {_MAX_READ_BYTES} characters; continue with "
            f"start_line={returned_end + 1}"
        )
    if file_truncated:
        data["total_lines_exact"] = False
    return data


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
    data = {**resolved, "output": out, "shallow": await is_shallow_repo(path, settings)}
    if data["shallow"] and len(out.splitlines()) < n:
        data["warning"] = (
            "history is truncated because the repository is a shallow clone; "
            "call refresh_repos with deepen or unshallow=true for older commits"
        )
    return data


async def source_diff(
    *,
    repo: str,
    ref1: str,
    ref2: str,
    filepath: Optional[str],
    settings: Settings,
    mode: Optional[str] = None,
) -> dict[str, Any]:
    mode = mode or ("patch" if filepath else "stat")
    if mode not in DIFF_MODES:
        raise SourceError("BAD_ARGUMENT", f"mode must be one of: {', '.join(DIFF_MODES)}")
    if filepath:
        _reject_bad_filepath(filepath)
    path = await ensure_repo(repo, settings)
    resolved1 = await _resolve_ref(repo, ref1, settings)
    resolved2 = await _resolve_ref(repo, ref2, settings)
    args = ["diff", resolved1["ref"], resolved2["ref"]]
    if mode == "stat":
        args.append("--stat=200")
    elif mode == "name-status":
        args.append("--name-status")
    if filepath:
        args.extend(["--", filepath])
    code, out, err = await run_git(path, *args, settings=settings, max_output=80_000)
    if code != 0:
        raise SourceError("GIT_DIFF_FAILED", err.strip())
    return {
        "repo": repo,
        "ref1": resolved1["ref"],
        "commit1": resolved1["commit"],
        "ref2": resolved2["ref"],
        "commit2": resolved2["commit"],
        "mode": mode,
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
        *await _shallow_fetch_args(path, settings),
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


async def _fetch_commit(path: Path, sha: str, settings: Settings) -> bool:
    sha = sha.lower()
    args = (
        "fetch",
        *await _shallow_fetch_args(path, settings),
        "--no-tags",
        "origin",
        f"+{sha}:refs/source-mcp/commits/{sha}",
    )
    code, _, err = await run_git(path, *args, settings=settings, max_output=8000)
    if code != 0 and settings.github_token and _is_git_auth_failure(err):
        code, _, err = await run_git(
            path, *args,
            settings=settings,
            max_output=8000,
            use_github_token=False,
        )
    if code == 0:
        return True
    if _is_missing_remote_commit(err):
        return False
    raise SourceError("GIT_FETCH_FAILED", err.strip() or f"git fetch exited {code}")


async def _shallow_fetch_args(path: Path, settings: Settings) -> tuple[str, ...]:
    # Keep shallow clones shallow, but never re-truncate a repo that already has history.
    return ("--depth=1",) if await is_shallow_repo(path, settings) else ()


async def _ensure_ref_present(
    path: Path,
    repo: str,
    ref: str,
    settings: Settings,
) -> dict[str, Any]:
    code, out, _ = await run_git(
        path, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}",
        settings=settings,
        max_output=2000,
    )
    is_sha = bool(_COMMIT_SHA_RE.match(ref))
    if code == 0 and is_sha:
        return {"ref": ref, "status": "present", "commit": out.strip()}
    if code == 0:
        # Branch or tag names may have moved; refresh them from origin.
        status = "refreshed" if await _fetch_ref(path, ref, settings) else "local-only"
    elif _FULL_COMMIT_SHA_RE.match(ref):
        status = "fetched" if await _fetch_commit(path, ref, settings) else "not_found"
    elif is_sha and await is_shallow_repo(path, settings):
        return {
            "ref": ref,
            "status": "not_found",
            "error": (
                "abbreviated commit SHAs cannot be fetched from origin; pass the full "
                "40-character SHA or use unshallow=true"
            ),
        }
    else:
        status = "fetched" if await _fetch_ref(path, ref, settings) else "not_found"
    code, out, _ = await run_git(
        path, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}",
        settings=settings,
        max_output=2000,
    )
    if code != 0:
        return {"ref": ref, "status": "not_found"}
    return {"ref": ref, "status": status, "commit": out.strip()}


async def _repo_snapshot(path: Path, settings: Settings) -> dict[str, Any]:
    code, out, err = await run_git(
        path, "for-each-ref", "--format=%(objectname) %(refname)",
        settings=settings,
        max_output=20_000_000,
    )
    if code != 0:
        raise SourceError("GIT_REFS_FAILED", err.strip())
    refs: dict[str, str] = {}
    for line in out.splitlines():
        parts = line.split(" ", 1)
        if len(parts) == 2:
            refs[parts[1]] = parts[0]
    return {"refs": refs, "shallow_boundary": await _shallow_boundary(path, settings)}


async def _shallow_boundary(path: Path, settings: Settings) -> frozenset[str]:
    code, out, _ = await run_git(
        path, "rev-parse", "--git-path", "shallow",
        settings=settings,
        max_output=4000,
    )
    if code != 0 or not out.strip():
        return frozenset()
    shallow_file = Path(out.strip())
    if not shallow_file.is_absolute():
        shallow_file = path / shallow_file
    try:
        return frozenset(line.strip() for line in shallow_file.read_text().splitlines() if line.strip())
    except OSError:
        return frozenset()


def _snapshot_changes(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    old_refs: dict[str, str] = before["refs"]
    new_refs: dict[str, str] = after["refs"]
    added = sorted(set(new_refs) - set(old_refs))
    removed = sorted(set(old_refs) - set(new_refs))
    updated = sorted(ref for ref in set(old_refs) & set(new_refs) if old_refs[ref] != new_refs[ref])
    history_changed = before["shallow_boundary"] != after["shallow_boundary"]
    return {
        "total": len(added) + len(removed) + len(updated) + int(history_changed),
        "added_refs": len(added),
        "updated_refs": len(updated),
        "removed_refs": len(removed),
        "history_changed": history_changed,
        "sample": [
            *(f"+ {ref}" for ref in added),
            *(f"~ {ref}" for ref in updated),
            *(f"- {ref}" for ref in removed),
        ][:_MAX_CHANGED_REF_SAMPLE],
    }


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


def _select_search_mode(mode: str, pattern: str) -> tuple[str, Optional[str]]:
    if mode != "auto":
        return mode, None
    if len(pattern) > 120:
        return "fixed", "pattern longer than 120 characters; searched as fixed string"
    # Extended regex is the default so alternation like "foo|bar" works; Perl-only
    # syntax (lookaround, \d, lazy quantifiers, inline groups) switches to PCRE.
    if _PERL_ONLY_RE_PATTERN.search(pattern):
        return "perl", None
    return "regex", None


def _is_pcre_unavailable(stderr: str) -> bool:
    lower = stderr.lower()
    return any(marker in lower for marker in _PCRE_UNAVAILABLE_MARKERS)


def _is_missing_remote_commit(stderr: str) -> bool:
    lower = stderr.lower()
    return any(marker in lower for marker in _MISSING_REMOTE_COMMIT_MARKERS)


def _first_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


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


def _grep_args(
    *,
    pattern: str,
    ref: str,
    glob: Optional[str],
    mode: str,
    ignore_case: bool = True,
    context: int = 0,
) -> list[str]:
    mode_flag = {"fixed": "-F", "regex": "-E", "perl": "-P"}[mode]
    args = ["grep", "-n", "--break", "--heading", mode_flag]
    if ignore_case:
        args.append("-i")
    if context:
        args.append(f"-C{context}")
    args.extend(["-e", pattern, ref])
    if glob:
        args.extend(["--", glob])
    return args


def _reject_bad_ref(ref: str) -> None:
    if not isinstance(ref, str) or not ref.strip() or ref.startswith("-") or ".." in ref:
        raise SourceError("BAD_ARGUMENT", f"bad ref: {ref!r}")


def _reject_bad_filepath(filepath: str) -> None:
    if filepath.startswith("/") or ".." in Path(filepath).parts:
        raise SourceError("BAD_FILEPATH", f"bad filepath: {filepath}")
