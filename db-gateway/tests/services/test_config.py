"""Config defaults and new query options."""
from db_gateway.config import AppConfig, load_config


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
