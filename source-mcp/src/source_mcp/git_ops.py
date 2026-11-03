from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import signal
import time
import weakref
from contextlib import suppress
from datetime import datetime, timezone
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
_DEFAULT_SEARCH_LIMIT = 30
_MAX_SEARCH_LIMIT = 200
_SEARCH_OUTPUT_CAP = 4_000_000
_MAX_MATCH_TEXT = 500
_DEFAULT_TREE_LIMIT = 200
_MAX_TREE_LIMIT = 1000
_TREE_OUTPUT_CAP = 16_000_000
_MAX_TAG_LIMIT = 200
_TRUNCATION_MARKER = "\n\n... (output truncated)"
# Optional component prefix ("dify-3.13.2", "backend/2.3.2", "redhat-chart-3.9.8-run-1"),
# optional v, then a dotted version and an optional suffix.
_VERSION_TAG_RE = re.compile(
    r"^(?P<prefix>[A-Za-z][A-Za-z0-9._-]*?[-/_])?[vV]?(\d+(?:\.\d+){1,3})(?:[-+._]?(.*))?$"
)
_PRERELEASE_RE = re.compile(r"^(alpha|beta|rc|pre|preview|dev|a|b)(?![a-z])", re.IGNORECASE)
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
_REPO_LOCKS: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]]" = (
    weakref.WeakKeyDictionary()
)
_BACKGROUND_STATE: dict[str, dict[str, Any]] = {}
_BACKGROUND_RUN: dict[str, Any] = {}
_LOG = logging.getLogger("source_mcp.background")


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


def _repo_lock(repo: str) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    locks = _REPO_LOCKS.setdefault(loop, {})
    lock = locks.get(repo)
    if lock is None:
        lock = locks[repo] = asyncio.Lock()
    return lock


def unknown_repo_message(settings: Settings, repo: str) -> str:
    configured = settings.repo_names
    message = (
        f"unknown repo: {repo}. Configured repos: {', '.join(configured) or '(none)'}."
    )
    hints = _repo_hint_lines(settings, repo)
    if hints:
        message += " Hint: " + " ".join(hints)
    message += (
        " To add a repository, append its GitHub name (github.com/langgenius/<name>) to "
        "SOURCE_MCP_REPOS (comma-separated) in source-mcp/deploy/runtime.json; repos "
        "outside langgenius also need a clone URL in SOURCE_MCP_REPO_URLS."
    )
    return message


def _repo_hint_lines(settings: Settings, repo: str) -> list[str]:
    hints = {str(key).lower(): value for key, value in settings.repo_hints.items()}
    key = repo.strip().lower()
    hint = hints.get(key)
    if hint is None and key.startswith("dify-"):
        hint = hints.get(key.removeprefix("dify-"))
    if hint is None:
        return []
    if isinstance(hint, str):
        return [hint]
    entries = hint if isinstance(hint, list) else [hint]
    lines: list[str] = []
    for entry in entries:
        if isinstance(entry, str):
            lines.append(entry)
            continue
        if not isinstance(entry, dict) or not isinstance(entry.get("repo"), str):
            continue
        target = entry["repo"]
        paths = [str(item) for item in entry.get("paths") or [] if str(item).strip()]
        where = f"repo {target}"
        if paths:
            where = f"{', '.join(paths)} in repo {target}"
        status = "configured" if target in settings.repo_names else "not configured here"
        note = str(entry.get("note") or "").strip()
        if note:
            status += f"; {note}"
        lines.append(f"{repo!r} is located at {where} ({status}).")
    return lines


def repo_path(settings: Settings, repo: str) -> Path:
    if repo not in settings.repo_names:
        raise SourceError("UNKNOWN_REPO", unknown_repo_message(settings, repo))
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
        out = out[:max_output] + _TRUNCATION_MARKER
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
    async with _repo_lock(repo):
        if not is_git_repo(path):
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
    async with _repo_lock(repo):
        return await _refresh_repo_locked(
            path, repo, settings, refs=refs, deepen=deepen, unshallow=unshallow
        )


async def _refresh_repo_locked(
    path: Path,
    repo: str,
    settings: Settings,
    *,
    refs: Optional[list[str]],
    deepen: Optional[int],
    unshallow: bool,
) -> dict[str, Any]:
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
        _mark_full_fetch(path)

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
    if len(names) == 1:
        return [
            await refresh_repo(names[0], settings, refs=refs, deepen=deepen, unshallow=unshallow)
        ]
    # With several repos one failure (e.g. a clone without access) must not hide the others.
    results: list[dict[str, Any]] = []
    for repo in names:
        try:
            results.append({**await refresh_repo(repo, settings), "ok": True})
        except Exception as e:  # noqa: BLE001 - reported per repo.
            results.append(_repo_error_entry(repo, e))
    return results


