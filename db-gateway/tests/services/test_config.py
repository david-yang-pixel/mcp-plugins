"""Config defaults and new query options."""
import json
from pathlib import Path

from db_gateway.config import AppConfig, load_config

ROOT = Path(__file__).resolve().parents[2]


def test_new_query_options_have_backward_compatible_defaults():
    cfg = AppConfig()
    assert cfg.postgres.count_timeout_seconds == 5
    assert cfg.postgres.max_rows == 1000
    assert cfg.db_gateway.audit_log_dir == ""
    assert not hasattr(cfg.db_gateway, "require_ticket_id")
    assert not hasattr(cfg.db_gateway, "missing_ticket_alert_interval_seconds")


def test_old_config_with_retired_ticket_keys_still_loads(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DB_GATEWAY_REQUIRE_TICKET_ID", "true")  # retired env var: ignored
    path = tmp_path / "config.yaml"
    path.write_text(
        "postgres:\n  count_timeout_seconds: 2\n"
        "db_gateway:\n  listen_port: 18082\n  require_ticket_id: true\n"
        "  missing_ticket_alert_interval_seconds: 600\n",
        encoding="utf-8",
    )
    cfg = load_config(str(path))
    assert cfg.postgres.count_timeout_seconds == 2
    assert cfg.db_gateway.listen_port == 18082
    assert not hasattr(cfg.db_gateway, "require_ticket_id")


def test_audit_log_dir_is_read_and_resolved(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "conf" / "config.yaml"
    path.parent.mkdir()
    path.write_text("db_gateway:\n  audit_log_dir: ./audit\n", encoding="utf-8")
    assert load_config(str(path)).db_gateway.audit_log_dir == str(tmp_path / "conf" / "audit")
    path.write_text("db_gateway:\n  audit_log_dir: /var/lib/mcp/logs\n", encoding="utf-8")
    assert load_config(str(path)).db_gateway.audit_log_dir == "/var/lib/mcp/logs"


def test_runtime_settings_load_and_put_audit_log_on_the_data_mount(tmp_path, monkeypatch):
    """deploy/runtime.json settings are written as config.yaml by the deployer."""
    import json
    from pathlib import Path

    monkeypatch.chdir(tmp_path)
    runtime = json.loads(
        (Path(__file__).resolve().parents[2] / "deploy" / "runtime.json").read_text(encoding="utf-8")
    )
    path = tmp_path / "config.yaml"
    path.write_text(json.dumps(runtime["settings"]), encoding="utf-8")
    cfg = load_config(str(path))
    assert cfg.db_gateway.audit_log_dir == "/var/lib/mcp/logs"
    assert "require_ticket_id" not in runtime["settings"]["db_gateway"]
    assert "missing_ticket_alert_interval_seconds" not in runtime["settings"]["db_gateway"]


def test_pool_driver_settings_default_to_psql():
    from db_gateway.config import PostgresConfig

    pg = PostgresConfig()
    assert pg.driver == "psql"
    assert pg.pool_min_size == 1
    assert pg.pool_max_idle_seconds == 240
    assert pg.pool_retry_delay_seconds == 1.5
    assert pg.pool_watch_teleport_keys is True


def test_pool_driver_rejects_unknown_driver():
    import pydantic
    import pytest as _pytest

    from db_gateway.config import PostgresConfig

    with _pytest.raises(pydantic.ValidationError):
        PostgresConfig(driver="psycopg2")


def test_teleport_tools_defaults():
    t = AppConfig().teleport_tools
    assert (t.tsh_path, t.shadow_home) == ("/opt/teleport/bin/tsh", "/tmp/db-gateway/tsh-home")
    assert (t.time_budget_seconds, t.max_concurrent) == (40, 3)
    assert t.lambda_logs.model_dump() == {
        "enabled": True, "app": "aws-support-logs", "aws_role": "SupportLambdaLogsReadOnly",
        "region": "us-east-1", "default_timezone": "Asia/Shanghai",
        "default_limit": 1000, "max_limit": 10000,
    }
    assert t.kube.clusters == {"prod": "lg-eks-01", "staging": "dify-plugin-1"}
    assert t.kube.max_output_bytes == 1048576
    assert t.kube.kubeconfig_path == "/tmp/db-gateway/kubeconfig"


def test_runtime_settings_enable_both_teleport_tools():
    settings = json.loads((ROOT / "deploy" / "runtime.json").read_text())["settings"]
    t = AppConfig(**settings).teleport_tools
    assert t.lambda_logs.enabled is True and t.kube.enabled is True
    assert "teleport_tools" in settings


def test_dockerfile_bundles_pinned_tsh():
    text = (ROOT / "Dockerfile").read_text()
    assert "TELEPORT_VERSION=18.11.0" in text
    assert "8d3dba03ea6b6e0d72c97e190da0595e8f0d70f5573c3a26b1908cf60caf6e3f" in text
    assert AppConfig().teleport_tools.tsh_path in text      # /opt/teleport/bin/tsh


def test_knowledgefs_defaults():
    cfg = AppConfig()
    assert (cfg.postgres.db_knowledgefs, cfg.postgres.port_knowledgefs) == ("knowledgefs", 56064)
    k = cfg.teleport_tools.knowledgefs
    assert (k.enabled, k.teleport_db, k.db_user) == (True, "knowledge-fs-production-readonly", "")


def test_runtime_settings_enable_knowledgefs():
    settings = json.loads((ROOT / "deploy" / "runtime.json").read_text())["settings"]
    cfg = AppConfig(**settings)
    assert settings["teleport_tools"]["knowledgefs"]["enabled"] is True
    assert settings["postgres"]["port_knowledgefs"] == 56064
    assert cfg.teleport_tools.knowledgefs.enabled is True
