from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    relay_api_key: SecretStr = Field(..., min_length=16)
    codex_completion_api_key: Optional[SecretStr] = Field(default=None, min_length=16)
    dify_result_workflow_url: Optional[str] = None
    dify_result_workflow_api_key: Optional[SecretStr] = Field(
        default=None, min_length=16
    )
    dify_result_workflow_user: str = "zendesk-ticket-actor"
    codex_runner_create_url: Optional[str] = None
    codex_runner_api_key: Optional[SecretStr] = Field(default=None, min_length=16)
    zendesk_mcp_url: Optional[str] = None
    zendesk_mcp_authorization: Optional[SecretStr] = Field(
        default=None, min_length=16
    )
    zendesk_mcp_authorization_file: Path = Path(
        "/run/mcp-secrets/zendesk_mcp_authorization"
    )
    zendesk_subdomain: str = "langgenius"
    zendesk_profile_dir: Path = Path("/opt/zendesk-browser-relay/profile")
    ticket_run_db_path: Path = Path(
        "/var/lib/zendesk-browser-relay/state/ticket-runs.sqlite3"
    )
    ticket_run_active_lease_seconds: int = Field(
        default=1_800, ge=60, le=7_200
    )
    ticket_run_coalesce_active_messages: bool = False
    ticket_automation_allowlist: str = ""
    feishu_notification_webhook_file: Path = Path(
        "/var/lib/zendesk-browser-relay/state/feishu_webhook_url"
    )
    zendesk_headless: bool = True
    zendesk_proxy_server: Optional[str] = None
    zendesk_navigation_timeout_ms: int = Field(default=60_000, ge=5_000, le=180_000)
    zendesk_composer_selector: Optional[str] = None
    zendesk_send_selector: Optional[str] = None
    zendesk_login_email: Optional[str] = None
    zendesk_login_password: Optional[SecretStr] = None
    zendesk_totp_secret: Optional[SecretStr] = None
    zendesk_session_supervisor_enabled: bool = True
    zendesk_session_check_interval_seconds: int = Field(default=300, ge=30, le=3_600)
    zendesk_browser_operation_timeout_seconds: int = Field(
        default=240,
        ge=30,
        le=900,
    )
    zendesk_context_close_timeout_seconds: int = Field(default=20, ge=5, le=120)
    zendesk_readiness_navigation_timeout_ms: int = Field(
        default=12_000,
        ge=3_000,
        le=60_000,
    )
    zendesk_browser_memory_limit_mb: int = Field(default=1_400, ge=0, le=16_384)
    zendesk_browser_memory_check_interval_seconds: int = Field(
        default=60,
        ge=15,
        le=600,
    )
    zendesk_auto_login_initial_backoff_seconds: int = Field(
        default=30,
        ge=5,
        le=3_600,
    )
    zendesk_auto_login_attempt_timeout_seconds: int = Field(
        default=120,
        ge=15,
        le=300,
    )
    zendesk_auto_login_max_backoff_seconds: int = Field(
        default=900,
        ge=30,
        le=7_200,
    )

    @field_validator("zendesk_subdomain")
    @classmethod
    def normalize_subdomain(cls, value: str) -> str:
        value = value.strip()
        if value.startswith("https://"):
            value = value.removeprefix("https://")
        if value.endswith(".zendesk.com"):
            value = value.removesuffix(".zendesk.com")
        return value.strip("/")

    @property
    def zendesk_base_url(self) -> str:
        return f"https://{self.zendesk_subdomain}.zendesk.com"

    @property
    def api_key_value(self) -> str:
        return self.relay_api_key.get_secret_value()

    @property
    def codex_completion_api_key_value(self) -> Optional[str]:
        return (
            self.codex_completion_api_key.get_secret_value()
            if self.codex_completion_api_key
            else None
        )

    @property
    def dify_result_workflow_api_key_value(self) -> Optional[str]:
        return (
            self.dify_result_workflow_api_key.get_secret_value()
            if self.dify_result_workflow_api_key
            else None
        )

    @property
    def codex_runner_api_key_value(self) -> Optional[str]:
        return (
            self.codex_runner_api_key.get_secret_value()
            if self.codex_runner_api_key
            else None
        )

    @property
    def zendesk_mcp_authorization_value(self) -> Optional[str]:
        try:
            raw = self.zendesk_mcp_authorization_file.read_text(encoding="utf-8")
        except FileNotFoundError:
            raw = None
            problem = "missing"
        except (OSError, UnicodeError):
            raw = None
            problem = "unreadable"
        else:
            raw = raw.strip()
            problem = None if raw else "blank"
        if problem == "blank":
            raise ValueError("Zendesk MCP authorization file is blank")
        if raw:
            return raw
        # A configured MCP URL must not silently skip cursor checks when the
        # mounted secret is missing or unreadable. Env is only a fallback
        # when freshness is not configured.
        if self.zendesk_mcp_url and problem in {"missing", "unreadable"}:
            raise ValueError("Zendesk MCP authorization file is " + problem)
        if self.zendesk_mcp_authorization is None:
            return None
        value = self.zendesk_mcp_authorization.get_secret_value()
        stripped = value.strip()
        if value and not stripped:
            raise ValueError("Zendesk MCP authorization is blank")
        return stripped or None

    @property
    def ticket_automation_allowlist_values(self) -> frozenset[int]:
        values: set[int] = set()
        for raw in self.ticket_automation_allowlist.split(","):
            item = raw.strip()
            if not item:
                continue
            value = int(item)
            if value <= 0:
                raise ValueError("ticket automation allowlist IDs must be positive")
            values.add(value)
        return frozenset(values)

    @property
    def feishu_notification_webhook_url(self) -> Optional[str]:
        try:
            value = self.feishu_notification_webhook_file.read_text(encoding="utf-8").strip()
        except (FileNotFoundError, OSError):
            return None
        return value or None

    @property
    def login_password_value(self) -> Optional[str]:
        return (
            self.zendesk_login_password.get_secret_value()
            if self.zendesk_login_password
            else None
        )

    @property
    def totp_secret_value(self) -> Optional[str]:
        return (
            self.zendesk_totp_secret.get_secret_value()
            if self.zendesk_totp_secret
            else None
        )

    @property
    def auto_login_configured(self) -> bool:
        return bool(self.zendesk_login_email and self.login_password_value)


@lru_cache
def get_settings() -> Settings:
    return Settings()