async def status_for_repos(settings: Settings) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for repo in settings.repo_names:
        try:
            results.append(await status_for_repo(repo, settings))
        except Exception as e:  # noqa: BLE001 - reported per repo.
            results.append(_repo_error_entry(repo, e))
    return results


def _repo_error_entry(repo: str, error: Exception) -> dict[str, Any]:
    if isinstance(error, SourceError):
        return {"repo": repo, "ok": False, "code": error.code, "error": error.message}
    return {"repo": repo, "ok": False, "code": "INTERNAL_ERROR", "error": str(error)}


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
        "full_history": settings.is_full_history_repo(repo),
        "background_refresh": background_refresh_status(repo, settings),
    }
    if not cloned:
        return data
    default_ref = await default_branch_ref(path, settings)
    code, out, _ = await run_git(
        path, "log", "-1", "--format=%H %cI", f"refs/remotes/{default_ref}",
        settings=settings,
        max_output=2000,
    )
    parts = out.split() if code == 0 else []
    # The default branch as last fetched, not the checked-out HEAD (which is
    # whatever the clone started on and is never moved by fetches).
    data["commit"] = parts[0] if parts else None
    data["default_branch_committed_at"] = parts[1] if len(parts) > 1 else None
    code, head_ref, _ = await run_git(
        path, "rev-parse", "--abbrev-ref", "HEAD", settings=settings, max_output=2000
    )
    data["head_ref"] = head_ref.strip() if code == 0 else None
    data["head_commit"] = await rev_parse(repo, "HEAD", settings)
    refspecs = await fetch_refspecs(path, settings)
    data["fetch_refspecs"] = refspecs
    wanted = await desired_fetch_refspec(path, settings)
    data["fetch_refspec_ok"] = None if wanted is None else refspecs == [wanted]
    full_fetch = _full_fetch_mtime(path)
    last_fetch = full_fetch if full_fetch is not None else _fetch_head_mtime(path)
    data["last_fetch_at"] = last_fetch
    data["last_fetch_at_iso"] = _iso(last_fetch)
    # "full" = last `fetch --all`; "fetch_head" = any fetch, possibly a single ref.
    data["last_fetch_kind"] = "full" if full_fetch is not None else ("fetch_head" if last_fetch else None)
    interval = settings.background_refresh_seconds
    if interval > 0:
        data["stale_after_seconds"] = interval * 2
        data["stale"] = last_fetch is None or (time.time() - last_fetch) > interval * 2
    else:
        data["stale_after_seconds"] = None
        data["stale"] = None
    data["default_branch_ref"] = default_ref
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
    target = await _preferred_ref(path, ref, settings)
    await _refresh_branch_on_use(path, repo, ref, target, settings)
    code, out, err = await run_git(
        path, "rev-parse", "--verify", "-q", f"{target}^{{commit}}",
        settings=settings,
        max_output=2000,
    )
    if code != 0:
        if await _can_auto_fetch(path, repo, settings):
            try:
                await _configure_origin(path, repo, settings)
                if _FULL_COMMIT_SHA_RE.match(ref):
                    await _fetch_commit(path, ref, settings)
                else:
                    await _fetch_ref(path, ref, settings)
            except SourceError:
                if target == ref:
                    raise
                # A local copy of the branch exists; fall back to it below.
                _LOG.warning("fetch of %s %s failed; using the local ref", repo, ref)
        target = await _preferred_ref(path, ref, settings)
        code, out, err = await run_git(
            path, "rev-parse", "--verify", "-q", f"{target}^{{commit}}",
            settings=settings,
            max_output=2000,
        )
    if code != 0 and target != ref:
        # Remote-tracking ref unavailable (no origin access, branch deleted
        # upstream): use whatever local ref git resolves, as before.
        code, out, err = await run_git(
            path, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}",
            settings=settings,
            max_output=2000,
        )
    if code != 0:
        raise SourceError("REF_NOT_FOUND", await _ref_not_found_message(path, repo, ref, err, settings))
    return out.strip()


_REF_EXPRESSION_CHARS = ("^", "~", ":", "@{", "..")
# (repo path, branch) -> time of the last on-demand fetch of that branch.
_BRANCH_FETCHED_AT: dict[tuple[str, str], float] = {}


async def _refresh_branch_on_use(
    path: Path, repo: str, ref: str, target: str, settings: Settings
) -> None:
    """Re-fetch a branch outside the fetch refspec when it is used after the TTL.

    Only the default branch is fetched in the background; branches fetched on
    demand (e.g. hotfix/1.18.0-fix.4) are refreshed here, one ref at a time.
    """
    if settings.fetch_all_branches or not target.startswith("refs/remotes/origin/"):
        return
    branch = target.removeprefix("refs/remotes/origin/")
    if branch == await _remote_default_branch_cached(path, settings):
        return
    key = (str(path), branch)
    if time.time() - _BRANCH_FETCHED_AT.get(key, 0.0) < settings.fetch_ttl_seconds:
        return
    _BRANCH_FETCHED_AT[key] = time.time()
    if not await _can_auto_fetch(path, repo, settings):
        return
    try:
        await _configure_origin(path, repo, settings)
        await _fetch_ref(path, branch, settings)
    except SourceError as e:
        _LOG.warning("on-use refresh of %s %s failed: %s", repo, branch, e.message)


