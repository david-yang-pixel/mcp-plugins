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

    notification_host: str = Field(default="127.0.0.1", validation_alias="NOTIFICATION_SERVICE_HOST")
    notification_port: int = Field(default=8113, validation_alias="NOTIFICATION_SERVICE_PORT")



    notification_api_key: str = Field(default="", validation_alias="NOTIFICATION_SERVICE_API_KEY")
    notification_timeout_seconds: float = Field(default=20.0, validation_alias="NOTIFICATION_SERVICE_TIMEOUT_SECONDS")
    feishu_webhook_url: str = Field(default="", validation_alias="FEISHU_WEBHOOK_URL")
    feishu_webhook_secret: str = Field(default="", validation_alias="FEISHU_WEBHOOK_SECRET")



@lru_cache
def get_settings() -> Settings:
    return Settings()
