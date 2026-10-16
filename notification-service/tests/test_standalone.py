import asyncio

from fastapi.testclient import TestClient
from starlette.requests import Request


def test_standalone_service_initializes_and_reports_its_identity(monkeypatch):
    monkeypatch.setenv('NOTIFICATION_SERVICE_API_KEY', "isolated-test-key")
    from notification_service.settings import get_settings
    from notification_service.app import create_app
    get_settings.cache_clear()
    client = TestClient(create_app())
    assert client.get("/health").json()["service"] == 'notification-service'
    response = client.post("/mcp", headers={"Authorization": "Bearer isolated-test-key"}, json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert response.status_code == 200
    assert response.json()["result"]["serverInfo"]["name"] == 'notification-service'
    assert client.post("/mcp", headers={"Authorization": "Bearer isolated-test-key"}, json={"jsonrpc": "2.0", "method": "notifications/initialized"}).status_code == 202


def test_standalone_service_preserves_deployed_sse_contract(monkeypatch):
    monkeypatch.setenv('NOTIFICATION_SERVICE_API_KEY', "isolated-test-key")
    from notification_service.settings import get_settings
    from notification_service.app import create_app
    get_settings.cache_clear()
    app = create_app()
    endpoint = next(r.endpoint for r in app.routes if r.path == "/sse")
    async def check():
        response = await endpoint(authorization="Bearer isolated-test-key", settings=get_settings())
        assert response.status_code == 200
        assert response.body == b"event: endpoint\ndata: /mcp\n\n"
    asyncio.run(check())
