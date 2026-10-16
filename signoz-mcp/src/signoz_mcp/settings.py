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

    api_key: str = Field(default="", validation_alias="SIGNOZ_MCP_API_KEY")
    host: str = Field(default="127.0.0.1", validation_alias="SIGNOZ_MCP_HOST")
    port: int = Field(default=8098, validation_alias="SIGNOZ_MCP_PORT")
    sz_path: str = Field(default="sz", validation_alias="SIGNOZ_MCP_SZ_PATH")
    timeout_seconds: float = Field(default=35.0, validation_alias="SIGNOZ_MCP_TIMEOUT_SECONDS")
    max_concurrent_runs: int = Field(default=4, validation_alias="SIGNOZ_MCP_MAX_CONCURRENT_RUNS")
    cache_ttl_seconds: float = Field(default=300.0, validation_alias="SIGNOZ_MCP_CACHE_TTL_SECONDS")
    cache_max_entries: int = Field(default=128, validation_alias="SIGNOZ_MCP_CACHE_MAX_ENTRIES")
    cacheable_commands: str = Field(
        default="services,schema",
        validation_alias="SIGNOZ_MCP_CACHEABLE_COMMANDS",
    )
    singleflight_enabled: bool = Field(
        default=True,
        validation_alias="SIGNOZ_MCP_SINGLEFLIGHT_ENABLED",
    )
    signoz_url: str = Field(default="", validation_alias="SIGNOZ_URL")
    signoz_api_key: str = Field(default="", validation_alias="SIGNOZ_API_KEY")

    @property
    def cacheable_command_set(self) -> set[str]:
        return {
            command.strip()
            for command in self.cacheable_commands.split(",")
            if command.strip()
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()
