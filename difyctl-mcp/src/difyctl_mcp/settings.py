"""Runtime configuration read from the process environment.

Required values have no defaults: the service refuses OAuth until they are set.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

LEVELS = {"read": 0, "write": 1, "destructive": 2}
SCOPES = {"read": "difyctl:read", "write": "difyctl:write", "destructive": "difyctl:destructive"}


def _list(value: str) -> list[str]:
    return [item for item in re.split(r"[\s,]+", value or "") if item]


def _int(env, key: str, default: int, low: int, high: int) -> int:
    raw = env.get(key, "")
    if raw == "":
        return default
    value = int(raw)
    if not low <= value <= high:
        raise ValueError(f"{key} must be between {low} and {high}")
    return value


@dataclass
class Settings:
    public_url: str = ""
    allowed_emails: list[str] = field(default_factory=list)
    allowed_redirect_uris: list[str] = field(default_factory=list)
    api_key: str = ""
    host: str = "127.0.0.1"
    port: int = 8000
    data_dir: Path = Path("/var/lib/mcp/difyctl")
    difyctl_bin: str = "/usr/local/bin/difyctl"
    skills_dir: Path = Path("/opt/difyctl/skills")
    dify_server: str = "https://cloud.dify.ai"
    dify_version: str = "1.18.0"
    static_client_id: str = ""
    static_client_secret: str = ""
    max_effect: str = "destructive"
    timeout_seconds: int = 90
    max_timeout_seconds: int = 600
    max_output_chars: int = 100_000
    max_file_bytes: int = 25 * 1024 * 1024
    file_retention_seconds: int = 24 * 3600
    descriptor_ttl_seconds: int = 300
    pending_ttl_seconds: int = 15 * 60
    poll_interval_seconds: int = 5
    access_token_ttl_seconds: int = 3600
    code_ttl_seconds: int = 300
    session_fallback_seconds: int = 14 * 24 * 3600
    max_clients: int = 1000

    @classmethod
    def from_env(cls, env=None) -> "Settings":
        env = os.environ if env is None else env
        settings = cls(
            public_url=env.get("MCP_PUBLIC_URL", "").strip().rstrip("/"),
            allowed_emails=[e.lower() for e in _list(env.get("MCP_ALLOWED_EMAILS", ""))],
            allowed_redirect_uris=_list(env.get("MCP_ALLOWED_REDIRECT_URIS", "")),
            api_key=env.get("DIFYCTL_MCP_API_KEY", ""),
            host=env.get("DIFYCTL_MCP_HOST", "127.0.0.1"),
            port=_int(env, "DIFYCTL_MCP_PORT", 8000, 1, 65535),
            data_dir=Path(env.get("DIFYCTL_MCP_DATA_DIR", "/var/lib/mcp/difyctl")),
            difyctl_bin=env.get("DIFYCTL_BIN", "/usr/local/bin/difyctl"),
            skills_dir=Path(env.get("DIFYCTL_SKILLS_DIR", "/opt/difyctl/skills")),
            dify_server=env.get("DIFY_SERVER", "https://cloud.dify.ai").strip().rstrip("/"),
            dify_version=env.get("DIFY_VERSION", "1.18.0"),
            static_client_id=env.get("MCP_STATIC_CLIENT_ID", ""),
            static_client_secret=env.get("MCP_STATIC_CLIENT_SECRET", ""),
            max_effect=env.get("DIFYCTL_MAX_EFFECT", "destructive") or "destructive",
            timeout_seconds=_int(env, "DIFYCTL_TIMEOUT_SECONDS", 90, 1, 3600),
            max_timeout_seconds=_int(env, "DIFYCTL_MAX_TIMEOUT_SECONDS", 600, 1, 3600),
            max_output_chars=_int(env, "DIFYCTL_MAX_OUTPUT_CHARS", 100_000, 1_000, 10_000_000),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.max_effect not in LEVELS:
            raise ValueError("DIFYCTL_MAX_EFFECT must be read, write or destructive")
        if self.timeout_seconds > self.max_timeout_seconds:
            raise ValueError("DIFYCTL_TIMEOUT_SECONDS exceeds DIFYCTL_MAX_TIMEOUT_SECONDS")
        server = urlsplit(self.dify_server)
        if server.scheme not in ("https", "http") or not server.netloc or server.query or server.fragment:
            raise ValueError("DIFY_SERVER must be an http(s) base URL")
        if self.public_url:
            public = urlsplit(self.public_url)
            loopback = public.hostname in ("127.0.0.1", "localhost", "::1")
            if (public.scheme != "https" and not loopback) or not public.netloc or public.query or public.fragment:
                raise ValueError("MCP_PUBLIC_URL must be an https URL without query or fragment")
        if self.static_client_secret and not self.static_client_id:
            raise ValueError("MCP_STATIC_CLIENT_SECRET requires MCP_STATIC_CLIENT_ID")

    @property
    def configured(self) -> bool:
        """OAuth stays closed (default deny) until every required value is present."""
        return bool(self.public_url and self.allowed_emails and self.allowed_redirect_uris and self.api_key)

    @property
    def issuer(self) -> str:
        return self.public_url

    @property
    def resource(self) -> str:
        return self.public_url + "/mcp"

    @property
    def base_path(self) -> str:
        return urlsplit(self.public_url).path.rstrip("/")

    @property
    def public_host(self) -> str:
        return urlsplit(self.public_url).netloc

    @property
    def secure_cookies(self) -> bool:
        return urlsplit(self.public_url).scheme == "https"

    @property
    def max_level(self) -> int:
        return LEVELS[self.max_effect]

    @property
    def scopes(self) -> list[str]:
        """Scopes this deployment grants, capped by DIFYCTL_MAX_EFFECT."""
        return [SCOPES[name] for name, level in LEVELS.items() if level <= self.max_level]

    @property
    def sessions_dir(self) -> Path:
        return self.data_dir / "sessions"

    @property
    def users_dir(self) -> Path:
        return self.data_dir / "users"

    @property
    def home_dir(self) -> Path:
        return self.data_dir / "home"

    @property
    def database(self) -> Path:
        return self.data_dir / "auth.db"
