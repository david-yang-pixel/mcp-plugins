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





@lru_cache
def get_settings() -> Settings:
    return Settings()
