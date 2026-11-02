from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager, suppress
from typing import Any, AsyncIterator, Optional

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse, StreamingResponse

from .git_ops import (
    DIFF_MODES,
    SEARCH_MODES,
    SourceError,
    background_refresh_loop,
    list_repo_tags,
    read_source,
    refresh_repos,
    rev_parse,
    search_source,
    source_diff,
    source_log,
    source_tree,
    status_for_repo,
    status_for_repos,
)
from .process_health import process_health_snapshot
from .runtime import (
    RuntimeRefUnavailable,
    configured_deployments,
    list_runtime_deployments,
    resolve_runtime_ref,
    select_path_deployment,
)
from .settings import Settings, get_settings


PROTOCOL_VERSION = "2025-03-26"
RUNTIME_REF = "runtime"
_REF_DESCRIPTION = (
    "Git branch, tag or commit SHA. Omitted means 'runtime', the code currently running "
    "in Dify SaaS production. For Enterprise or self-hosted investigations always pass the "
    "customer's version tag or pinned commit; never rely on the default."
)
_DEPLOYMENT_DESCRIPTION = (
    "Kubernetes deployment whose running image tag defines the runtime ref when ref is "
    "omitted or 'runtime' (e.g. dify-web for web/ code). Omitted: chosen from "
    "SOURCE_MCP_RUNTIME_PATH_DEPLOYMENTS by path, else the repo's default deployment."
)
_ENVIRONMENT_DESCRIPTION = (
    "deployment.environment used to resolve the runtime ref (default: the configured "
    "environment, usually production; 'any' disables the filter). An explicit deployment "
    "that has no logs in the default environment is retried in any environment."
)
_DEFAULT_REF_WARNING = (
    "ref was not specified, so the Dify SaaS production runtime code was used. "
    "This is NOT the code of an Enterprise or self-hosted deployment; pass the "
    "customer's version tag or pinned commit as ref for those."
)


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    task: Optional[asyncio.Task[None]] = None
    if settings.background_refresh_seconds > 0:
        task = asyncio.create_task(background_refresh_loop(settings))
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


def create_app() -> FastAPI:
    app = FastAPI(title="source-mcp", version="0.1.0", lifespan=_lifespan)

    @app.get("/health")
    async def health(settings: Settings = Depends(get_settings)) -> dict[str, Any]:
        process_health = process_health_snapshot(
            zombie_warning_threshold=settings.process_zombie_warning_threshold,
            pids_warning_ratio=settings.process_pids_warning_ratio,
        )
        return {
            "ok": not process_health["degraded"],
            "repos": settings.repo_names,
            "runtime_repos": sorted(settings.runtime_deployments.keys()),
            "background_refresh_seconds": settings.background_refresh_seconds,
            "full_repo_search_concurrency": settings.full_repo_search_concurrency,
            "process": process_health,
        }

    @app.get("/sse")
    async def sse(settings: Settings = Depends(_require_auth)) -> StreamingResponse:
        _ = settings
        return StreamingResponse(
            iter(("event: endpoint\ndata: /mcp\n\n",)),
            media_type="text/event-stream",
        )

    @app.post("/mcp")
    async def mcp(
        request: Request,
        settings: Settings = Depends(_require_auth),
    ) -> JSONResponse:
        payload = await request.json()
        request_id = payload.get("id")
        method = payload.get("method")

        if method == "notifications/initialized":
            return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content={})
        if method == "initialize":
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "protocolVersion": PROTOCOL_VERSION,
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "source-mcp", "version": "0.1.0"},
                    },
                }
            )
        if method == "ping":
            return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": {}})
        if method == "tools/list":
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"tools": _tools_list()},
                }
            )
        if method == "tools/call":
            params = payload.get("params") or {}
            result = await _handle_tool_call(
                name=str(params.get("name") or ""),
                arguments=params.get("arguments") or {},
                settings=settings,
            )
            return JSONResponse(_tool_result(request_id, result))

        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": f"method not found: {method}"},
            },
            status_code=status.HTTP_404_NOT_FOUND,
        )

    return app


async def _require_auth(
    authorization: Optional[str] = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> Settings:
    if not settings.api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="SOURCE_MCP_API_KEY is not configured",
        )
    expected = f"Bearer {settings.api_key}"
    if authorization != expected:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")
    return settings


# All tools read source code or telemetry. refresh_repos only updates this
# service's local clone cache, so it is also reported read-only. Clients such as
# OpenConnector derive read/write from these hints; without them unfamiliar tool
# names (source_tree, list_runtime_deployments) were classified as writes.
READ_ONLY_ANNOTATIONS: dict[str, Any] = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": True,
}


