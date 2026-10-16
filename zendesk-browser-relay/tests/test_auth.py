from fastapi.testclient import TestClient


def test_send_rejects_missing_bearer_token(monkeypatch):
    monkeypatch.setenv("RELAY_API_KEY", "x" * 32)
    from zendesk_browser_relay.app import create_app
    from zendesk_browser_relay.settings import get_settings

    get_settings.cache_clear()
    client = TestClient(create_app())

    response = client.post("/send", json={"ticket_id": 1, "body": "hello"})

    assert response.status_code == 401
    assert response.json()["detail"]["error"] == "missing_bearer_token"


def test_send_rejects_invalid_bearer_token(monkeypatch):
    monkeypatch.setenv("RELAY_API_KEY", "x" * 32)
    from zendesk_browser_relay.app import create_app
    from zendesk_browser_relay.settings import get_settings

    get_settings.cache_clear()
    client = TestClient(create_app())

    response = client.post(
        "/send",
        headers={"Authorization": "Bearer wrong"},
        json={"ticket_id": 1, "body": "hello"},
    )

    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "invalid_api_key"
