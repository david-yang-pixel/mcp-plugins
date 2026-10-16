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
            '"dify-billing":"dify-saas-billing,billing"}'
        ),
        validation_alias="SOURCE_MCP_RUNTIME_DEPLOYMENTS",
    )
    runtime_ref_overrides_json: str = Field(
        default="{}",
        validation_alias="SOURCE_MCP_RUNTIME_REF_OVERRIDES",
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
