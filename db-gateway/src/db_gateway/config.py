"""Configuration for the standalone DB Gateway service."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field
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
    # KnowledgeFS production (Teleport db knowledge-fs-production-readonly). Its tunnel
    # is run by db-gateway itself (tsh_daemons.KNOWLEDGEFS_TUNNEL), not a tsh-proxy container.
    db_knowledgefs: str = "knowledgefs"
    port_knowledgefs: int = 56064
    query_timeout_seconds: int = 30
    max_rows: int = 1000
    max_concurrent_queries: int = 3
    teleport_retry_attempts: int = 1
    teleport_retry_delay_seconds: float = 0.25
    singleflight_enabled: bool = True
    # total_rows 计数预算（秒）：在同一游标上 MOVE ALL，超时则 total_rows=null；0 关闭计数。
    count_timeout_seconds: float = 5
    # psql：每次查询启动 psql 并新建连接（经 Teleport 隧道约 2s）；
    # pool：进程内 psycopg 连接池，复用连接（见 pg_pool.py）。
    driver: Literal["psql", "pool"] = "psql"
    pool_min_size: int = 1
    # 空闲连接回收时间；需短于 Teleport/网络对空闲连接的掐断时间。
    pool_max_idle_seconds: float = 240
    pool_max_lifetime_seconds: float = 1800
    # 空闲超过该秒数的连接在交给查询前先探测一次（一次往返）。
    pool_check_idle_seconds: float = 60
    # 等待/新建连接的上限，超时按 TELEPORT_EXPIRED 处理并重试。
    pool_acquire_timeout_seconds: float = 15
    # pool 模式连接失败后的重试间隔，需覆盖 tsh-proxy-db 重启的几秒。
    pool_retry_delay_seconds: float = 1.5
    # 监视 <teleport.auth_home>/keys，证书续签（隧道会被重启）后清空连接池。
    pool_watch_teleport_keys: bool = True


class DbGatewayConfig(BaseModel):
    # Unknown keys are ignored (pydantic default), so old config.yaml files that
    # still carry the retired require_ticket_id / missing_ticket_alert_interval_seconds
    # keep loading.
    model_config = ConfigDict(extra="ignore")

    api_key: str = ""
    listen_host: str = "127.0.0.1"
    listen_port: int = 18081
    request_timeout_seconds: int = 35
    # 审计日志目录（文件名固定为 db_gateway_audit.log）；为空时使用
    # <resources.base_dir>/<resources.state_dir>/logs。相对路径相对 config.yaml 所在目录。
    audit_log_dir: str = ""


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


class LambdaLogsConfig(BaseModel):
    enabled: bool = True
    app: str = "aws-support-logs"
    aws_role: str = "SupportLambdaLogsReadOnly"
    region: str = "us-east-1"
    default_timezone: str = "Asia/Shanghai"
    default_limit: int = 1000
    max_limit: int = 10000


class KubeReadConfig(BaseModel):
    enabled: bool = True
    # alias -> Teleport kube cluster name; the context is matched in the kubeconfig at runtime.
    clusters: dict[str, str] = Field(
        default_factory=lambda: {"prod": "lg-eks-01", "staging": "dify-plugin-1"}
    )
    max_output_bytes: int = 1048576
    kubeconfig_path: str = "/tmp/db-gateway/kubeconfig"


class KnowledgeFsConfig(BaseModel):
    enabled: bool = True
    teleport_db: str = "knowledge-fs-production-readonly"
    # The tunnel and the queries both log in as postgres.user.


class TeleportToolsConfig(BaseModel):
    """plugin_lambda_logs / kube_read: read-only tsh tools (see teleport_cli.py)."""

    # tsh 18.11 bundled in the image; the host-mounted /usr/local/bin/tsh (18.0.0)
    # fails `tsh aws` signature verification against the 18.11 proxy.
    tsh_path: str = "/opt/teleport/bin/tsh"
    # Container-local copy of teleport.auth_home; the tools never write the shared one.
    shadow_home: str = "/tmp/db-gateway/tsh-home"
    # Budget for one whole tool call (Nginx proxy_read_timeout is 60s).
    time_budget_seconds: float = 40
    max_concurrent: int = 3
    lambda_logs: LambdaLogsConfig = Field(default_factory=LambdaLogsConfig)
    kube: KubeReadConfig = Field(default_factory=KubeReadConfig)
    knowledgefs: KnowledgeFsConfig = Field(default_factory=KnowledgeFsConfig)


class AppConfig(BaseModel):
    resources: ResourcesConfig = Field(default_factory=ResourcesConfig)
    postgres: PostgresConfig = Field(default_factory=PostgresConfig)
    db_gateway: DbGatewayConfig = Field(default_factory=DbGatewayConfig)
    teleport: TeleportConfig = Field(default_factory=TeleportConfig)
    feishu: FeishuConfig = Field(default_factory=FeishuConfig)
    teleport_tools: TeleportToolsConfig = Field(default_factory=TeleportToolsConfig)


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
    if cfg.db_gateway.audit_log_dir.strip():
        cfg.db_gateway.audit_log_dir = _resolve(cfg.db_gateway.audit_log_dir.strip())

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
