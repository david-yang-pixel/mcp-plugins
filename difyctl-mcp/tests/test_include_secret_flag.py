import pytest

from difyctl_mcp.policy import PolicyError, check_input

DESCRIPTOR = {"input": {"properties": {"app_id": {"type": "string"}, "include_secret": {"type": "boolean"}}}}


def test_include_secret_blocked_by_default(monkeypatch):
    monkeypatch.delenv("DIFYCTL_ALLOW_INCLUDE_SECRET", raising=False)
    with pytest.raises(PolicyError) as err:
        check_input("export.console_app.dsl", DESCRIPTOR, {"app_id": "a", "include_secret": True})
    assert err.value.code == "forbidden_input"


@pytest.mark.parametrize("value", ["true", "1", "yes", "TRUE"])
def test_include_secret_allowed_when_enabled(monkeypatch, value):
    monkeypatch.setenv("DIFYCTL_ALLOW_INCLUDE_SECRET", value)
    assert check_input("export.console_app.dsl", DESCRIPTOR, {"app_id": "a", "include_secret": True})


def test_include_secret_flag_false(monkeypatch):
    monkeypatch.setenv("DIFYCTL_ALLOW_INCLUDE_SECRET", "false")
    with pytest.raises(PolicyError):
        check_input("export.console_app.dsl", DESCRIPTOR, {"app_id": "a", "include_secret": True})
