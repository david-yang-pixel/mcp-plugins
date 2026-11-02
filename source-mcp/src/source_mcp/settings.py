from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    api_key: str = Field(default="", validation_alias="SOURCE_MCP_API_KEY")
    host: str = Field(default="127.0.0.1", validation_alias="SOURCE_MCP_HOST")
    port: int = Field(default=8099, validation_alias="SOURCE_MCP_PORT")

    repos_base_dir: str = Field(
        default="/opt/zd-agent/resources/saas源代码",
        validation_alias="SOURCE_MCP_REPOS_BASE_DIR",
    )
    repos: str = Field(
        default=(
            "dify,dify-billing,dify-marketplace-backend,"
            "dify-marketplace-frontend,dify-plugin-daemon,saas-deploy"
        ),
        validation_alias="SOURCE_MCP_REPOS",
    )
    repo_urls_json: str = Field(default="", validation_alias="SOURCE_MCP_REPO_URLS")
    github_token: str = Field(default="", validation_alias="SOURCE_MCP_GITHUB_TOKEN")
    fetch_ttl_seconds: int = Field(default=900, validation_alias="SOURCE_MCP_FETCH_TTL_SECONDS")
    git_timeout_seconds: float = Field(default=60.0, validation_alias="SOURCE_MCP_GIT_TIMEOUT_SECONDS")
    git_history_timeout_seconds: float = Field(
        default=900.0,
        validation_alias="SOURCE_MCP_GIT_HISTORY_TIMEOUT_SECONDS",
    )
    read_max_file_bytes: int = Field(
        default=20_000_000,
        ge=120_000,
        validation_alias="SOURCE_MCP_READ_MAX_FILE_BYTES",
    )
    full_repo_search_concurrency: int = Field(
        default=1,
        ge=1,
        le=4,
        validation_alias="SOURCE_MCP_FULL_REPO_SEARCH_CONCURRENCY",
    )
    process_zombie_warning_threshold: int = Field(
        default=10,
        validation_alias="SOURCE_MCP_PROCESS_ZOMBIE_WARNING_THRESHOLD",
    )
    process_pids_warning_ratio: float = Field(
        default=0.8,
        validation_alias="SOURCE_MCP_PROCESS_PIDS_WARNING_RATIO",
    )

    signoz_mcp_url: str = Field(default="", validation_alias="SOURCE_MCP_SIGNOZ_MCP_URL")
    signoz_mcp_api_key: str = Field(default="", validation_alias="SOURCE_MCP_SIGNOZ_MCP_API_KEY")
    signoz_mcp_timeout_seconds: float = Field(
        default=35.0,
        validation_alias="SOURCE_MCP_SIGNOZ_MCP_TIMEOUT_SECONDS",
    )
    runtime_environment: str = Field(default="production", validation_alias="SOURCE_MCP_RUNTIME_ENVIRONMENT")
    runtime_hours: int = Field(default=3, validation_alias="SOURCE_MCP_RUNTIME_HOURS")
    runtime_deployments_json: str = Field(
        default=(
            '{"dify":"dify-api-oltp",'
            '"dify-plugin-daemon":"dify-plugindaemon,dify-plugin-daemon",'
            '"dify-billing":"dify-billing,billing",'
            '"dify-sandbox":"dify-sandbox",'
            '"dify-marketplace":"dify-marketplace-backend,dify-marketplace-ops-backend",'
            '"rbac":"dify-rbac,rbac"}'
        ),
        validation_alias="SOURCE_MCP_RUNTIME_DEPLOYMENTS",
    )
    # repo -> {path prefix -> deployment}: paths under a prefix run in a different
    # deployment (and possibly a different version) than the repo's default one.
    runtime_path_deployments_json: str = Field(
        default='{"dify":{"web/":"dify-web"}}',
        validation_alias="SOURCE_MCP_RUNTIME_PATH_DEPLOYMENTS",
    )
    # repo -> {deployment or "*" -> tag prefix}: monorepos tag each component as
    # "<prefix><version>" while the image tag is the bare version (dify-marketplace
    # images are 2.3.2, git tags backend/2.3.2, ops/2.3.2, ...).
    runtime_tag_prefixes_json: str = Field(
        default=json.dumps(
            {
                "dify-marketplace": {
                    "dify-marketplace-backend": "backend/",
                    "dify-marketplace-scheduler": "backend/",
                    "dify-marketplace-ops-backend": "ops/",
                    "dify-marketplace-ops-web": "ops/",
                    "dify-marketplace-web": "frontend/",
                    "dify-marketplace-creators-backend": "creators/",
                    "dify-marketplace-creators-web": "creators/",
                },
            },
            separators=(",", ":"),
        ),
        validation_alias="SOURCE_MCP_RUNTIME_TAG_PREFIXES",
    )
    # When telemetry has no image tag for a deployment (no logs shipped, e.g.
    # dify-sandbox, or no production logs, e.g. dify-rbac), fall back to the tag
    # declared in this GitOps repo's environments/<env>/**/kustomization.yaml.
    gitops_repo: str = Field(default="saas-deploy", validation_alias="SOURCE_MCP_GITOPS_REPO")
    # deployment -> image name looked up in the GitOps kustomizations.
    gitops_images_json: str = Field(
        default=json.dumps(
            {
                "dify-rbac": "langgenius/rbac",
                "rbac": "langgenius/rbac",
                "dify-sandbox": "langgenius/dify-sandbox",
                "dify-billing": "langgenius/dify-billing",
                "billing": "langgenius/dify-billing",
            },
            separators=(",", ":"),
        ),
        validation_alias="SOURCE_MCP_GITOPS_IMAGES",
    )
    runtime_ref_overrides_json: str = Field(
        default="{}",
        validation_alias="SOURCE_MCP_RUNTIME_REF_OVERRIDES",
    )
    # Comma-separated repos that should keep full git history; "*" means all repos.
    full_history_repos: str = Field(default="*", validation_alias="SOURCE_MCP_FULL_HISTORY_REPOS")
    # Interval of the background fetch loop; <= 0 disables it.
    background_refresh_seconds: int = Field(
        default=3600,
        validation_alias="SOURCE_MCP_BACKGROUND_REFRESH_SECONDS",
    )
    # Name -> hint (string, or list of {"repo": ..., "paths": [...]}) shown when an
    # unconfigured repo name is requested, e.g. modules that live inside another repo.
    repo_hints_json: str = Field(
        default=json.dumps(
            {
                "billing": [{"repo": "dify-billing", "paths": []}],
                "dify-saas-billing": (
                    "the SaaS billing service (deployment dify-saas-billing, image "
                    "langgenius/dify-saas-billing, tags like release-20261030.2) is not built "
                    "from dify-billing (deployment dify-billing, tags release-202606xx); its "
                    "source repository has not been identified, so it is not configured"
                ),
                "ops": [{"repo": "dify-marketplace", "paths": ["ops/"]}],
                "ops-backend": [{"repo": "dify-marketplace", "paths": ["ops/"]}],
                "ops-web": [{"repo": "dify-marketplace", "paths": ["ops/"]}],
                "gateway": [
                    {
                        "repo": "dify-enterprise",
                        "paths": ["server/cmd/gateway/", "server/pkg/gateway/"],
                        "note": (
                            "unconfirmed: the SaaS image langgenius/dify-gateway is most "
                            "likely built from here"
                        ),
                    },
                ],
                "sandbox-gateway": [
                    {"repo": "dify-enterprise", "paths": ["server/cmd/sandbox-gateway/"]},
                ],
            },
            separators=(",", ":"),
        ),
        validation_alias="SOURCE_MCP_REPO_HINTS",
    )

    @property
    def repo_names(self) -> list[str]:
        return [part.strip() for part in self.repos.split(",") if part.strip()]

    @property
    def repo_urls(self) -> dict[str, str]:
        return _load_json_object(self.repo_urls_json)

    @property
    def runtime_deployments(self) -> dict[str, str]:
        return _load_json_object(self.runtime_deployments_json)

    @property
    def runtime_ref_overrides(self) -> dict[str, str]:
        return _load_json_object(self.runtime_ref_overrides_json)

    @property
    def runtime_path_deployments(self) -> dict[str, dict[str, str]]:
        result: dict[str, dict[str, str]] = {}
        for repo, mapping in _load_json_object(self.runtime_path_deployments_json).items():
            if not isinstance(mapping, dict):
                continue
            prefixes = {
                str(prefix).lstrip("/"): str(deployment).strip()
                for prefix, deployment in mapping.items()
                if str(prefix).strip("/") and isinstance(deployment, str) and deployment.strip()
            }
            if prefixes:
                result[str(repo)] = prefixes
        return result

    @property
    def runtime_tag_prefixes(self) -> dict[str, dict[str, str]]:
        result: dict[str, dict[str, str]] = {}
        for repo, mapping in _load_json_object(self.runtime_tag_prefixes_json).items():
            if isinstance(mapping, dict):
                result[str(repo)] = {
                    str(name): str(prefix)
                    for name, prefix in mapping.items()
                    if isinstance(prefix, str) and prefix
                }
        return result

    @property
    def gitops_images(self) -> dict[str, str]:
        return {
            str(name): str(image)
            for name, image in _load_json_object(self.gitops_images_json).items()
            if isinstance(image, str) and image
        }

    @property
    def repo_hints(self) -> dict[str, Any]:
        return _load_json_object(self.repo_hints_json)

    def is_full_history_repo(self, repo: str) -> bool:
        names = {part.strip() for part in self.full_history_repos.split(",") if part.strip()}
        return "*" in names or repo in names


def _load_json_object(raw: str) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


@lru_cache
def get_settings() -> Settings:
    return Settings()
