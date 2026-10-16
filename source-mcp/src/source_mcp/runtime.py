from __future__ import annotations

import json
import re
import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Optional
from urllib import error as urlerror
from urllib import request as urlrequest

from .errors import SourceError
from .settings import Settings


RevParseFunc = Callable[[str, str, Settings], Awaitable[str]]


class RuntimeRefUnavailable(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


_BASE_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+)")
_SEMVER_VARIANT_RE = re.compile(r"^(\d+\.\d+\.\d+)(?:[-_].+)$")
_KUBERNETES_DEPLOYMENT_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$"
)


def image_tag_to_git_ref(tag: str) -> str:
    if tag.startswith("hotfix-"):
        return "hotfix/" + tag[len("hotfix-"):]
    return tag


def runtime_ref_candidates(tag: str) -> list[str]:
    semver_variant = _SEMVER_VARIANT_RE.match(tag)
    if semver_variant and tag.endswith("-serverless"):
        candidates = [semver_variant.group(1), tag]
        return list(dict.fromkeys(candidates))

    candidates = [image_tag_to_git_ref(tag), tag]
    if semver_variant:
        candidates.append(semver_variant.group(1))
    return list(dict.fromkeys(candidates))


async def resolve_runtime_ref(
    *,
    repo: str,
    deployment: Optional[str],
    settings: Settings,
    rev_parse_func: RevParseFunc,
) -> dict[str, Any]:
    overrides = settings.runtime_ref_overrides
    if repo in overrides:
        ref = str(overrides[repo])
        commit = await rev_parse_func(repo, ref, settings)
        return {
            "repo": repo,
            "ref": ref,
            "commit": commit,
            "source": "override",
        }

    deployment_names = _runtime_deployment_candidates(
        repo=repo,
        deployment=deployment,
        settings=settings,
    )
    if not deployment_names:
        raise RuntimeRefUnavailable(
            f"no runtime deployment configured for repo {repo}; pass explicit ref or deployment"
        )

    deployment_name = ""
    tag: Optional[str] = None
    for candidate in deployment_names:
        tag = await _query_runtime_image_tag(candidate, settings)
        if tag:
            deployment_name = candidate
            break
    if not tag:
        raise RuntimeRefUnavailable(
            f"no runtime image tag found for deployments {', '.join(deployment_names)}"
        )
    candidates = runtime_ref_candidates(tag)
    last_error = ""
    for ref in dict.fromkeys(candidates):
        try:
            commit = await rev_parse_func(repo, ref, settings)
            return {
                "repo": repo,
                "ref": ref,
                "commit": commit,
                "image_tag": tag,
                "deployment": deployment_name,
                "base_version": extract_base_version(tag),
                "source": "signoz",
            }
        except SourceError as e:
            if e.code != "REF_NOT_FOUND":
                raise
            last_error = str(e)
    raise SourceError(
        "GIT_REF_UNPUBLISHED",
        f"runtime image tag {tag!r} for {deployment_name} did not resolve in {repo}: {last_error}",
    )


def _runtime_deployment_candidates(
    *,
    repo: str,
    deployment: Optional[str],
    settings: Settings,
) -> list[str]:
    if deployment:
        return [deployment]

    configured = settings.runtime_deployments.get(repo)
    if not isinstance(configured, str):
        return []
    return list(
        dict.fromkeys(name.strip() for name in configured.split(",") if name.strip())
    )


def extract_base_version(tag: str) -> str:
    m = _BASE_VERSION_RE.search(tag or "")
    return m.group(1) if m else ""


