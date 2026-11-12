from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    github_host: str = Field(default="127.0.0.1", validation_alias="GITHUB_MCP_HOST")
    github_port: int = Field(default=8111, validation_alias="GITHUB_MCP_PORT")

    github_api_key: str = Field(default="", validation_alias="GITHUB_MCP_API_KEY")
    github_token: str = Field(default="", validation_alias="GITHUB_MCP_GITHUB_TOKEN")
    github_timeout_seconds: float = Field(default=30.0, validation_alias="GITHUB_MCP_TIMEOUT_SECONDS")

    # Write tools. All of these must be set (and the write key must differ from the
    # read key) before any write tool is listed or callable; see ``write_active``.
    github_write_api_key: str = Field(default="", validation_alias="GITHUB_MCP_WRITE_API_KEY")
    github_write_token: str = Field(default="", validation_alias="GITHUB_MCP_GITHUB_WRITE_TOKEN")
    github_write_enabled: bool = Field(default=False, validation_alias="GITHUB_MCP_WRITE_ENABLED")
    github_write_owners: str = Field(default="david-yang-pixel", validation_alias="GITHUB_MCP_WRITE_OWNERS")
    github_upstream_repos: str = Field(default="", validation_alias="GITHUB_MCP_UPSTREAM_REPOS")
    github_write_rate_per_hour: int = Field(default=30, validation_alias="GITHUB_MCP_WRITE_RATE_PER_HOUR")

    @property
    def write_active(self) -> bool:
        return (
            self.github_write_enabled
            and bool(self.github_write_token)
            and bool(self.github_write_api_key)
            and self.github_write_api_key != self.github_api_key
        )




@lru_cache
def get_settings() -> Settings:
    return Settings()
