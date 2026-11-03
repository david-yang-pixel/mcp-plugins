from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
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


ANY_ENVIRONMENT = "any"
_ENVIRONMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
_NAME_FILTER_RE = re.compile(r"^[a-z0-9.-]{1,63}$")
_MAX_RUNTIME_HOURS = 168
_MAX_DEPLOYMENT_ROWS = 500


async def resolve_runtime_ref(
    *,
    repo: str,
    deployment: Optional[str],
    settings: Settings,
    rev_parse_func: RevParseFunc,
    environment: Optional[str] = None,
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
            f"no runtime deployment configured for repo {repo}; pass explicit ref or deployment "
            "(list_runtime_deployments shows deployments with image tags)"
        )

    requested_environment = environment or settings.runtime_environment
    _validate_environment(requested_environment)
    deployment_name, image = await _first_runtime_image(deployment_names, requested_environment, settings)
    environment_fallback = False
    if image is None and deployment and not environment and not _is_any_environment(requested_environment):
        # An explicitly requested deployment may live in another environment (e.g. gray).
        deployment_name, image = await _first_runtime_image(deployment_names, ANY_ENVIRONMENT, settings)
        environment_fallback = image is not None
    gitops_image: Optional[dict[str, Any]] = None
    if image is None:
        gitops_environment = (
            settings.runtime_environment
            if _is_any_environment(requested_environment)
            else requested_environment
        )
        deployment_name, gitops_image = await _first_gitops_image(
            deployment_names, gitops_environment, settings
        )
        if gitops_image and gitops_image.get("scaled_to_zero"):
            raise RuntimeRefUnavailable(
                f"deployment {deployment_name} has no logs and {gitops_image['gitops']['repo']}:"
                f"{gitops_image['gitops']['path']} scales it to 0 replicas, so it is not running "
                f"(declared tag {gitops_image['image_tag']!r} is not a runtime version)"
                + _runtime_notes(settings, deployment_names)
            )
        image = gitops_image
    if image is None:
        scope = (
            "any environment"
            if _is_any_environment(requested_environment)
            else f"environment {requested_environment}"
        )
        raise RuntimeRefUnavailable(
            f"no runtime image tag found for deployments {', '.join(deployment_names)} in {scope} "
            f"within the last {settings.runtime_hours}h, and no image declared for them in "
            f"{settings.gitops_repo} environments/; list_runtime_deployments shows what is running"
            + _runtime_notes(settings, deployment_names)
        )
    tag = image["image_tag"]
    candidates = _prefixed_candidates(settings, repo, deployment_name, runtime_ref_candidates(tag))
    ref, commit, last_error = await _first_resolvable(repo, candidates, settings, rev_parse_func)
    if ref is None:
        component_refs = [
            name for name in await _component_tag_candidates(repo, tag, settings)
            if name not in candidates
        ]
        ref, commit, component_error = await _first_resolvable(
            repo, component_refs, settings, rev_parse_func
        )
        last_error = last_error or component_error
    if ref is not None:
        result: dict[str, Any] = {
            "repo": repo,
            "ref": ref,
            "commit": commit,
            "image_tag": tag,
            "deployment": deployment_name,
            "environment": image.get("environment") or requested_environment,
            "base_version": extract_base_version(tag),
            "source": "gitops" if gitops_image else "signoz",
        }
        if gitops_image:
            result["gitops"] = gitops_image["gitops"]
            result["source_warning"] = (
                f"no image tag for {deployment_name} in telemetry; used the tag declared in "
                f"{gitops_image['gitops']['repo']}:{gitops_image['gitops']['path']} "
                "(GitOps desired state, not observed running)"
            )
        if environment_fallback:
            result["environment_fallback"] = True
            result["environment_warning"] = (
                f"deployment {deployment_name} has no logs in environment "
                f"{requested_environment}; used environment {result['environment']} instead"
            )
        return result
    raise SourceError(
        "GIT_REF_UNPUBLISHED",
        f"runtime image tag {tag!r} for {deployment_name} did not resolve in {repo}: {last_error}",
    )


