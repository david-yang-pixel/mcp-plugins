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


@pytest.mark.asyncio
async def test_mcp_tools_list_exposes_paging_and_format(mcp_client):
    resp = await _post_mcp(
        mcp_client,
        {"jsonrpc": "2.0", "id": 5, "method": "tools/list", "params": {}},
    )

    props = (await resp.json())["result"]["tools"][0]["inputSchema"]["properties"]
    assert props["offset"]["type"] == "integer" and props["offset"]["minimum"] == 0
    assert props["limit"]["type"] == "integer" and props["limit"]["minimum"] == 1
    assert props["format"]["enum"] == ["csv", "json"]
    assert "ticket_id" in props


@pytest.mark.asyncio
async def test_mcp_tools_call_forwards_paging_and_format(mcp_client):
    result = GatewayQueryResult(success=True, format="json", columns=["ok"], rows=[["1"]], row_count=1)
    with patch("db_gateway.mcp.execute_gateway_query", AsyncMock(return_value=result)) as query:
        resp = await _post_mcp(
            mcp_client,
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {
                    "name": "db_gateway_query",
                    "arguments": {
                        "database": "prod",
                        "sql": "select 1 as ok",
                        "offset": 20,
                        "limit": 10,
                        "format": "json",
                    },
                },
            },
        )

    content = json.loads((await resp.json())["result"]["content"][0]["text"])
    assert content["rows"] == [["1"]]
    kwargs = query.await_args.kwargs
    assert kwargs["offset"] == 20
    assert kwargs["limit"] == 10
    assert kwargs["output_format"] == "json"
    assert kwargs["ticket_id"] == ""


@pytest.mark.asyncio
async def test_http_query_forwards_paging_and_format(mcp_client):
    result = GatewayQueryResult(success=True, csv="ok\n1\n", row_count=1)
    with patch("db_gateway.app.execute_gateway_query", AsyncMock(return_value=result)) as query:
        resp = await mcp_client.post(
            "/query",
            json={"database": "prod", "sql": "select 1", "ticket_id": "9", "offset": 5, "limit": 2, "format": "csv"},
            headers={"X-API-Key": "secret"},
        )

    assert resp.status == 200
    kwargs = query.await_args.kwargs
    assert (kwargs["offset"], kwargs["limit"], kwargs["output_format"]) == (5, 2, "csv")


@pytest.mark.asyncio
async def test_http_query_rejects_non_object_body(mcp_client):
    resp = await mcp_client.post("/query", json=[1, 2], headers={"X-API-Key": "secret"})

    assert resp.status == 400


@pytest.mark.asyncio
async def test_mcp_tool_is_annotated_read_only(mcp_client):
    resp = await _post_mcp(
        mcp_client,
        {"jsonrpc": "2.0", "id": 9, "method": "tools/list", "params": {}},
    )

    assert resp.status == 200
    tools = (await resp.json())["result"]["tools"]
    assert tools[0]["annotations"]["readOnlyHint"] is True
    assert tools[0]["annotations"]["destructiveHint"] is False
