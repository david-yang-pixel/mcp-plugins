from __future__ import annotations

import json
from typing import Any, Optional

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse, StreamingResponse

from .git_ops import (
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
            "description": "Search source code. Defaults ref to runtime, meaning the online running code.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string"},
                    "pattern": {"type": "string"},
                    "ref": {"type": "string", "default": "runtime"},
                    "glob": {"type": "string"},
                    "limit": {"type": "integer", "default": 30},
                },
                "required": ["repo", "pattern"],
            },
        },
        {
            "name": "source_read",
            "description": "Read a file from source code. Defaults ref to runtime.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string"},
                    "filepath": {"type": "string"},
                    "ref": {"type": "string", "default": "runtime"},
                },
                "required": ["repo", "filepath"],
            },
        },
        {
            "name": "source_log",
            "description": "Show git log for a repository or file. Defaults ref to runtime.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string"},
                    "ref": {"type": "string", "default": "runtime"},
                    "filepath": {"type": "string"},
                    "n": {"type": "integer", "default": 20},
                },
                "required": ["repo"],
            },
        },
        {
            "name": "source_diff",
            "description": "Show diff/stat between two refs. Use ref runtime to compare online code.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string"},
                    "ref1": {"type": "string"},
                    "ref2": {"type": "string"},
                    "filepath": {"type": "string"},
                },
                "required": ["repo", "ref1", "ref2"],
            },
        },
        {
            "name": "repo_status",
            "description": "Show local clone/fetch status for configured source repositories.",
            "inputSchema": {
                "type": "object",
                "properties": {"repo": {"type": "string"}},
            },
        },
        {
            "name": "refresh_repos",
            "description": "Fetch or clone configured source repositories.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "repos": {"type": "array", "items": {"type": "string"}},
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
            return _success(
                await search_source(
                    repo=_required_str(arguments, "repo"),
                    ref=_optional_str(arguments, "ref") or "runtime",
                    pattern=_required_str(arguments, "pattern"),
                    glob=_optional_str(arguments, "glob"),
                    limit=_optional_int(arguments, "limit", 30),
                    settings=settings,
                )
            )
        if name == "source_read":
            return _success(
                await read_source(
                    repo=_required_str(arguments, "repo"),
                    ref=_optional_str(arguments, "ref") or "runtime",
                    filepath=_required_str(arguments, "filepath"),
                    settings=settings,
                )
            )
        if name == "source_log":
            return _success(
                await source_log(
                    repo=_required_str(arguments, "repo"),
                    ref=_optional_str(arguments, "ref") or "runtime",
                    filepath=_optional_str(arguments, "filepath"),
                    n=_optional_int(arguments, "n", 20),
                    settings=settings,
                )
            )
        if name == "source_diff":
            return _success(
                await source_diff(
                    repo=_required_str(arguments, "repo"),
                    ref1=_required_str(arguments, "ref1"),
                    ref2=_required_str(arguments, "ref2"),
                    filepath=_optional_str(arguments, "filepath"),
                    settings=settings,
                )
            )
        if name == "repo_status":
            repo = _optional_str(arguments, "repo")
            if repo:
                return _success(await status_for_repo(repo, settings))
            return _success([await status_for_repo(repo_name, settings) for repo_name in settings.repo_names])
        if name == "refresh_repos":
            repos = arguments.get("repos")
            if repos is not None and not isinstance(repos, list):
                raise SourceError("BAD_ARGUMENT", "repos must be a string array")
            return _success(await refresh_repos(settings, repos=repos))
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


def _required_str(arguments: dict[str, Any], name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip():
        raise SourceError("BAD_ARGUMENT", f"{name} is required")
    return value.strip()


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


def _success(data: Any) -> dict[str, Any]:
    return {"success": True, "data": data}


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