async def _query_runtime_image_tag(deployment: str, settings: Settings) -> Optional[str]:
    endpoint = _mcp_endpoint(settings.signoz_mcp_url)
    if not endpoint:
        raise _runtime_telemetry_error(deployment, "SigNoz MCP URL is not configured")
    sql = _runtime_sql(deployment, settings.runtime_environment)
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "sz_run",
            "arguments": {"args": ["query", sql, "--hours", str(settings.runtime_hours)]},
        },
    }
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    if settings.signoz_mcp_api_key:
        headers["Authorization"] = f"Bearer {settings.signoz_mcp_api_key}"
    status_code, text = await asyncio.to_thread(
        _post_json,
        endpoint,
        payload,
        headers,
        settings.signoz_mcp_timeout_seconds + 5,
    )
    if status_code >= 400:
        raise _runtime_telemetry_error(deployment, f"SigNoz MCP returned HTTP {status_code}")
    try:
        data = _load_json_or_sse(text)
    except (json.JSONDecodeError, TypeError, ValueError) as e:
        raise _runtime_telemetry_error(deployment, "SigNoz MCP returned invalid JSON") from e
    raw = data.get("result", {}) if isinstance(data, dict) else {}
    content = raw.get("content") if isinstance(raw, dict) else None
    if not isinstance(content, list) or not content:
        raise _runtime_telemetry_error(deployment, "SigNoz MCP response has no content")
    first = content[0]
    if not isinstance(first, dict):
        raise _runtime_telemetry_error(deployment, "SigNoz MCP content is invalid")
    try:
        parsed = json.loads(first.get("text") or "{}")
    except json.JSONDecodeError:
        raise _runtime_telemetry_error(deployment, "SigNoz tool returned invalid JSON") from None
    if not parsed.get("success"):
        raise _runtime_telemetry_error(deployment, "SigNoz tool query failed")
    rows = parsed.get("data")
    if not isinstance(rows, list):
        raise _runtime_telemetry_error(deployment, "SigNoz tool returned invalid rows")
    return _select_primary_image_tag(rows)


def _runtime_telemetry_error(deployment: str, message: str) -> SourceError:
    return SourceError("RUNTIME_TELEMETRY_FAILED", f"{message} for deployment {deployment}")


def _runtime_sql(deployment: str, environment: str) -> str:
    if not _KUBERNETES_DEPLOYMENT_RE.fullmatch(deployment):
        raise RuntimeRefUnavailable(f"invalid Kubernetes deployment name: {deployment!r}")
    deployment_q = deployment.replace("'", "''")
    environment_q = environment.replace("'", "''")
    return (
        "SELECT "
        "resources_string['container.image.tag'] AS image_tag, "
        "any(resources_string['service.version']) AS service_version, "
        "count() AS log_count, "
        "max(timestamp) AS latest_ts "
        "FROM signoz_logs.distributed_logs_v2 "
        "WHERE timestamp >= {start_ns} "
        f"AND resources_string['k8s.deployment.name'] = '{deployment_q}' "
        f"AND resources_string['deployment.environment'] = '{environment_q}' "
        "AND resources_string['container.image.tag'] != '' "
        "GROUP BY image_tag "
        "ORDER BY latest_ts DESC, log_count DESC "
        "LIMIT 5"
    )


def _select_primary_image_tag(rows: list[dict[str, Any]]) -> Optional[str]:
    candidates: list[tuple[float, int, int, str]] = []
    first_seen: Optional[str] = None
    for index, row in enumerate(rows):
        tag = (row or {}).get("image_tag")
        if not tag:
            continue
        tag_s = str(tag)
        first_seen = first_seen or tag_s
        candidates.append((
            _coerce_float(row.get("latest_ts")),
            _coerce_int(row.get("log_count")),
            -index,
            tag_s,
        ))
    if not candidates:
        return first_seen
    candidates.sort(reverse=True)
    return candidates[0][3]


def _coerce_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _coerce_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _mcp_endpoint(url: str) -> str:
    base = (url or "").rstrip("/")
    if not base:
        return ""
    return base if base.endswith("/mcp") else f"{base}/mcp"


def _load_json_or_sse(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("{"):
        return json.loads(stripped)
    chunks: list[str] = []
    for line in stripped.splitlines():
        if line.startswith("data:"):
            chunks.append(line.split(":", 1)[1].strip())
    if not chunks:
        return {}
    return json.loads("\n".join(chunks))


def _post_json(
    endpoint: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    timeout_seconds: float,
) -> tuple[int, str]:
    body = json.dumps(payload).encode("utf-8")
    req = urlrequest.Request(endpoint, data=body, headers=headers, method="POST")
    try:
        with urlrequest.urlopen(req, timeout=timeout_seconds) as resp:  # noqa: S310 - configured internal endpoint.
            return int(resp.status), resp.read().decode("utf-8", errors="replace")
    except urlerror.HTTPError as e:
        return int(e.code), e.read().decode("utf-8", errors="replace")
    except urlerror.URLError:
        return 599, ""
