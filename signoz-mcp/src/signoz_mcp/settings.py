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
    # Absolute windows (start/end) and long relative windows are capped here.
    max_window_days: float = Field(default=31.0, validation_alias="SIGNOZ_MCP_MAX_WINDOW_DAYS")
    # Count/aggregate structured tools split windows longer than the threshold
    # into chunk_hours sub-windows and merge the results.
    chunk_threshold_hours: float = Field(
        default=48.0,
        validation_alias="SIGNOZ_MCP_CHUNK_THRESHOLD_HOURS",
    )
    chunk_hours: float = Field(default=24.0, validation_alias="SIGNOZ_MCP_CHUNK_HOURS")
    chunk_concurrency: int = Field(default=2, validation_alias="SIGNOZ_MCP_CHUNK_CONCURRENCY")
    chunk_row_limit: int = Field(default=10000, validation_alias="SIGNOZ_MCP_CHUNK_ROW_LIMIT")
    # Kept below the 60s proxy read timeout in front of the legacy route, so a
    # long aggregation returns its partial result instead of a gateway 504.
    total_timeout_seconds: float = Field(
        default=50.0,
        validation_alias="SIGNOZ_MCP_TOTAL_TIMEOUT_SECONDS",
    )
    # Adds ts_bucket_start predicates (SigNoz logs v2 / traces v3 primary key)
    # so ClickHouse can skip granules outside the window.
    ts_bucket_pruning: bool = Field(default=True, validation_alias="SIGNOZ_MCP_TS_BUCKET_PRUNING")
    log_compact_denylist: str = Field(
        default=(
            "host.ip,host.mac,host.id,host.arch,host.image.id,os.type,os.description,os.version,"
            "process.pid,process.parent_pid,process.owner,process.executable.name,"
            "process.executable.path,process.command,process.command_line,process.command_args,"
            "process.runtime.name,process.runtime.version,process.runtime.description,"
            "telemetry.sdk.name,telemetry.sdk.language,telemetry.sdk.version,telemetry.auto.version,"
            "k8s.pod.uid,k8s.node.uid,k8s.replicaset.uid,k8s.deployment.uid,container.id,"
            "log.file.path,log.iostream,logtag"
        ),
        validation_alias="SIGNOZ_MCP_LOG_COMPACT_DENYLIST",
    )
    log_max_field_chars: int = Field(default=4000, validation_alias="SIGNOZ_MCP_LOG_MAX_FIELD_CHARS")
    signoz_url: str = Field(default="", validation_alias="SIGNOZ_URL")
    signoz_api_key: str = Field(default="", validation_alias="SIGNOZ_API_KEY")

    @property
    def cacheable_command_set(self) -> set[str]:
        return {
            command.strip()
            for command in self.cacheable_commands.split(",")
            if command.strip()
        }

    @property
    def log_compact_denyset(self) -> frozenset[str]:
        return frozenset(
            key.strip() for key in self.log_compact_denylist.split(",") if key.strip()
        )

    @property
    def max_window_hours(self) -> float:
        return max(1.0, float(self.max_window_days) * 24.0)


@lru_cache
def get_settings() -> Settings:
    return Settings()
