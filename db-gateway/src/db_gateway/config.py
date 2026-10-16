"""Configuration for the standalone DB Gateway service."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ResourcesConfig(BaseModel):
    base_dir: str = "/opt/zd-agent/resources"
    state_dir: str = ".state"


class PostgresConfig(BaseModel):
    host: str = "localhost"
    port: int = 56061
    user: str = "pro_ops_teleport_read"
    password: str = ""
    db_prod: str = "langgenius"
    db_billing: str = "billing"
    db_plugin: str = "dify_plugin_daemon"
    port_prod: int = 56061
    port_billing: int = 56062
    port_plugin: int = 56063
    query_timeout_seconds: int = 30
    max_rows: int = 1000
    max_concurrent_queries: int = 3
    teleport_retry_attempts: int = 1
    teleport_retry_delay_seconds: float = 0.25
    singleflight_enabled: bool = True


class DbGatewayConfig(BaseModel):
    api_key: str = ""
    listen_host: str = "127.0.0.1"
    listen_port: int = 18081
    request_timeout_seconds: int = 35


class TeleportConfig(BaseModel):
    proxy: str = ""
    user: str = ""
    public_base_url: str = ""
    auth_home: str = "/opt/zd-agent/.tsh"
    warn_threshold_minutes: int = 30
    check_interval_seconds: int = 300
    notify_interval_seconds: int = 3600


class FeishuConfig(BaseModel):
    webhook_url: str = ""
    webhook_secret: str = ""


class AppConfig(BaseModel):
    resources: ResourcesConfig = Field(default_factory=ResourcesConfig)
    postgres: PostgresConfig = Field(default_factory=PostgresConfig)
    db_gateway: DbGatewayConfig = Field(default_factory=DbGatewayConfig)
    teleport: TeleportConfig = Field(default_factory=TeleportConfig)
    feishu: FeishuConfig = Field(default_factory=FeishuConfig)


class EnvSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    pg_password: str = ""
    db_gateway_api_key: str = ""
    db_gateway_listen_host: str | None = None
    db_gateway_listen_port: int | None = None
    teleport_user: str = ""
    teleport_public_base_url: str = ""
    teleport_password: str = ""
    teleport_totp_secret: str = ""
    feishu_webhook_url: str = ""
    feishu_webhook_secret: str = ""


def load_config(config_path: str = "config.yaml") -> AppConfig:
    path = Path(config_path).resolve()
    if path.exists():
        with path.open(encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        config_dir = path.parent
    else:
        raw = {}
        config_dir = Path.cwd()

    cfg = AppConfig(**raw)

    def _resolve(p: str) -> str:
        pp = Path(p)
        if not pp.is_absolute():
            pp = (config_dir / pp).resolve()
        return str(pp)

    cfg.resources.base_dir = _resolve(cfg.resources.base_dir)

    env = EnvSettings()
    if env.pg_password:
        cfg.postgres.password = env.pg_password
    if env.db_gateway_api_key:
        cfg.db_gateway.api_key = env.db_gateway_api_key
    if env.db_gateway_listen_host is not None:
        cfg.db_gateway.listen_host = env.db_gateway_listen_host
    if env.db_gateway_listen_port is not None:
        cfg.db_gateway.listen_port = env.db_gateway_listen_port
    if env.teleport_user:
        cfg.teleport.user = env.teleport_user
    if env.teleport_public_base_url:
        cfg.teleport.public_base_url = env.teleport_public_base_url.rstrip("/")
    if env.feishu_webhook_url:
        cfg.feishu.webhook_url = env.feishu_webhook_url
    if env.feishu_webhook_secret:
        cfg.feishu.webhook_secret = env.feishu_webhook_secret
    return cfg


_config: AppConfig | None = None


def get_config() -> AppConfig:
    global _config
    if _config is None:
        _config = load_config()
    return _config
