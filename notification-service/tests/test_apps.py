from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}






def test_notification_service_requires_own_api_key(monkeypatch) -> None:
    monkeypatch.setenv("NOTIFICATION_SERVICE_API_KEY", "notify-secret")
    from notification_service.settings import get_settings

    get_settings.cache_clear()
    from notification_service.app import create_service_app

    client = TestClient(create_service_app("notification-service"))
    response = client.post(
        "/mcp",
        headers=_auth("wrong-secret"),
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )

    assert response.status_code == 401