def _runtime_notes(settings: Settings, deployment_names: list[str]) -> str:
    notes = [settings.runtime_notes[name] for name in deployment_names if name in settings.runtime_notes]
    return "".join(f". {note}" for note in dict.fromkeys(notes))


async def _first_resolvable(
    repo: str,
    candidates: list[str],
    settings: Settings,
    rev_parse_func: RevParseFunc,
) -> tuple[Optional[str], str, str]:
    last_error = ""
    for ref in dict.fromkeys(candidates):
        try:
            return ref, await rev_parse_func(repo, ref, settings), last_error
        except SourceError as e:
            if e.code != "REF_NOT_FOUND":
                raise
            last_error = str(e)
    return None, "", last_error


def _prefixed_candidates(
    settings: Settings,
    repo: str,
    deployment: str,
    candidates: list[str],
) -> list[str]:
    mapping = settings.runtime_tag_prefixes.get(repo) or {}
    prefix = mapping.get(deployment) or mapping.get("*")
    if not prefix:
        return list(candidates)
    return list(dict.fromkeys([prefix + ref for ref in candidates] + candidates))


async def _component_tag_candidates(repo: str, tag: str, settings: Settings) -> list[str]:
    """Tags such as backend/2.3.2 for image tag 2.3.2 when no prefix is configured."""
    if not tag or "/" in tag or any(ch in tag for ch in "*?[]\\"):
        return []
    from .git_ops import is_git_repo, list_repo_tags, repo_path

    try:
        if not is_git_repo(repo_path(settings, repo)):
            return []
        listed = await list_repo_tags(repo, 200, settings, pattern=f"*/{tag}")
    except SourceError:
        return []
    matches = [name for name in listed["tags"] if name.endswith("/" + tag)]
    # Only unambiguous: with several components (backend/, ops/) configure a prefix.
    return matches if len(matches) == 1 else []


async def _first_gitops_image(
    deployment_names: list[str],
    environment: str,
    settings: Settings,
) -> tuple[str, Optional[dict[str, Any]]]:
    for candidate in deployment_names:
        image = await gitops_image_tag(candidate, environment, settings)
        if image:
            return candidate, image
    return "", None


_KUSTOMIZE_IMAGE_NAME_RE = re.compile(r"^(\s*)-?\s*name:\s*['\"]?([^'\"\s#]+)['\"]?\s*(?:#.*)?$")
_KUSTOMIZE_NEW_TAG_RE = re.compile(r"^\s*newTag:\s*['\"]?([^'\"\s#]+)['\"]?\s*(?:#.*)?$")


