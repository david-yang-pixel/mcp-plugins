import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

from db_gateway.app import create_db_gateway_app
from db_gateway.core import GatewayQueryResult


@pytest.fixture
async def mcp_client():
    cfg = MagicMock()
    cfg.db_gateway.api_key = "secret"
    cfg.postgres.host = "localhost"
    cfg.postgres.port_prod = 56061
    cfg.postgres.port_billing = 56062
    cfg.postgres.port_plugin = 56063
    cfg.teleport.proxy = "teleport.example:443"

    async with TestClient(TestServer(create_db_gateway_app(cfg))) as client:
        yield client


async def _post_mcp(client, payload, *, api_key="secret"):
    return await client.post(
        "/mcp",
        json=payload,
        headers={
            "X-API-Key": api_key,
            "Accept": "application/json, text/event-stream",
        },
    )


@pytest.mark.asyncio
async def test_mcp_rejects_unauthorized(mcp_client):
    resp = await _post_mcp(
        mcp_client,
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        api_key="bad",
    )

    assert resp.status == 401


@pytest.mark.asyncio
async def test_mcp_initialize(mcp_client):
    resp = await _post_mcp(
        mcp_client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        },
    )

    assert resp.status == 200
    data = await resp.json()
    assert data["result"]["serverInfo"]["name"] == "db-gateway-mcp"
    assert data["result"]["capabilities"] == {"tools": {}}


@pytest.mark.asyncio
async def test_mcp_tools_list(mcp_client):
    resp = await _post_mcp(
        mcp_client,
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    )

    assert resp.status == 200
    data = await resp.json()
    tools = data["result"]["tools"]
    assert tools[0]["name"] == "db_gateway_query"
    assert tools[0]["inputSchema"]["properties"]["database"]["enum"] == [
        "prod",
        "billing",
        "plugin",
    ]


@pytest.mark.asyncio
async def test_mcp_tools_call_queries_gateway(mcp_client):
    result = GatewayQueryResult(
        success=True,
        csv="ok\n1\n",
        row_count=1,
        elapsed_ms=12,
        truncated=False,
    )
    with patch("db_gateway.mcp.execute_gateway_query", AsyncMock(return_value=result)) as query:
        resp = await _post_mcp(
            mcp_client,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "db_gateway_query",
                    "arguments": {
                        "database": "prod",
                        "sql": "select 1 as ok",
                        "ticket_id": "2830",
                    },
                },
            },
        )

    assert resp.status == 200
    data = await resp.json()
    content = json.loads(data["result"]["content"][0]["text"])
    assert content["success"] is True
    assert content["csv"] == "ok\n1\n"
    query.assert_awaited_once()
    assert query.await_args.kwargs["database"] == "prod"
    assert query.await_args.kwargs["ticket_id"] == "2830"


@pytest.mark.asyncio
async def test_mcp_accepts_bearer_auth(mcp_client):
    resp = await mcp_client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 4, "method": "tools/list", "params": {}},
        headers={
            "Authorization": "Bearer secret",
            "Accept": "application/json, text/event-stream",
        },
    )

    assert resp.status == 200


@pytest.mark.asyncio
async def test_mcp_get_returns_sse_probe(mcp_client):
    resp = await mcp_client.get(
        "/mcp",
        headers={"X-API-Key": "secret", "Accept": "text/event-stream"},
    )

    assert resp.status == 200
    assert resp.headers["Content-Type"].startswith("text/event-stream")
    assert await resp.content.readline() == b"event: endpoint\n"
    assert await resp.content.readline() == b"data: /db-gateway/mcp\n"
    resp.close()
