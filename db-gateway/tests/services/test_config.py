"""Config defaults and new query options."""
from db_gateway.config import AppConfig, load_config


def test_new_query_options_have_backward_compatible_defaults():
    cfg = AppConfig()
    assert cfg.db_gateway.require_ticket_id is False
    assert cfg.db_gateway.missing_ticket_alert_interval_seconds == 0
    assert cfg.postgres.count_timeout_seconds == 5
    assert cfg.postgres.max_rows == 1000


def test_load_config_reads_new_options(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DB_GATEWAY_REQUIRE_TICKET_ID", raising=False)
    path = tmp_path / "config.yaml"
    path.write_text(
        "postgres:\n  count_timeout_seconds: 2\n"
        "db_gateway:\n  require_ticket_id: true\n  missing_ticket_alert_interval_seconds: 600\n",
        encoding="utf-8",
    )
    cfg = load_config(str(path))
    assert cfg.postgres.count_timeout_seconds == 2
    assert cfg.db_gateway.require_ticket_id is True
    assert cfg.db_gateway.missing_ticket_alert_interval_seconds == 600


def test_env_can_enable_require_ticket_id(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DB_GATEWAY_REQUIRE_TICKET_ID", "true")
    cfg = load_config(str(tmp_path / "missing.yaml"))
    assert cfg.db_gateway.require_ticket_id is True