async def gitops_image_tag(
    deployment: str,
    environment: str,
    settings: Settings,
) -> Optional[dict[str, Any]]:
    """Image tag declared for a deployment in the GitOps repo, or None.

    Looks in <gitops_repo>:environments/<environment>/**/kustomization.yaml for the
    image mapped to the deployment (SOURCE_MCP_GITOPS_IMAGES). When several overlays
    declare the image, the one whose directory matches the deployment name wins.
    """
    image_name = settings.gitops_images.get(deployment)
    gitops_repo = settings.gitops_repo
    if not image_name or not gitops_repo or gitops_repo not in settings.repo_names:
        return None
    if not _ENVIRONMENT_RE.fullmatch(environment or ""):
        return None
    from .git_ops import default_branch_ref, is_git_repo, repo_path, run_git

    try:
        path = repo_path(settings, gitops_repo)
        if not is_git_repo(path):
            return None  # never clone from the request path; the background refresh clones it
        ref = await default_branch_ref(path, settings)
        code, out, _ = await run_git(
            path, "grep", "-l", "-F", "-e", image_name, ref, "--",
            f"environments/{environment}/*kustomization.yaml",
            settings=settings,
            max_output=200_000,
        )
    except SourceError:
        return None
    if code != 0:
        return None
    files = [line.split(":", 1)[1] for line in out.splitlines() if ":" in line]
    declared: list[tuple[str, str]] = []
    for file in files:
        code, text, _ = await run_git(path, "show", f"{ref}:{file}", settings=settings, max_output=200_000)
        if code != 0:
            continue
        tag = _kustomize_image_tag(text, image_name)
        if tag:
            declared.append((file, tag, _kustomize_replicas(text)))
    if not declared:
        return None
    short = deployment.removeprefix("dify-")
    declared.sort(key=lambda item: (item[0].split("/")[-2] not in (deployment, short), item[0]))
    file, tag, replicas = declared[0]
    code, commit, _ = await run_git(path, "rev-parse", ref, settings=settings, max_output=200)
    replica_count = _declared_replicas(replicas, deployment, file)
    return {
        "image_tag": tag,
        "environment": environment,
        "scaled_to_zero": replica_count == 0,
        "gitops": {
            "repo": gitops_repo,
            "ref": ref,
            "commit": commit.strip() if code == 0 else None,
            "path": file,
            "image": image_name,
            "replicas": replica_count,
            "other_overlays": [{"path": f, "image_tag": t} for f, t, _ in declared[1:]],
        },
    }


def _kustomize_replicas(text: str) -> dict[str, int]:
    """name -> count from the top-level `replicas:` list of a kustomization.yaml."""
    result: dict[str, int] = {}
    in_block = False
    name: Optional[str] = None
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith((" ", "-")):
            in_block = line.split("#", 1)[0].strip() == "replicas:"
            name = None
            continue
        if not in_block:
            continue
        item = line.strip().removeprefix("- ").strip()
        key, _, value = item.partition(":")
        value = value.split("#", 1)[0].strip().strip("'\"")
        if key == "name":
            name = value
        elif key == "count" and name is not None:
            try:
                result[name] = int(value)
            except ValueError:
                pass
    return result


def _declared_replicas(replicas: dict[str, int], deployment: str, file: str) -> Optional[int]:
    """Replica count the overlay declares for the deployment, or None if unknown."""
    if not replicas:
        return None
    overlay = file.split("/")[-2] if "/" in file else ""
    for name in (deployment, deployment.removeprefix("dify-"), overlay):
        if name in replicas:
            return replicas[name]
    return replicas[next(iter(replicas))] if len(replicas) == 1 else None


