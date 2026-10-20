from __future__ import annotations

import json
from typing import Any, Optional

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse, StreamingResponse

from .git_ops import (
    DIFF_MODES,
    SEARCH_MODES,
    SourceError,
    list_repo_tags,
    read_source,
    refresh_repos,
    rev_parse,
    search_source,
    source_diff,
    source_log,
    status_for_repo,
)
from .process_health import process_health_snapshot
from .runtime import RuntimeRefUnavailable, resolve_runtime_ref
from .settings import Settings, get_settings


PROTOCOL_VERSION = "2025-03-26"
RUNTIME_REF = "runtime"
_REF_DESCRIPTION = (
    "Git branch, tag or commit SHA. Omitted means 'runtime', the code currently running "
    "in Dify SaaS production. For Enterprise or self-hosted investigations always pass the "
    "customer's version tag or pinned commit; never rely on the default."
)
_DEFAULT_REF_WARNING = (
    "ref was not specified, so the Dify SaaS production runtime code was used. "
    "This is NOT the code of an Enterprise or self-hosted deployment; pass the "
    "customer's version tag or pinned commit as ref for those."
)


def create_app() -> FastAPI:
    app = FastAPI(title="source-mcp", version="0.1.0")

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


def _tools_list() -> list[dict[str, Any]]:
    return [
        {
            "name": "source_runtime_ref",
            "description": "Resolve the git ref/commit for the code currently running online.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string"},
                    "deployment": {"type": "string"},
                },
                "required": ["repo"],
            },
        },
        {
            "name": "source_search",
            "description": (
                "Search source code with git grep. mode=auto (default) uses extended regex, "
                "so 'foo|bar' alternation works, and switches to Perl regex for lookaround, "
                "\\d or inline groups; a leading (?i) is honoured. The result reports the "
                "effective mode; 'no matches' only means no match under that mode. "
                "Omitting ref searches Dify SaaS production runtime code."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string"},
                    "pattern": {"type": "string"},
                    "ref": {"type": "string", "description": _REF_DESCRIPTION},
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
                    "limit": {"type": "integer", "default": 30},
                },
                "required": ["repo", "pattern"],
            },
        },
        {
            "name": "source_read",
            "description": (
                "Read a file from source code, optionally a 1-based inclusive line range. "
                "Returns total_lines and truncated; continue with start_line when truncated. "
                "Omitting ref reads Dify SaaS production runtime code."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string"},
                    "filepath": {"type": "string"},
                    "ref": {"type": "string", "description": _REF_DESCRIPTION},
                    "start_line": {"type": "integer", "minimum": 1},
                    "end_line": {"type": "integer", "minimum": 1},
                },
                "required": ["repo", "filepath"],
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
                "or patch when filepath is given. Use ref runtime to compare online code."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string"},
                    "ref1": {"type": "string"},
                    "ref2": {"type": "string"},
                    "filepath": {"type": "string", "description": "File or directory to limit the diff."},
                    "mode": {"type": "string", "enum": list(DIFF_MODES)},
                },
                "required": ["repo", "ref1", "ref2"],
            },
        },
        {
            "name": "repo_status",
            "description": (
                "Show local clone/fetch status for configured source repositories, "
                "including whether each clone is shallow."
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
                "ineffective fetch is visible. With exactly one repo: refs fetches specific "
                "branches, tags or full commit SHAs; deepen=N adds N commits of history; "
                "unshallow=true fetches full history (slow for large repos)."
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
            "description": "List git tags for a source repository.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string"},
                    "limit": {"type": "integer", "default": 30},
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
                    settings=settings,
                    rev_parse_func=rev_parse,
                )
            )
        if name == "source_search":
            ref = _optional_str(arguments, "ref")
            return _success_for_ref(
                await search_source(
                    repo=_required_str(arguments, "repo"),
                    ref=ref or RUNTIME_REF,
                    pattern=_required_str(arguments, "pattern", strip=False),
                    glob=_optional_str(arguments, "glob"),
                    limit=_optional_int(arguments, "limit", 30),
                    mode=_optional_str(arguments, "mode") or "auto",
                    case_sensitive=_optional_bool(arguments, "case_sensitive", False),
                    context=_optional_int(arguments, "context", 0),
                    settings=settings,
                ),
                ref_defaulted=ref is None,
            )
        if name == "source_read":
            ref = _optional_str(arguments, "ref")
            return _success_for_ref(
                await read_source(
                    repo=_required_str(arguments, "repo"),
                    ref=ref or RUNTIME_REF,
                    filepath=_required_str(arguments, "filepath"),
                    start_line=_optional_int_or_none(arguments, "start_line"),
                    end_line=_optional_int_or_none(arguments, "end_line"),
                    settings=settings,
                ),
                ref_defaulted=ref is None,
            )
        if name == "source_log":
            ref = _optional_str(arguments, "ref")
            return _success_for_ref(
                await source_log(
                    repo=_required_str(arguments, "repo"),
                    ref=ref or RUNTIME_REF,
                    filepath=_optional_str(arguments, "filepath"),
                    n=_optional_int(arguments, "n", 20),
                    settings=settings,
                ),
                ref_defaulted=ref is None,
            )
        if name == "source_diff":
            return _success(
                await source_diff(
                    repo=_required_str(arguments, "repo"),
                    ref1=_required_str(arguments, "ref1"),
                    ref2=_required_str(arguments, "ref2"),
                    filepath=_optional_str(arguments, "filepath"),
                    mode=_optional_str(arguments, "mode"),
                    settings=settings,
                )
            )
        if name == "repo_status":
            repo = _optional_str(arguments, "repo")
            if repo:
                return _success(await status_for_repo(repo, settings))
            return _success([await status_for_repo(repo_name, settings) for repo_name in settings.repo_names])
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


def _success_for_ref(data: dict[str, Any], *, ref_defaulted: bool) -> dict[str, Any]:
    if ref_defaulted:
        data = {**data, "ref_defaulted": True, "ref_warning": _DEFAULT_REF_WARNING}
    return _success(data)


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
