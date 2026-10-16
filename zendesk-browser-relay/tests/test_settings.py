from pathlib import Path


def test_settings_normalize_zendesk_subdomain(monkeypatch):
    monkeypatch.setenv("RELAY_API_KEY", "x" * 32)
    monkeypatch.setenv("ZENDESK_SUBDOMAIN", "https://langgenius.zendesk.com")
    monkeypatch.setenv("ZENDESK_PROFILE_DIR", "/tmp/relay-profile")
    monkeypatch.setenv("TICKET_RUN_DB_PATH", "/tmp/ticket-runs.sqlite3")

    from zendesk_browser_relay.settings import get_settings

    get_settings.cache_clear()
    settings = get_settings()

    assert settings.zendesk_subdomain == "langgenius"
    assert settings.zendesk_base_url == "https://langgenius.zendesk.com"
    assert settings.zendesk_profile_dir == Path("/tmp/relay-profile")
    assert settings.ticket_run_db_path == Path("/tmp/ticket-runs.sqlite3")
    assert settings.ticket_run_active_lease_seconds == 1_800
    assert settings.ticket_run_coalesce_active_messages is False
    assert settings.ticket_automation_allowlist_values == frozenset()


def test_settings_parse_ticket_automation_allowlist(monkeypatch):
    monkeypatch.setenv("RELAY_API_KEY", "x" * 32)
    monkeypatch.setenv("TICKET_AUTOMATION_ALLOWLIST", "3880, 3936,3880")
    from zendesk_browser_relay.settings import get_settings

    get_settings.cache_clear()
    settings = get_settings()

    assert settings.ticket_automation_allowlist_values == frozenset({3880, 3936})