def _kustomize_image_tag(text: str, image_name: str) -> Optional[str]:
    """newTag of the images[] entry named image_name in a kustomization.yaml."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = _KUSTOMIZE_IMAGE_NAME_RE.match(line)
        if not match or match.group(2) != image_name:
            continue
        item_indent = len(match.group(1))
        for follow in lines[index + 1:index + 8]:
            if not follow.strip():
                continue
            stripped = follow.lstrip()
            indent = len(follow) - len(stripped)
            if stripped.startswith("- ") or indent < item_indent or (indent == 0 and not follow.startswith(" ")):
                break
            tag = _KUSTOMIZE_NEW_TAG_RE.match(follow)
            if tag:
                return tag.group(1)
    return None


async def _first_runtime_image(
    deployment_names: list[str],
    environment: str,
    settings: Settings,
) -> tuple[str, Optional[dict[str, Any]]]:
    for candidate in deployment_names:
        image = await _query_runtime_image(candidate, settings, environment)
        if image:
            return candidate, image
    return "", None


def _runtime_deployment_candidates(
    *,
    repo: str,
    deployment: Optional[str],
    settings: Settings,
) -> list[str]:
    if deployment:
        return [deployment]
    return configured_deployments(settings, repo)


def configured_deployments(settings: Settings, repo: str) -> list[str]:
    configured = settings.runtime_deployments.get(repo)
    if not isinstance(configured, str):
        return []
    return list(
        dict.fromkeys(name.strip() for name in configured.split(",") if name.strip())
    )


def extract_base_version(tag: str) -> str:
    m = _BASE_VERSION_RE.search(tag or "")
    return m.group(1) if m else ""


def _is_any_environment(environment: Optional[str]) -> bool:
    return (environment or "").strip().lower() in (ANY_ENVIRONMENT, "*")


def _validate_environment(environment: str) -> None:
    if _is_any_environment(environment):
        return
    if not _ENVIRONMENT_RE.fullmatch(environment or ""):
        raise RuntimeRefUnavailable(f"invalid deployment environment: {environment!r}")


async def _query_runtime_image(
    deployment: str,
    settings: Settings,
    environment: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    environment = environment or settings.runtime_environment
    sql = _runtime_sql(deployment, environment)
    rows = await _signoz_query(sql, settings.runtime_hours, settings, f"deployment {deployment}")
    return _select_primary_image(rows)


async def _query_runtime_image_tag(deployment: str, settings: Settings) -> Optional[str]:
    image = await _query_runtime_image(deployment, settings)
    return image["image_tag"] if image else None


async def _signoz_query(
    sql: str,
    hours: int,
    settings: Settings,
    label: str,
) -> list[dict[str, Any]]:
    endpoint = _mcp_endpoint(settings.signoz_mcp_url)
    if not endpoint:
        raise _runtime_telemetry_error(label, "SigNoz MCP URL is not configured")
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "sz_run",
            "arguments": {"args": ["query", sql, "--hours", str(hours)]},
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
        raise _runtime_telemetry_error(label, f"SigNoz MCP returned HTTP {status_code}")
    try:
        data = _load_json_or_sse(text)
    except (json.JSONDecodeError, TypeError, ValueError) as e:
        raise _runtime_telemetry_error(label, "SigNoz MCP returned invalid JSON") from e
    raw = data.get("result", {}) if isinstance(data, dict) else {}
    content = raw.get("content") if isinstance(raw, dict) else None
    if not isinstance(content, list) or not content:
        raise _runtime_telemetry_error(label, "SigNoz MCP response has no content")
    first = content[0]
    if not isinstance(first, dict):
        raise _runtime_telemetry_error(label, "SigNoz MCP content is invalid")
    try:
        parsed = json.loads(first.get("text") or "{}")
    except json.JSONDecodeError:
        raise _runtime_telemetry_error(label, "SigNoz tool returned invalid JSON") from None
    if not isinstance(parsed, dict) or not parsed.get("success"):
        raise _runtime_telemetry_error(label, "SigNoz tool query failed")
    rows = parsed.get("data")
    if not isinstance(rows, list):
        raise _runtime_telemetry_error(label, "SigNoz tool returned invalid rows")
    return [row for row in rows if isinstance(row, dict)]


def _runtime_telemetry_error(label: str, message: str) -> SourceError:
    return SourceError("RUNTIME_TELEMETRY_FAILED", f"{message} for {label}")


def _runtime_sql(deployment: str, environment: str) -> str:
    if not _KUBERNETES_DEPLOYMENT_RE.fullmatch(deployment):
        raise RuntimeRefUnavailable(f"invalid Kubernetes deployment name: {deployment!r}")
    _validate_environment(environment)
    deployment_q = deployment.replace("'", "''")
    environment_clause = ""
    if not _is_any_environment(environment):
        environment_q = environment.replace("'", "''")
        environment_clause = f"AND resources_string['deployment.environment'] = '{environment_q}' "
    return (
        "SELECT "
        "resources_string['container.image.tag'] AS image_tag, "
        "resources_string['deployment.environment'] AS environment, "
        "any(resources_string['service.version']) AS service_version, "
        "count() AS log_count, "
        "max(timestamp) AS latest_ts "
        "FROM signoz_logs.distributed_logs_v2 "
        "WHERE timestamp >= {start_ns} "
        f"AND resources_string['k8s.deployment.name'] = '{deployment_q}' "
        f"{environment_clause}"
        "AND resources_string['container.image.tag'] != '' "
        "GROUP BY image_tag, environment "
        "ORDER BY latest_ts DESC, log_count DESC "
        "LIMIT 5"
    )


def _deployments_sql(
    *,
    name_contains: Optional[str],
    environment: Optional[str],
    limit: int,
) -> str:
    clauses = [
        "timestamp >= {start_ns}",
        "resources_string['k8s.deployment.name'] != ''",
        "resources_string['container.image.tag'] != ''",
    ]
    if name_contains:
        # Validated to [a-z0-9.-], so it cannot carry quotes or LIKE wildcards.
        clauses.append(f"resources_string['k8s.deployment.name'] LIKE '%{name_contains}%'")
    if environment and not _is_any_environment(environment):
        clauses.append(
            "resources_string['deployment.environment'] = "
            f"'{environment.replace(chr(39), chr(39) * 2)}'"
        )
    return (
        "SELECT "
        "resources_string['k8s.deployment.name'] AS deployment, "
        "resources_string['deployment.environment'] AS environment, "
        "resources_string['container.image.tag'] AS image_tag, "
        "count() AS log_count, "
        "max(timestamp) AS latest_ts "
        "FROM signoz_logs.distributed_logs_v2 "
        f"WHERE {' AND '.join(clauses)} "
        "GROUP BY deployment, environment, image_tag "
        "ORDER BY deployment ASC, environment ASC, latest_ts DESC "
        f"LIMIT {int(limit)}"
    )


async def list_runtime_deployments(
    *,
    settings: Settings,
    hours: Optional[int] = None,
    name_contains: Optional[str] = None,
    environment: Optional[str] = None,
    limit: int = 200,
) -> dict[str, Any]:
    hours = settings.runtime_hours if hours is None else hours
    if not 1 <= hours <= _MAX_RUNTIME_HOURS:
        raise SourceError("BAD_ARGUMENT", f"hours must be between 1 and {_MAX_RUNTIME_HOURS}")
    if not 1 <= limit <= _MAX_DEPLOYMENT_ROWS:
        raise SourceError("BAD_ARGUMENT", f"limit must be between 1 and {_MAX_DEPLOYMENT_ROWS}")
    if name_contains is not None:
        name_contains = name_contains.strip().lower()
        if not _NAME_FILTER_RE.fullmatch(name_contains):
            raise SourceError(
                "BAD_ARGUMENT",
                "name_contains may only contain lowercase letters, digits, '.' and '-' (max 63)",
            )
    if environment is not None and not _is_any_environment(environment):
        if not _ENVIRONMENT_RE.fullmatch(environment):
            raise SourceError("BAD_ARGUMENT", f"invalid environment: {environment!r}")
    sql = _deployments_sql(name_contains=name_contains, environment=environment, limit=limit + 1)
    rows = await _signoz_query(sql, hours, settings, "deployment listing")
    truncated = len(rows) > limit
    rows = rows[:limit]

    configured: dict[str, list[str]] = {}
    deployment_repos: dict[str, list[str]] = {}
    for repo in settings.runtime_deployments:
        names = configured_deployments(settings, repo)
        configured[repo] = names
        for name in names:
            deployment_repos.setdefault(name, []).append(repo)
    for repo, prefixes in settings.runtime_path_deployments.items():
        for name in prefixes.values():
            if repo not in deployment_repos.setdefault(name, []):
                deployment_repos[name].append(repo)
    # Default deployments only: component deployments (cronjobs, workers) may
    # legitimately log rarely, so they are mapped back to repos but not reported.
    expected = set(deployment_repos)
    for repo, names in settings.repo_component_deployments.items():
        for name in names:
            if repo not in deployment_repos.setdefault(name, []):
                deployment_repos[name].append(repo)

    items: list[dict[str, Any]] = []
    for row in rows:
        name = str(row.get("deployment") or "")
        latest = row.get("latest_ts")
        item = {
            "deployment": name,
            "environment": str(row.get("environment") or ""),
            "image_tag": str(row.get("image_tag") or ""),
            "log_count": _coerce_int(row.get("log_count")),
            "latest_ts": latest,
            "latest_at": _timestamp_to_iso(latest),
            "repos": deployment_repos.get(name, []),
        }
        items.append(item)
    seen = {item["deployment"] for item in items}
    # Only deployments the query could have returned: those matching name_contains.
    # When the listing was truncated, absence proves nothing.
    considered = [name for name in expected if not name_contains or name_contains in name]
    without_logs = None if truncated else sorted(name for name in considered if name not in seen)
    return {
        "hours": hours,
        "environment": environment or "any",
        "name_contains": name_contains,
        "deployments": items,
        "returned": len(items),
        "truncated": truncated,
        "configured_repo_deployments": configured,
        "configured_path_deployments": settings.runtime_path_deployments,
        "deployment_repos": deployment_repos,
        "configured_without_logs": without_logs,
    }


def _timestamp_to_iso(value: Any) -> Optional[str]:
    number = _coerce_float(value)
    if number <= 0:
        return None
    for divisor in (1e9, 1e6, 1e3, 1.0):
        seconds = number / divisor
        if seconds < 1e11:
            break
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return None


def _select_primary_image(rows: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    candidates: list[tuple[float, int, int, dict[str, Any]]] = []
    first_seen: Optional[dict[str, Any]] = None
    for index, row in enumerate(rows):
        tag = (row or {}).get("image_tag")
        if not tag:
            continue
        image = {"image_tag": str(tag), "environment": str(row.get("environment") or "")}
        first_seen = first_seen or image
        candidates.append((
            _coerce_float(row.get("latest_ts")),
            _coerce_int(row.get("log_count")),
            -index,
            image,
        ))
    if not candidates:
        return first_seen
    candidates.sort(key=lambda item: item[:3], reverse=True)
    return candidates[0][3]


def _select_primary_image_tag(rows: list[dict[str, Any]]) -> Optional[str]:
    image = _select_primary_image(rows)
    return image["image_tag"] if image else None


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


_PATHSPEC_META = "*?[\\"


def select_path_deployment(
    settings: Settings,
    repo: str,
    path: Optional[str],
    *,
    is_pattern: bool = False,
) -> dict[str, Any]:
    """Decide which runtime deployment a path (or pathspec glob) belongs to.

    Returns {"kind": "match", "deployment", "prefix"} when every path it can
    name lives under a configured prefix, {"kind": "spans", "prefixes": {...}}
    when it may cover both a prefix and the rest of the repo, else {"kind": "none"}.
    """
    prefixes = settings.runtime_path_deployments.get(repo) or {}
    if not prefixes:
        return {"kind": "none"}
    value = (path or "").strip()
    while value.startswith("./"):
        value = value[2:]
    if is_pattern and value.startswith(":"):
        literal, complete = "", False  # pathspec magic: treat as unknown scope
    elif is_pattern:
        cut = next((index for index, char in enumerate(value) if char in _PATHSPEC_META), None)
        literal = value if cut is None else value[:cut]
        complete = cut is None
    else:
        literal, complete = value, True

    matched: list[tuple[str, str]] = []
    spanned: dict[str, str] = {}
    for prefix, deployment in prefixes.items():
        norm = prefix.rstrip("/") + "/"
        if literal.startswith(norm) or (complete and literal and literal.rstrip("/") + "/" == norm):
            matched.append((norm, deployment))
        elif norm.startswith(literal) and (is_pattern or not literal):
            spanned[norm] = deployment
    if matched:
        prefix, deployment = max(matched, key=lambda item: len(item[0]))
        return {"kind": "match", "deployment": deployment, "prefix": prefix}
    if spanned:
        return {"kind": "spans", "prefixes": spanned}
    return {"kind": "none"}