def _tools_list() -> list[dict[str, Any]]:
    return [
        {**tool, "annotations": dict(READ_ONLY_ANNOTATIONS)} for tool in _tool_definitions()
    ]


def _tool_definitions() -> list[dict[str, Any]]:
    runtime_props = {
        "deployment": {"type": "string", "description": _DEPLOYMENT_DESCRIPTION},
        "environment": {"type": "string", "description": _ENVIRONMENT_DESCRIPTION},
    }
    return [
        {
            "name": "source_runtime_ref",
            "description": (
                "Resolve the git ref/commit for the code currently running online. Returns "
                "the deployment and deployment.environment that were used. Different "
                "deployments of one repo (e.g. dify-api-oltp vs dify-web) can run "
                "different versions; pass deployment to choose."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string"},
                    **runtime_props,
                },
                "required": ["repo"],
            },
        },
        {
            "name": "list_runtime_deployments",
            "description": (
                "List Kubernetes deployments that reported a container.image.tag in SigNoz "
                "logs within the last hours, with deployment.environment, image_tag, "
                "log_count and latest_ts, plus the configured repo -> deployment mapping. "
                "Use it to find the deployment name to pass to source_runtime_ref or the "
                "source tools."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "hours": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 168,
                        "description": "Look-back window; defaults to SOURCE_MCP_RUNTIME_HOURS.",
                    },
                    "name_contains": {
                        "type": "string",
                        "description": "Substring of the deployment name ([a-z0-9.-]).",
                    },
                    "environment": {
                        "type": "string",
                        "description": "Only this deployment.environment; omitted lists all.",
                    },
                    "limit": {"type": "integer", "default": 200, "minimum": 1, "maximum": 500},
                },
            },
        },
        {
            "name": "source_search",
            "description": (
                "Search source code with git grep. mode=auto (default) uses extended regex, "
                "so 'foo|bar' alternation works, and switches to Perl regex for lookaround, "
                "\\d or inline groups; a leading (?i) is honoured. The result reports the "
                "effective mode; 'no matches' only means no match under that mode. "
                "Returns structured matches paginated by limit/offset with total_matches "
                "and next_offset. Omitting ref searches Dify SaaS production runtime code; "
                "a glob under a path mapped to another deployment (e.g. web/ -> dify-web) "
                "resolves the runtime ref from that deployment."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string"},
                    "pattern": {"type": "string"},
                    "ref": {"type": "string", "description": _REF_DESCRIPTION},
                    **runtime_props,
                    "glob": {"type": "string", "description": "Pathspec such as 'api/**/*.py'."},
                    "mode": {
                        "type": "string",
                        "enum": list(SEARCH_MODES),
                        "default": "auto",
                        "description": "fixed=literal string, regex=POSIX extended, perl=PCRE.",
                    },
                    "case_sensitive": {"type": "boolean", "default": False},
                    "context": {
                        "type": "integer",
                        "default": 0,
                        "minimum": 0,
                        "maximum": 10,
                        "description": "Context lines before and after each match.",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 30,
                        "minimum": 1,
                        "maximum": 200,
                        "description": "Maximum number of matching lines to return.",
                    },
                    "offset": {
                        "type": "integer",
                        "default": 0,
                        "minimum": 0,
                        "description": "Number of matching lines to skip (use next_offset).",
                    },
                },
                "required": ["repo", "pattern"],
            },
        },
        {
            "name": "source_read",
            "description": (
                "Read a file from source code, optionally a 1-based inclusive line range. "
                "Returns total_lines and truncated; continue with start_line when truncated. "
                "Omitting ref reads Dify SaaS production runtime code (web/ files use the "
                "dify-web deployment's version)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string"},
                    "filepath": {"type": "string"},
                    "ref": {"type": "string", "description": _REF_DESCRIPTION},
                    **runtime_props,
                    "start_line": {"type": "integer", "minimum": 1},
                    "end_line": {"type": "integer", "minimum": 1},
                },
                "required": ["repo", "filepath"],
            },
        },
        {
            "name": "source_tree",
            "description": (
                "List a directory with git ls-tree: entries with path, type (blob/tree/"
                "commit) and size for blobs. recursive=true lists the whole subtree. "
                "Paginated by limit/offset. Omitting ref uses Dify SaaS production runtime code."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string"},
                    "ref": {"type": "string", "description": _REF_DESCRIPTION},
                    **runtime_props,
                    "path": {
                        "type": "string",
                        "description": "Directory (or file) relative to the repo root; omitted lists the root.",
                    },
                    "recursive": {"type": "boolean", "default": False},
                    "limit": {"type": "integer", "default": 200, "minimum": 1, "maximum": 1000},
                    "offset": {"type": "integer", "default": 0, "minimum": 0},
                },
                "required": ["repo"],
            },
        },
        {
            "name": "source_log",
            "description": (
                "Show git log for a repository or file. Reports shallow=true when history is "
                "truncated by a shallow clone. Omitting ref uses Dify SaaS production runtime code."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string"},
                    "ref": {"type": "string", "description": _REF_DESCRIPTION},
                    **runtime_props,
                    "filepath": {"type": "string"},
                    "n": {"type": "integer", "default": 20},
                },
                "required": ["repo"],
            },
        },
        {
            "name": "source_diff",
            "description": (
                "Diff two refs. mode=stat lists changed files with line counts, name-status "
                "lists files with A/M/D status, patch returns the full diff. Default is stat, "
                "or patch when filepath is given. Use ref runtime to compare online code; "
                "deployment and environment apply to the runtime side(s)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string"},
                    "ref1": {"type": "string"},
                    "ref2": {"type": "string"},
                    **runtime_props,
                    "filepath": {"type": "string", "description": "File or directory to limit the diff."},
                    "mode": {"type": "string", "enum": list(DIFF_MODES)},
                },
                "required": ["repo", "ref1", "ref2"],
            },
        },
        {
            "name": "repo_status",
            "description": (
                "Show local clone/fetch status for configured source repositories: shallow, "
                "full_history (configured), last_fetch_at, stale and the last background "
                "refresh result. A repo that fails is reported with error instead of failing "
                "the whole call."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"repo": {"type": "string"}},
            },
        },
        {
            "name": "refresh_repos",
            "description": (
                "Fetch or clone configured source repositories. Reports changed and changes "
                "(refs added/updated/removed, history_changed), shallow and warnings so an "
                "ineffective fetch is visible. With several repos, a repo that fails is "
                "reported with ok=false and error while the others continue. With exactly one "
                "repo: refs fetches specific branches, tags or full commit SHAs; deepen=N adds "
                "N commits of history; unshallow=true fetches full history (slow for large repos)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "repos": {"type": "array", "items": {"type": "string"}},
                    "refs": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": 20,
                        "description": "Branches, tags or full 40-character commit SHAs to fetch.",
                    },
                    "deepen": {"type": "integer", "minimum": 1},
                    "unshallow": {"type": "boolean", "default": False},
                },
            },
        },
        {
            "name": "list_repo_tags",
            "description": (
                "List git tags for a source repository. Version tags (optionally v-prefixed) "
                "come first, newest version first; other tags follow sorted by name."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string"},
                    "pattern": {
                        "type": "string",
                        "description": "git tag -l glob such as '1.14.*' or 'v2*'.",
                    },
                    "limit": {"type": "integer", "default": 30, "maximum": 200},
                },
                "required": ["repo"],
            },
        },
    ]


