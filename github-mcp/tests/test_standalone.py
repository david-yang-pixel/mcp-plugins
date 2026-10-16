import asyncio

from fastapi.testclient import TestClient
from starlette.requests import Request


def test_standalone_service_initializes_and_reports_its_identity(monkeypatch):
    monkeypatch.setenv('GITHUB_MCP_API_KEY', "isolated-test-key")
    from github_mcp.settings import get_settings
    from github_mcp.app import create_app
    get_settings.cache_clear()
    client = TestClient(create_app())
    assert client.get("/health").json()["service"] == 'github-mcp'
    response = client.post("/mcp", headers={"Authorization": "Bearer isolated-test-key"}, json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert response.status_code == 200
    assert response.json()["result"]["serverInfo"]["name"] == 'github-mcp'
    assert client.post("/mcp", headers={"Authorization": "Bearer isolated-test-key"}, json={"jsonrpc": "2.0", "method": "notifications/initialized"}).status_code == 202


def test_standalone_service_preserves_deployed_sse_contract(monkeypatch):
    monkeypatch.setenv('GITHUB_MCP_API_KEY', "isolated-test-key")
    from github_mcp.settings import get_settings
    from github_mcp.app import create_app
    get_settings.cache_clear()
    app = create_app()
    endpoint = next(r.endpoint for r in app.routes if r.path == "/sse")
    async def check():
        request = Request({"type": "http", "headers": []})
        response = await endpoint(request=request, authorization="Bearer isolated-test-key", settings=get_settings())
        assert response.status_code == 200
        assert await anext(response.body_iterator) == "event: endpoint\ndata: /github-mcp/mcp\n\n"
        await response.body_iterator.aclose()
    asyncio.run(check())
