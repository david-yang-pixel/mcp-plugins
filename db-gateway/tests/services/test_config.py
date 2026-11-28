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


def test_pool_driver_rejects_unknown_driver():
    import pydantic
    import pytest as _pytest

    from db_gateway.config import PostgresConfig

    with _pytest.raises(pydantic.ValidationError):
        PostgresConfig(driver="psycopg2")


def test_teleport_tools_defaults():
    t = AppConfig().teleport_tools
    assert (t.time_budget_seconds, t.max_concurrent) == (40, 3)
    assert t.lambda_logs.model_dump() == {
        "enabled": True, "app": "aws-support-logs", "aws_role": "SupportLambdaLogsReadOnly",
        "region": "us-east-1", "default_timezone": "Asia/Shanghai",
        "default_limit": 1000, "max_limit": 10000,
    }
    assert t.kube.clusters == {"prod": "lg-eks-01", "staging": "dify-plugin-1"}
    assert t.kube.max_output_bytes == 1048576


def test_runtime_settings_enable_both_teleport_tools():
    settings = json.loads((ROOT / "deploy" / "runtime.json").read_text())["settings"]
    t = AppConfig(**settings).teleport_tools
    assert t.lambda_logs.enabled is True and t.kube.enabled is True
    assert "teleport_tools" in settings


def test_dockerfile_bundles_pinned_tsh():
    text = (ROOT / "Dockerfile").read_text()
    assert "TELEPORT_VERSION=18.11.0" in text
    assert "8d3dba03ea6b6e0d72c97e190da0595e8f0d70f5573c3a26b1908cf60caf6e3f" in text
    assert AppConfig().teleport.tsh_path in text      # /opt/teleport/bin/tsh


def test_knowledgefs_defaults():
    cfg = AppConfig()
    assert (cfg.postgres.db_knowledgefs, cfg.postgres.port_knowledgefs) == ("knowledgefs", 56064)
    k = cfg.teleport_tools.knowledgefs
    assert (k.enabled, k.teleport_db) == (True, "knowledge-fs-production-readonly")
    assert not hasattr(k, "db_user")       # 查询和隧道都用 postgres.user（PR #30 评论）


def test_runtime_settings_enable_knowledgefs():
    settings = json.loads((ROOT / "deploy" / "runtime.json").read_text())["settings"]
    cfg = AppConfig(**settings)
    assert settings["teleport_tools"]["knowledgefs"]["enabled"] is True
    assert settings["postgres"]["port_knowledgefs"] == 56064
    assert cfg.teleport_tools.knowledgefs.enabled is True


def test_teleport_session_defaults():
    t = AppConfig().teleport
    assert t.tsh_path == "/opt/teleport/bin/tsh"          # the only tsh db-gateway runs
    assert t.sessions_dir == "/tmp/db-gateway/tsh-sessions"
    assert (t.auto_renew, t.renew_before_seconds, t.renew_retry_seconds) == (True, 480, 30)
    assert (t.session_retain_seconds, t.max_auth_failures) == (90, 2)


def test_teleport_credentials_come_from_env_and_are_never_serialized(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEPORT_PASSWORD", "pw-secret-value")
    monkeypatch.setenv("TELEPORT_TOTP_SECRET", "TOTPSECRETVALUE")
    cfg = load_config(str(tmp_path / "missing.yaml"))
    assert cfg.teleport.password == "pw-secret-value"
    assert cfg.teleport.totp_secret == "TOTPSECRETVALUE"
    dumped = json.dumps(cfg.model_dump(mode="json"))
    assert "pw-secret-value" not in dumped and "TOTPSECRETVALUE" not in dumped
    assert "pw-secret-value" not in repr(cfg) and "TOTPSECRETVALUE" not in repr(cfg)


def test_self_managed_tunnels_are_off_by_default_with_fixed_ports():
    pg = AppConfig().postgres
    assert (pg.teleport_db_prod, pg.teleport_db_billing, pg.teleport_db_plugin) == ("", "", "")
    assert (pg.tunnel_port_prod, pg.tunnel_port_billing, pg.tunnel_port_plugin) == (56071, 56072, 56073)
    assert (pg.port_prod, pg.port_billing, pg.port_plugin) == (56061, 56062, 56063)  # host tunnels


def test_retired_teleport_keys_in_old_configs_still_load(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "config.yaml"
    path.write_text(
        "teleport:\n  auth_home: /opt/zd-agent/.tsh\n  proxy: t.example:443\n"
        "teleport_tools:\n  tsh_path: /usr/local/bin/tsh\n  shadow_home: /tmp/x\n"
        "  kube:\n    kubeconfig_path: /tmp/kc\n"
        "postgres:\n  pool_watch_teleport_keys: true\n",
        encoding="utf-8",
    )
    cfg = load_config(str(path))
    assert cfg.teleport.proxy == "t.example:443"
    assert cfg.teleport.tsh_path == "/opt/teleport/bin/tsh"   # the retired tools key is ignored


def test_runtime_settings_no_longer_chown_host_tsh():
    runtime = json.loads((ROOT / "deploy" / "runtime.json").read_text())
    assert "HOST_TSH_UID" not in runtime["environment"]
    assert "HOST_TSH_GID" not in runtime["environment"]
    assert {"TELEPORT_PASSWORD", "TELEPORT_TOTP_SECRET", "TELEPORT_USER"} <= set(runtime["secrets"])
