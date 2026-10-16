from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ZENDESK_KG_MCP_", extra="ignore")

    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1024, le=65535)
    api_key: SecretStr = SecretStr("")
    upstream_api_key: SecretStr = SecretStr("")
    timeout_seconds: float = Field(default=45, gt=0, le=120)
    max_response_bytes: int = Field(default=2 * 1024 * 1024, ge=1024, le=16 * 1024 * 1024)

    @property
    def configured(self) -> bool:
        return bool(self.api_key.get_secret_value() and self.upstream_api_key.get_secret_value())