async def _remote_default_branch_cached(path: Path, settings: Settings) -> Optional[str]:
    code, out, _ = await run_git(
        path, "config", "--get", _DEFAULT_BRANCH_CONFIG, settings=settings, max_output=500
    )
    return out.strip() if code == 0 and out.strip() else None


async def _preferred_ref(path: Path, ref: str, settings: Settings) -> str:
    """Resolve a bare branch name to its remote-tracking ref.

    Background fetches only update refs/remotes/origin/*, so a local refs/heads/main
    (left by the initial clone or an old on-demand fetch) silently goes stale. Tags
    keep git's normal precedence; SHAs, HEAD and explicit ref paths are unchanged.
    """
    if (
        not ref
        or ref == "HEAD"
        or ref.startswith(("origin/", "refs/", "-"))
        or _COMMIT_SHA_RE.match(ref)
        or any(token in ref for token in _REF_EXPRESSION_CHARS)
    ):
        return ref
    remote = f"refs/remotes/origin/{ref}"
    if await _ref_exists(path, f"refs/tags/{ref}", settings):
        return ref
    if await _ref_exists(path, remote, settings):
        return remote
    if await _ref_exists(path, f"refs/heads/{ref}", settings):
        # Only a local copy exists (left by the old on-demand fetch path): aim at
        # the remote-tracking ref so it is fetched; rev_parse falls back to the
        # local branch if that fetch is impossible.
        return remote
    return ref