async def _handle_tool_call(
    *,
    name: str,
    arguments: dict[str, Any],
    settings: Settings,
) -> dict[str, Any]:
    try:
        if name == "source_runtime_ref":
            return _success(
                await resolve_runtime_ref(
                    repo=_required_str(arguments, "repo"),
                    deployment=_optional_str(arguments, "deployment"),
                    environment=_optional_str(arguments, "environment"),
                    settings=settings,
                    rev_parse_func=rev_parse,
                )
            )
        if name == "list_runtime_deployments":
            return _success(
                await list_runtime_deployments(
                    settings=settings,
                    hours=_optional_int_or_none(arguments, "hours"),
                    name_contains=_optional_str(arguments, "name_contains"),
                    environment=_optional_str(arguments, "environment"),
                    limit=_optional_int(arguments, "limit", 200),
                )
            )
        if name == "source_search":
            repo = _required_str(arguments, "repo")
            glob = _optional_str(arguments, "glob")
            plan = _plan_runtime(settings, repo, arguments, path=glob, is_pattern=True)
            data = await search_source(
                repo=repo,
                ref=plan["ref"],
                pattern=_required_str(arguments, "pattern", strip=False),
                glob=glob,
                limit=_optional_int(arguments, "limit", 30),
                offset=_optional_int(arguments, "offset", 0),
                mode=_optional_str(arguments, "mode") or "auto",
                case_sensitive=_optional_bool(arguments, "case_sensitive", False),
                context=_optional_int(arguments, "context", 0),
                deployment=plan["deployment"],
                environment=plan["environment"],
                settings=settings,
            )
            return _success_for_ref(_annotate_runtime(settings, repo, data, plan, [data]), plan)
        if name == "source_read":
            repo = _required_str(arguments, "repo")
            filepath = _required_str(arguments, "filepath")
            plan = _plan_runtime(settings, repo, arguments, path=filepath)
            data = await read_source(
                repo=repo,
                ref=plan["ref"],
                filepath=filepath,
                start_line=_optional_int_or_none(arguments, "start_line"),
                end_line=_optional_int_or_none(arguments, "end_line"),
                deployment=plan["deployment"],
                environment=plan["environment"],
                settings=settings,
            )
            return _success_for_ref(_annotate_runtime(settings, repo, data, plan, [data]), plan)
        if name == "source_tree":
            repo = _required_str(arguments, "repo")
            tree_path = _optional_str(arguments, "path")
            plan = _plan_runtime(settings, repo, arguments, path=tree_path)
            data = await source_tree(
                repo=repo,
                ref=plan["ref"],
                path=tree_path,
                recursive=_optional_bool(arguments, "recursive", False),
                limit=_optional_int(arguments, "limit", 200),
                offset=_optional_int(arguments, "offset", 0),
                deployment=plan["deployment"],
                environment=plan["environment"],
                settings=settings,
            )
            return _success_for_ref(_annotate_runtime(settings, repo, data, plan, [data]), plan)
        if name == "source_log":
            repo = _required_str(arguments, "repo")
            filepath = _optional_str(arguments, "filepath")
            plan = _plan_runtime(settings, repo, arguments, path=filepath)
            data = await source_log(
                repo=repo,
                ref=plan["ref"],
                filepath=filepath,
                n=_optional_int(arguments, "n", 20),
                deployment=plan["deployment"],
                environment=plan["environment"],
                settings=settings,
            )
            return _success_for_ref(_annotate_runtime(settings, repo, data, plan, [data]), plan)
        if name == "source_diff":
            repo = _required_str(arguments, "repo")
            ref1 = _required_str(arguments, "ref1")
            ref2 = _required_str(arguments, "ref2")
            filepath = _optional_str(arguments, "filepath")
            uses_runtime = RUNTIME_REF in (ref1, ref2)
            plan = _plan_runtime(
                settings,
                repo,
                {**arguments, "ref": RUNTIME_REF if uses_runtime else ref1},
                path=filepath,
            )
            data = await source_diff(
                repo=repo,
                ref1=ref1,
                ref2=ref2,
                filepath=filepath,
                mode=_optional_str(arguments, "mode"),
                deployment=plan["deployment"],
                environment=plan["environment"],
                settings=settings,
            )
            runtime_sides = list((data.get("runtime") or {}).values())
            return _success(_annotate_runtime(settings, repo, data, plan, runtime_sides))
        if name == "repo_status":
            repo = _optional_str(arguments, "repo")
            if repo:
                return _success(await status_for_repo(repo, settings))
            return _success(await status_for_repos(settings))
        if name == "refresh_repos":
            return _success(
                await refresh_repos(
                    settings,
                    repos=_optional_str_list(arguments, "repos"),
                    refs=_optional_str_list(arguments, "refs"),
                    deepen=_optional_int_or_none(arguments, "deepen"),
                    unshallow=_optional_bool(arguments, "unshallow", False),
                )
            )
        if name == "list_repo_tags":
            return _success(
                await list_repo_tags(
                    repo=_required_str(arguments, "repo"),
                    limit=_optional_int(arguments, "limit", 30),
                    pattern=_optional_str(arguments, "pattern"),
                    settings=settings,
                )
            )
        return _failure("UNKNOWN_TOOL", f"unknown tool: {name}")
    except RuntimeRefUnavailable as e:
        return _failure("RUNTIME_REF_UNAVAILABLE", e.message)
    except SourceError as e:
        return _failure(e.code, e.message)
    except Exception as e:  # noqa: BLE001 - expose controlled MCP error payload.
        return _failure("INTERNAL_ERROR", str(e))


