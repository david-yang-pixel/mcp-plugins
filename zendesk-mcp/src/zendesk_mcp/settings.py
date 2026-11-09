from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class Settings:
    api_key: str
    host: str
    port: int
    workers: int
    audit_log: str
    allowed_custom_field_ids: str
    request_timeout_seconds: float
    connect_timeout_seconds: float
    read_timeout_seconds: float
    max_retries: int
    cache_ttl_seconds: float
    cache_max_entries: int
    download_max_bytes: int
    zendesk_subdomain: str
    zendesk_email: str
    zendesk_api_token: str
    zendesk_auth_mode: str = "api_token"
    zendesk_oauth_file: str = ""
    zendesk_oauth_seed_file: str = ""
    zendesk_oauth_expected_user_id: str = ""
    zendesk_oauth_keepalive_hours: float = 24.0

    @property
    def uses_oauth(self) -> bool:
        return self.zendesk_auth_mode == "oauth"

    @property
    def zendesk_base_url(self) -> str:
        return f"https://{self.zendesk_subdomain}.zendesk.com/api/v2"

    @property
    def custom_field_id_set(self) -> set[str]:
        return {
            item.strip()
            for item in self.allowed_custom_field_ids.split(",")
            if item.strip()
        }


@lru_cache
def get_settings() -> Settings:
    _load_dotenv(".env")
    return Settings(
        api_key=os.getenv("ZENDESK_MCP_API_KEY", ""),
        host=os.getenv("ZENDESK_MCP_HOST", "127.0.0.1"),
        port=_int_env("ZENDESK_MCP_PORT", 8100),
        workers=max(1, _int_env("ZENDESK_MCP_WORKERS", 2)),
        audit_log=os.getenv("ZENDESK_MCP_AUDIT_LOG", "./logs/audit.jsonl"),
        allowed_custom_field_ids=os.getenv("ZENDESK_MCP_ALLOWED_CUSTOM_FIELD_IDS", ""),
        request_timeout_seconds=_float_env("ZENDESK_MCP_REQUEST_TIMEOUT_SECONDS", 30.0),
        connect_timeout_seconds=_float_env("ZENDESK_MCP_CONNECT_TIMEOUT_SECONDS", 3.0),
        read_timeout_seconds=_float_env(
            "ZENDESK_MCP_READ_TIMEOUT_SECONDS",
            _float_env("ZENDESK_MCP_REQUEST_TIMEOUT_SECONDS", 20.0),
        ),
        max_retries=max(0, _int_env("ZENDESK_MCP_MAX_RETRIES", 1)),
        cache_ttl_seconds=max(0.0, _float_env("ZENDESK_MCP_CACHE_TTL_SECONDS", 300.0)),
        cache_max_entries=max(0, _int_env("ZENDESK_MCP_CACHE_MAX_ENTRIES", 256)),
        download_max_bytes=max(1, _int_env("ZENDESK_MCP_DOWNLOAD_MAX_BYTES", 10 * 1024 * 1024)),
        zendesk_subdomain=os.getenv("ZENDESK_SUBDOMAIN", ""),
        zendesk_email=os.getenv("ZENDESK_EMAIL", ""),
        zendesk_api_token=os.getenv("ZENDESK_API_TOKEN", ""),
        zendesk_auth_mode=os.getenv("ZENDESK_AUTH_MODE", "api_token").strip().lower() or "api_token",
        zendesk_oauth_file=os.getenv("ZENDESK_OAUTH_FILE", ""),
        zendesk_oauth_seed_file=os.getenv("ZENDESK_OAUTH_SEED_FILE", ""),
        zendesk_oauth_expected_user_id=os.getenv("ZENDESK_OAUTH_EXPECTED_USER_ID", ""),
        zendesk_oauth_keepalive_hours=max(0.0, _float_env("ZENDESK_OAUTH_KEEPALIVE_HOURS", 24.0)),
    )


def _load_dotenv(path: str) -> None:
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default