async def _ref_exists(path: Path, full_ref: str, settings: Settings) -> bool:
    code, _, _ = await run_git(
        path, "show-ref", "--verify", "--quiet", full_ref,
        settings=settings,
        max_output=200,
    )
    return code == 0


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
    offset: int = 0,
    deployment: Optional[str] = None,
    environment: Optional[str] = None,
) -> dict[str, Any]:
    if mode not in SEARCH_MODES:
        raise SourceError("BAD_ARGUMENT", f"mode must be one of: {', '.join(SEARCH_MODES)}")
    if not 0 <= context <= _MAX_SEARCH_CONTEXT:
        raise SourceError("BAD_ARGUMENT", f"context must be between 0 and {_MAX_SEARCH_CONTEXT}")
    if offset < 0:
        raise SourceError("BAD_ARGUMENT", "offset must be >= 0")
    limit = max(1, min(limit, _MAX_SEARCH_LIMIT))
    path = await ensure_repo(repo, settings)
    resolved = await _resolve_ref(
        repo, ref, settings, deployment=deployment, environment=environment
    )
    tree = resolved["commit"]
    effective_pattern = pattern
    ignore_case = not case_sensitive
    if mode != "fixed" and pattern.startswith(_INLINE_CASE_FLAG):
        effective_pattern = pattern[len(_INLINE_CASE_FLAG):]
        ignore_case = True
    effective_mode, fallback = _select_search_mode(mode, effective_pattern)

    async def grep(
        grep_mode: str,
        *,
        grep_context: int = 0,
        paths: Optional[list[str]] = None,
    ) -> tuple[int, str, str]:
        args = _grep_args(
            pattern=effective_pattern,
            ref=tree,
            glob=None if paths else glob,
            mode=grep_mode,
            ignore_case=ignore_case,
            context=grep_context,
            paths=paths,
        )
        return await run_git(
            path,
            *args,
            settings=settings,
            max_output=_SEARCH_OUTPUT_CAP,
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
    if code not in (0, 1):
        if _is_missing_blob_error(err):
            raise SourceError(
                "REPO_BLOBS_MISSING",
                f"{repo} is missing blobs required to search {resolved['ref']}",
            )
        if _is_grep_pattern_error(err) or _is_pcre_unavailable(err):
            raise SourceError("BAD_PATTERN", f"{effective_mode} pattern rejected: {err.strip()}")
        raise SourceError("GIT_GREP_FAILED", err.strip())

    records, output_truncated = ([], False) if code == 1 else _parse_grep_z(out, tree)
    total = len(records)
    page = records[offset:offset + limit]
    context_lines: dict[str, dict[int, str]] = {}
    if context and page:
        page_files = list(dict.fromkeys(record[0] for record in page))
        ctx_code, ctx_out, _ = await grep(effective_mode, grep_context=context, paths=page_files)
        if ctx_code == 0:
            for file, line, text in _parse_grep_z(ctx_out, tree)[0]:
                context_lines.setdefault(file, {})[line] = text
    match_lines: dict[str, set[int]] = {}
    for file, line, _ in records:
        match_lines.setdefault(file, set()).add(line)

    matches = [
        _match_entry(file, line, text, context, context_lines.get(file, {}))
        for file, line, text in page
    ]
    returned_end = offset + len(page)
    has_more = returned_end < total or output_truncated
    if code == 1:
        output = (
            f"no matches for {effective_pattern!r} (mode={effective_mode}, "
            f"{'case-insensitive' if ignore_case else 'case-sensitive'})"
        )
    elif not page:
        output = f"no matches at offset {offset}; total_matches={total}"
    else:
        output = _render_matches(matches, match_lines)
        if has_more:
            output += f"\n\n... more matches; continue with offset={returned_end}"
    data: dict[str, Any] = {
        **resolved,
        "pattern": effective_pattern,
        "mode": effective_mode,
        "case_sensitive": not ignore_case,
        "context": context,
        "matched": total > 0,
        "total_matches": total,
        "total_exact": not output_truncated,
        "files_with_matches": len(match_lines),
        "offset": offset,
        "limit": limit,
        "returned": len(page),
        "has_more": has_more,
        "next_offset": returned_end if has_more else None,
        "matches": matches,
        "output": output,
    }
    if output_truncated:
        data["total_note"] = (
            f"git grep output exceeded {_SEARCH_OUTPUT_CAP} characters; total_matches is a "
            "lower bound. Narrow the pattern or glob for an exact count."
        )
    if fallback:
        data["fallback"] = fallback
    return data


def _parse_grep_z(out: str, tree: str) -> tuple[list[tuple[str, int, str]], bool]:
    """Parse `git grep -n -z <tree>` output into (file, line, text) records."""
    truncated = out.endswith(_TRUNCATION_MARKER)
    if truncated:
        out = out[: -len(_TRUNCATION_MARKER)]
    prefix = f"{tree}:"
    records: list[tuple[str, int, str]] = []
    pos = 0
    size = len(out)
    while pos < size:
        if out.startswith("--\n", pos):
            pos += 3
            continue
        path_end = out.find("\0", pos)
        line_end = out.find("\0", path_end + 1) if path_end >= 0 else -1
        if path_end < 0 or line_end < 0:
            break
        text_end = out.find("\n", line_end + 1)
        if text_end < 0:
            if truncated:
                break  # the final record was cut by the output cap
            text_end = size
        name = out[pos:path_end]
        number = out[path_end + 1:line_end]
        text = out[line_end + 1:text_end]
        pos = text_end + 1
        if name.startswith(prefix):
            name = name[len(prefix):]
        if not number.isdigit():
            continue
        records.append((name, int(number), text))
    return records, truncated


def _clip_text(text: str) -> tuple[str, bool]:
    if len(text) <= _MAX_MATCH_TEXT:
        return text, False
    return text[:_MAX_MATCH_TEXT], True


def _match_entry(
    file: str,
    line: int,
    text: str,
    context: int,
    lines: dict[int, str],
) -> dict[str, Any]:
    clipped, cut = _clip_text(text)
    entry: dict[str, Any] = {"file": file, "line": line, "text": clipped}
    if cut:
        entry["text_truncated"] = True
    if context:
        entry["context_before"] = [
            {"line": number, "text": _clip_text(lines[number])[0]}
            for number in range(max(1, line - context), line)
            if number in lines
        ]
        entry["context_after"] = [
            {"line": number, "text": _clip_text(lines[number])[0]}
            for number in range(line + 1, line + context + 1)
            if number in lines
        ]
    return entry


def _render_matches(matches: list[dict[str, Any]], match_lines: dict[str, set[int]]) -> str:
    out: list[str] = []
    current_file: Optional[str] = None
    last_line: Optional[int] = None
    for match in matches:
        file = match["file"]
        if file != current_file:
            if out:
                out.append("")
            out.append(file)
            current_file = file
            last_line = None
        block = [
            *match.get("context_before", []),
            {"line": match["line"], "text": match["text"]},
            *match.get("context_after", []),
        ]
        for item in block:
            number = item["line"]
            if last_line is not None and number <= last_line:
                continue
            if last_line is not None and number > last_line + 1:
                out.append("--")
            separator = ":" if number in match_lines.get(file, set()) else "-"
            out.append(f"{number}{separator}{item['text']}")
            last_line = number
    return "\n".join(out)


async def read_source(
    *,
    repo: str,
    ref: str,
    filepath: str,
    settings: Settings,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
    deployment: Optional[str] = None,
    environment: Optional[str] = None,
) -> dict[str, Any]:
    _reject_bad_filepath(filepath)
    if start_line is not None and start_line < 1:
        raise SourceError("BAD_ARGUMENT", "start_line must be >= 1")
    if end_line is not None and end_line < 1:
        raise SourceError("BAD_ARGUMENT", "end_line must be >= 1")
    if start_line is not None and end_line is not None and end_line < start_line:
        raise SourceError("BAD_ARGUMENT", "end_line must be >= start_line")
    path = await ensure_repo(repo, settings)
    resolved = await _resolve_ref(
        repo, ref, settings, deployment=deployment, environment=environment
    )
    code, out, err = await run_git(
        path, "show", f"{resolved['commit']}:{filepath}",
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
    deployment: Optional[str] = None,
    environment: Optional[str] = None,
) -> dict[str, Any]:
    path = await ensure_repo(repo, settings)
    resolved = await _resolve_ref(
        repo, ref, settings, deployment=deployment, environment=environment
    )
    args = ["log", resolved["commit"], f"-{max(1, min(n, 100))}", "--oneline", "--decorate"]
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
        if settings.is_full_history_repo(repo):
            data["warning"] += (
                "; this repo is configured for full history, so the background refresh "
                "will unshallow it"
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
    deployment: Optional[str] = None,
    environment: Optional[str] = None,
) -> dict[str, Any]:
    mode = mode or ("patch" if filepath else "stat")
    if mode not in DIFF_MODES:
        raise SourceError("BAD_ARGUMENT", f"mode must be one of: {', '.join(DIFF_MODES)}")
    if filepath:
        _reject_bad_filepath(filepath)
    path = await ensure_repo(repo, settings)
    resolved1 = await _resolve_ref(
        repo, ref1, settings, deployment=deployment, environment=environment
    )
    resolved2 = await _resolve_ref(
        repo, ref2, settings, deployment=deployment, environment=environment
    )
    args = ["diff", resolved1["commit"], resolved2["commit"]]
    if mode == "stat":
        args.append("--stat=200")
    elif mode == "name-status":
        args.append("--name-status")
    if filepath:
        args.extend(["--", filepath])
    code, out, err = await run_git(path, *args, settings=settings, max_output=80_000)
    if code != 0:
        raise SourceError("GIT_DIFF_FAILED", err.strip())
    data: dict[str, Any] = {
        "repo": repo,
        "ref1": resolved1["ref"],
        "commit1": resolved1["commit"],
        "ref2": resolved2["ref"],
        "commit2": resolved2["commit"],
        "mode": mode,
        "output": out or "no diff",
    }
    runtime_sides = {
        side: resolved
        for side, resolved in (("ref1", resolved1), ("ref2", resolved2))
        if resolved.get("source") != "explicit"
    }
    if runtime_sides:
        data["runtime"] = runtime_sides
    return data


async def list_repo_tags(
    repo: str,
    limit: int,
    settings: Settings,
    pattern: Optional[str] = None,
) -> dict[str, Any]:
    if pattern is not None and (pattern.startswith("-") or len(pattern) > 200):
        raise SourceError("BAD_ARGUMENT", f"bad tag pattern: {pattern!r}")
    path = await ensure_repo(repo, settings)
    args = ["tag", "-l"]
    if pattern:
        args.append(pattern)
    code, out, err = await run_git(path, *args, settings=settings, max_output=2_000_000)
    if code != 0:
        raise SourceError("GIT_TAGS_FAILED", err.strip())
    tags = [
        line.strip()
        for line in out.removesuffix(_TRUNCATION_MARKER).splitlines()
        if line.strip()
    ]
    version_tags = sorted(
        (tag for tag in tags if _version_sort_key(tag) is not None),
        key=lambda tag: _version_sort_key(tag) or (),
        reverse=True,
    )
    other_tags = sorted(tag for tag in tags if _version_sort_key(tag) is None)
    ordered = version_tags + other_tags
    return {
        "repo": repo,
        "pattern": pattern,
        "tags": ordered[: max(1, min(limit, _MAX_TAG_LIMIT))],
        "total": len(tags),
        "version_tags": len(version_tags),
        "non_version_tags": len(other_tags),
        "order": (
            "version tags newest first (a component prefix such as 'dify-' or 'backend/' "
            "and a v prefix are allowed), then other tags by name"
        ),
    }


def _version_sort_key(tag: str) -> Optional[tuple[Any, ...]]:
    match = _VERSION_TAG_RE.match(tag)
    if not match:
        return None
    numbers = [int(part) for part in match.group(2).split(".")]
    numbers += [0] * (4 - len(numbers))
    suffix = match.group(3) or ""
    prefix = match.group("prefix") or ""
    if not suffix:
        rank = 1
    elif _PRERELEASE_RE.match(suffix):
        rank = 0  # 1.2.3-rc1 sorts before 1.2.3
    else:
        rank = 2  # 1.2.3-fix.4 / hotfix builds sort after 1.2.3
    # Same version: the bare tag first, then prefixed ones (sorted descending overall).
    return (*numbers, rank, 0 if prefix else 1, _natural_key(suffix), _natural_key(prefix))


def _natural_key(text: str) -> tuple[tuple[int, int, str], ...]:
    return tuple(
        (0, int(part), "") if part.isdigit() else (1, 0, part.lower())
        for part in re.findall(r"\d+|\D+", text)
    )


async def source_tree(
    *,
    repo: str,
    ref: str,
    path: Optional[str],
    settings: Settings,
    recursive: bool = False,
    limit: int = _DEFAULT_TREE_LIMIT,
    offset: int = 0,
    deployment: Optional[str] = None,
    environment: Optional[str] = None,
) -> dict[str, Any]:
    tree_path = normalize_repo_path(path)
    if tree_path:
        _reject_bad_filepath(tree_path)
    if offset < 0:
        raise SourceError("BAD_ARGUMENT", "offset must be >= 0")
    limit = max(1, min(limit, _MAX_TREE_LIMIT))
    repo_dir = await ensure_repo(repo, settings)
    resolved = await _resolve_ref(
        repo, ref, settings, deployment=deployment, environment=environment
    )
    commit = resolved["commit"]
    object_type = "tree"
    if tree_path:
        code, out, _ = await run_git(
            repo_dir, "cat-file", "-t", f"{commit}:{tree_path}",
            settings=settings,
            max_output=2000,
        )
        if code != 0:
            raise SourceError(
                "PATH_NOT_FOUND", f"{tree_path} does not exist in {repo} at {resolved['ref']}"
            )
        object_type = out.strip()
    if object_type == "tree":
        args = ["ls-tree", "-z", "--long"]
        if recursive:
            args.extend(["-r", "-t"])
        args.append(f"{commit}:{tree_path}" if tree_path else commit)
        entry_prefix = f"{tree_path}/" if tree_path else ""
    else:
        args = ["ls-tree", "-z", "--long", commit, "--", tree_path]
        entry_prefix = ""
    code, out, err = await run_git(repo_dir, *args, settings=settings, max_output=_TREE_OUTPUT_CAP)
    if code != 0:
        raise SourceError("GIT_LS_TREE_FAILED", err.strip())
    truncated = out.endswith(_TRUNCATION_MARKER)
    records = out.removesuffix(_TRUNCATION_MARKER).split("\0")
    if truncated and records:
        records = records[:-1]
    entries: list[dict[str, Any]] = []
    for record in records:
        header, tab, name = record.partition("\t")
        fields = header.split()
        if not tab or len(fields) != 4:
            continue
        mode, entry_type, _, size = fields
        entry: dict[str, Any] = {"path": entry_prefix + name, "type": entry_type, "mode": mode}
        if size.isdigit():
            entry["size"] = int(size)
        entries.append(entry)
    total = len(entries)
    page = entries[offset:offset + limit]
    returned_end = offset + len(page)
    has_more = returned_end < total or truncated
    return {
        **resolved,
        "path": tree_path,
        "object_type": object_type,
        "recursive": recursive,
        "entries": page,
        "total": total,
        "total_exact": not truncated,
        "offset": offset,
        "limit": limit,
        "returned": len(page),
        "has_more": has_more,
        "next_offset": returned_end if has_more else None,
    }


def normalize_repo_path(path: Optional[str]) -> str:
    value = (path or "").strip()
    while value.startswith("./"):
        value = value[2:]
    if value in ("", "."):
        return ""
    if value.startswith("/"):
        raise SourceError("BAD_FILEPATH", f"bad filepath: {path}")
    return value.rstrip("/")


async def _resolve_ref(
    repo: str,
    ref: str,
    settings: Settings,
    *,
    deployment: Optional[str] = None,
    environment: Optional[str] = None,
) -> dict[str, Any]:
    if not ref or ref == "runtime":
        from .runtime import resolve_runtime_ref

        return await resolve_runtime_ref(
            repo=repo,
            deployment=deployment,
            settings=settings,
            rev_parse_func=rev_parse,
            environment=environment,
        )
    commit = await rev_parse(repo, ref, settings)
    return {"repo": repo, "ref": ref, "commit": commit, "source": "explicit"}


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

    # Branches go to refs/remotes/origin/* (refreshed on use); a local
    # refs/heads/* copy would silently go stale.
    destination = tag_ref if source_ref == tag_ref else f"refs/remotes/origin/{branch}"
    if destination != tag_ref:
        _BRANCH_FETCHED_AT[(str(path), branch)] = time.time()
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
    # Branches live under refs/remotes/origin/*, which git does not find from a
    # short name; resolve the same way rev_parse does.
    target = await _preferred_ref(path, ref, settings)
    code, out, _ = await run_git(
        path, "rev-parse", "--verify", "-q", f"{target}^{{commit}}",
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
    target = await _preferred_ref(path, ref, settings)
    code, out, _ = await run_git(
        path, "rev-parse", "--verify", "-q", f"{target}^{{commit}}",
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
    existed = path.exists()
    # Repos configured for full history get a complete clone; others stay shallow.
    depth_args = () if settings.is_full_history_repo(repo) else ("--depth=1",)
    code, _, err = await run_git(
        path.parent, "clone", *depth_args, url, str(path),
        settings=settings,
        max_output=8000,
        timeout_seconds=settings.git_history_timeout_seconds,
    )
    if code != 0:
        if not existed and path.exists():
            # Do not leave a half-written clone that later looks like a valid repo.
            shutil.rmtree(path, ignore_errors=True)
        raise SourceError("GIT_CLONE_FAILED", f"{repo}: {err.strip()}")


ALL_BRANCHES_REFSPEC = "+refs/heads/*:refs/remotes/origin/*"
_DEFAULT_BRANCH_CONFIG = "source-mcp.defaultbranch"


async def _configure_origin(path: Path, repo: str, settings: Settings) -> bool:
    """Point origin at the repo URL and set the fetch refspec.

    By default only the remote default branch is fetched (tags come with --tags,
    other branches on demand). Returns True when the refspec had to be changed,
    e.g. a `clone --depth=1`/--single-branch made on a hotfix branch, which made
    `fetch --all` "succeed" while origin/main never moved.
    """
    url = _repo_url(repo, settings)
    code, out, _ = await run_git(
        path, "remote", "get-url", "origin",
        settings=settings,
        max_output=2000,
    )
    if code != 0 or out.strip() != url:
        args = ("remote", "set-url", "origin", url) if code == 0 else ("remote", "add", "origin", url)
        code, _, err = await run_git(path, *args, settings=settings, max_output=4000)
        if code != 0:
            raise SourceError("GIT_REMOTE_FAILED", f"{repo}: {err.strip()}")
    wanted = await desired_fetch_refspec(path, settings)
    if wanted is None or await fetch_refspecs(path, settings) == [wanted]:
        return False
    code, _, err = await run_git(
        path, "config", "--replace-all", "remote.origin.fetch", wanted,
        settings=settings,
        max_output=4000,
    )
    if code != 0:
        raise SourceError("GIT_REMOTE_FAILED", f"{repo}: cannot set fetch refspec: {err.strip()}")
    _LOG.warning("set fetch refspec of %s to %s", repo, wanted)
    return True


async def desired_fetch_refspec(path: Path, settings: Settings) -> Optional[str]:
    if settings.fetch_all_branches:
        return ALL_BRANCHES_REFSPEC
    branch = await _remote_default_branch(path, settings)
    if not branch:
        return None  # unknown (offline); leave the refspec alone
    return f"+refs/heads/{branch}:refs/remotes/origin/{branch}"


async def _remote_default_branch(path: Path, settings: Settings) -> Optional[str]:
    """Remote HEAD branch, asked once and cached in the clone's git config."""
    code, out, _ = await run_git(
        path, "config", "--get", _DEFAULT_BRANCH_CONFIG, settings=settings, max_output=500
    )
    if code == 0 and out.strip():
        return out.strip()
    code, out, _ = await run_git(
        path, "ls-remote", "--symref", "origin", "HEAD",
        settings=settings,
        max_output=4000,
        timeout_seconds=min(settings.git_timeout_seconds, 20.0),
    )
    branch = None
    for line in out.splitlines() if code == 0 else []:
        if line.startswith("ref: refs/heads/") and line.rstrip().endswith("HEAD"):
            branch = line.split(None, 2)[1].removeprefix("refs/heads/")
            break
    if not branch:
        return None
    await run_git(path, "config", _DEFAULT_BRANCH_CONFIG, branch, settings=settings, max_output=500)
    # Keep origin/HEAD (used as default_branch_ref) pointing at the real default.
    await run_git(
        path, "symbolic-ref", "refs/remotes/origin/HEAD", f"refs/remotes/origin/{branch}",
        settings=settings,
        max_output=500,
    )
    return branch


async def fetch_refspecs(path: Path, settings: Settings) -> list[str]:
    code, out, _ = await run_git(
        path, "config", "--get-all", "remote.origin.fetch",
        settings=settings,
        max_output=4000,
    )
    return [line.strip() for line in out.splitlines() if line.strip()] if code == 0 else []


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


def _iso(timestamp: Optional[float]) -> Optional[str]:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def background_refresh_status(repo: str, settings: Settings) -> dict[str, Any]:
    state = dict(_BACKGROUND_STATE.get(repo, {}))
    return {
        "enabled": settings.background_refresh_seconds > 0,
        "interval_seconds": settings.background_refresh_seconds,
        "last_attempt_at": _iso(state.get("last_attempt_at")),
        "last_success_at": _iso(state.get("last_success_at")),
        "last_actions": state.get("last_actions", []),
        "last_error": state.get("last_error"),
        "last_error_at": _iso(state.get("last_error_at")),
    }


async def background_refresh_repo(repo: str, settings: Settings) -> dict[str, Any]:
    """Fetch one repo for the background loop; errors are recorded, never raised."""
    state = _BACKGROUND_STATE.setdefault(repo, {})
    state["last_attempt_at"] = time.time()
    actions: list[str] = []
    errors: list[str] = []
    try:
        path = repo_path(settings, repo)
        async with _repo_lock(repo):
            if not is_git_repo(path):
                await _clone_repo(path, repo, settings)
                actions.append("cloned")
            elif not await _can_auto_fetch(path, repo, settings):
                actions.append("local-only")
            else:
                if await _configure_origin(path, repo, settings):
                    actions.append("refspec-repaired")
                last_fetch = _full_fetch_mtime(path)
                if (
                    "refspec-repaired" not in actions
                    and last_fetch is not None
                    and time.time() - last_fetch < settings.fetch_ttl_seconds
                ):
                    actions.append("fresh")
                else:
                    code, _, err = await run_git(
                        path, "fetch", "--all", "--prune", "--tags", "--force",
                        settings=settings,
                        max_output=8000,
                        timeout_seconds=settings.git_history_timeout_seconds,
                    )
                    if code == 0:
                        _mark_full_fetch(path)
                        actions.append("fetched")
                    else:
                        errors.append(f"fetch failed: {err.strip() or f'git exited {code}'}")
                if settings.is_full_history_repo(repo) and await is_shallow_repo(path, settings):
                    code, _, err = await run_git(
                        path, "fetch", "--all", "--prune", "--tags", "--force", "--unshallow",
                        settings=settings,
                        max_output=8000,
                        timeout_seconds=settings.git_history_timeout_seconds,
                    )
                    if code == 0:
                        _mark_full_fetch(path)
                        actions.append("unshallowed")
                    else:
                        errors.append(f"unshallow failed: {err.strip() or f'git exited {code}'}")
    except Exception as e:  # noqa: BLE001 - one repo must not stop the loop.
        errors.append(e.message if isinstance(e, SourceError) else str(e))
    now = time.time()
    state["last_actions"] = actions
    if errors:
        state["last_error"] = "; ".join(errors)
        state["last_error_at"] = now
        _LOG.warning("background refresh of %s failed: %s", repo, state["last_error"])
    else:
        state["last_error"] = None
        state["last_success_at"] = now
    return {"repo": repo, "actions": actions, "error": state["last_error"]}


async def background_refresh_all(settings: Settings) -> list[dict[str, Any]]:
    _BACKGROUND_RUN["last_run_started_at"] = time.time()
    results = [await background_refresh_repo(repo, settings) for repo in settings.repo_names]
    _BACKGROUND_RUN["last_run_finished_at"] = time.time()
    return results


async def background_refresh_loop(
    settings: Settings,
    *,
    initial_delay: Optional[float] = None,
) -> None:
    interval = settings.background_refresh_seconds
    if interval <= 0:
        return
    delay = min(60.0, float(interval)) if initial_delay is None else initial_delay
    while True:
        await asyncio.sleep(delay)
        try:
            await background_refresh_all(settings)
        except Exception:  # noqa: BLE001 - keep the loop alive.
            _LOG.exception("background refresh run failed")
        delay = float(interval)


_FULL_FETCH_MARKER = "source-mcp-full-fetch"


def _git_dir(path: Path) -> Path:
    return path / ".git" if (path / ".git").is_dir() else path


def _mark_full_fetch(path: Path) -> None:
    """Record a successful `fetch --all`. FETCH_HEAD is also rewritten by the
    single-ref fetches done on demand, so its mtime cannot tell whether the
    branches were refreshed."""
    with suppress(OSError):
        (_git_dir(path) / _FULL_FETCH_MARKER).touch()


def _full_fetch_mtime(path: Path) -> Optional[float]:
    marker = _git_dir(path) / _FULL_FETCH_MARKER
    try:
        return marker.stat().st_mtime
    except OSError:
        return None


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
    paths: Optional[list[str]] = None,
) -> list[str]:
    mode_flag = {"fixed": "-F", "regex": "-E", "perl": "-P"}[mode]
    # -z separates path, line number and text with NUL so paths containing ':' parse safely;
    # -I skips binary files, which otherwise print "Binary file ... matches".
    args = ["grep", "-n", "-z", "-I", mode_flag]
    if ignore_case:
        args.append("-i")
    if context:
        args.append(f"-C{context}")
    args.extend(["-e", pattern, ref])
    if paths:
        args.extend(["--", *(f":(literal){item}" for item in paths)])
    elif glob:
        args.extend(["--", glob])
    return args


def _reject_bad_ref(ref: str) -> None:
    if not isinstance(ref, str) or not ref.strip() or ref.startswith("-") or ".." in ref:
        raise SourceError("BAD_ARGUMENT", f"bad ref: {ref!r}")


def _reject_bad_filepath(filepath: str) -> None:
    if filepath.startswith("/") or ".." in Path(filepath).parts:
        raise SourceError("BAD_FILEPATH", f"bad filepath: {filepath}")