def _required_str(arguments: dict[str, Any], name: str, *, strip: bool = True) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip():
        raise SourceError("BAD_ARGUMENT", f"{name} is required")
    return value.strip() if strip else value


def _optional_str(arguments: dict[str, Any], name: str) -> Optional[str]:
    value = arguments.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise SourceError("BAD_ARGUMENT", f"{name} must be a string")
    return value.strip() or None


def _optional_int(arguments: dict[str, Any], name: str, default: int) -> int:
    value = arguments.get(name, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        raise SourceError("BAD_ARGUMENT", f"{name} must be an integer") from None


def _optional_int_or_none(arguments: dict[str, Any], name: str) -> Optional[int]:
    if arguments.get(name) is None:
        return None
    return _optional_int(arguments, name, 0)


def _optional_bool(arguments: dict[str, Any], name: str, default: bool) -> bool:
    value = arguments.get(name, default)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise SourceError("BAD_ARGUMENT", f"{name} must be a boolean")
    return value


def _optional_str_list(arguments: dict[str, Any], name: str) -> Optional[list[str]]:
    value = arguments.get(name)
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise SourceError("BAD_ARGUMENT", f"{name} must be a string array")
    return [item.strip() for item in value]


def _success(data: Any) -> dict[str, Any]:
    return {"success": True, "data": data}


def _success_for_ref(data: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    if plan["ref_defaulted"]:
        data = {**data, "ref_defaulted": True, "ref_warning": _DEFAULT_REF_WARNING}
    return _success(data)


def _plan_runtime(
    settings: Settings,
    repo: str,
    arguments: dict[str, Any],
    *,
    path: Optional[str],
    is_pattern: bool = False,
) -> dict[str, Any]:
    """Pick the ref and, for runtime refs, the deployment/environment to resolve it with."""
    ref = _optional_str(arguments, "ref")
    deployment = _optional_str(arguments, "deployment")
    environment = _optional_str(arguments, "environment")
    plan: dict[str, Any] = {
        "ref": ref or RUNTIME_REF,
        "ref_defaulted": ref is None,
        "deployment": deployment,
        "environment": environment,
        "selection": None,
        "notes": [],
    }
    if plan["ref"] != RUNTIME_REF:
        if deployment or environment:
            plan["notes"].append(
                "deployment/environment only apply when ref is omitted or 'runtime'; "
                "they were ignored for the explicit ref"
            )
        plan["deployment"] = plan["environment"] = None
        return plan
    if deployment:
        plan["selection"] = {"kind": "explicit"}
        return plan
    selection = select_path_deployment(settings, repo, path, is_pattern=is_pattern)
    plan["selection"] = selection
    if selection["kind"] == "match":
        plan["deployment"] = selection["deployment"]
    return plan


def _annotate_runtime(
    settings: Settings,
    repo: str,
    data: dict[str, Any],
    plan: dict[str, Any],
    resolved_items: list[dict[str, Any]],
) -> dict[str, Any]:
    warnings = list(plan["notes"])
    selection = plan.get("selection") or {}
    signoz = [item for item in resolved_items if item.get("source") == "signoz"]
    overridden = any(item.get("source") == "override" for item in resolved_items)
    if overridden and selection.get("kind") in ("explicit", "match"):
        warnings.append(
            f"SOURCE_MCP_RUNTIME_REF_OVERRIDES pins the runtime ref of {repo}; "
            "deployment selection was not used"
        )
    if signoz:
        used = str(signoz[0].get("deployment") or "")
        defaults = configured_deployments(settings, repo)
        default = defaults[0] if defaults else "the default deployment"
        kind = selection.get("kind")
        if kind == "explicit":
            data["deployment_selection"] = "explicit"
        elif kind == "match":
            data["deployment_selection"] = "path"
            warnings.append(
                f"path is under {selection['prefix']}, which runs as deployment "
                f"{selection['deployment']}; its online version can differ from {repo}'s "
                f"default deployment ({default}). The runtime ref was resolved from "
                f"deployment {used}."
            )
        else:
            data["deployment_selection"] = "default"
            for prefix, deployment in (selection.get("prefixes") or {}).items():
                warnings.append(
                    f"the runtime ref was resolved from deployment {used}; results under "
                    f"{prefix} therefore reflect {used}'s version, while {prefix} runs as "
                    f"deployment {deployment} and may be on a different version. Pass "
                    f"deployment={deployment} (or limit the path to {prefix}) for that code."
                )
    if warnings:
        data = {**data, "deployment_warning": " ".join(warnings)}
    return data


def _failure(code: str, error: str) -> dict[str, Any]:
    return {"success": False, "code": code, "error": error}


def _tool_result(request_id: Any, payload: dict[str, Any]) -> dict[str, Any]:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "content": [{"type": "text", "text": text}],
            "structuredContent": payload,
            "isError": not payload.get("success", False),
        },
    }


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "source_mcp.app:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
    )


if __name__ == "__main__":
    main()
