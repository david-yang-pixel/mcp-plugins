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

    docs_host: str = Field(default="127.0.0.1", validation_alias="DOCS_TOOL_SERVICE_HOST")
    docs_port: int = Field(default=8112, validation_alias="DOCS_TOOL_SERVICE_PORT")


    docs_api_key: str = Field(default="", validation_alias="DOCS_TOOL_SERVICE_API_KEY")
    docs_timeout_seconds: float = Field(default=20.0, validation_alias="DOCS_TOOL_SERVICE_TIMEOUT_SECONDS")
    docs_cache_ttl_seconds: float = Field(default=900.0, validation_alias="DOCS_TOOL_SERVICE_CACHE_TTL_SECONDS")




@lru_cache
def get_settings() -> Settings:
    return Settings()
