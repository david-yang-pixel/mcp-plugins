import json
from contextlib import asynccontextmanager
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

from db_gateway.app import create_db_gateway_app
from db_gateway.config import AppConfig
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
        "knowledgefs",
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
    # Legacy callers may still send ticket_id: it is ignored, not an error.
    assert "ticket_id" not in query.await_args.kwargs
    assert data["result"]["isError"] is False


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
    assert props["explain"]["type"] == "boolean"
    assert props["count_total"]["type"] == "boolean" and props["count_total"]["default"] is False
    assert "ticket_id" not in props


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
    assert kwargs["explain"] is None
    assert "ticket_id" not in kwargs


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
    assert "ticket_id" not in kwargs


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


@pytest.mark.asyncio
async def test_mcp_tools_list_includes_schema_tool_read_only(mcp_client):
    resp = await _post_mcp(
        mcp_client,
        {"jsonrpc": "2.0", "id": 10, "method": "tools/list", "params": {}},
    )

    tools = {t["name"]: t for t in (await resp.json())["result"]["tools"]}
    assert set(tools) == {"db_gateway_query", "db_gateway_schema"}
    schema_tool = tools["db_gateway_schema"]
    assert schema_tool["annotations"] == tools["db_gateway_query"]["annotations"]
    assert schema_tool["annotations"]["readOnlyHint"] is True
    props = schema_tool["inputSchema"]["properties"]
    assert props["database"]["enum"] == ["prod", "billing", "plugin", "knowledgefs"]
    assert props["tables"]["type"] == "array"
    assert schema_tool["inputSchema"]["required"] == ["database", "tables"]


@pytest.mark.asyncio
async def test_mcp_tools_call_explain_is_forwarded(mcp_client):
    result = GatewayQueryResult(success=True, csv="QUERY PLAN\nSeq Scan\n", columns=["QUERY PLAN"])
    with patch("db_gateway.mcp.execute_gateway_query", AsyncMock(return_value=result)) as query:
        resp = await _post_mcp(
            mcp_client,
            {
                "jsonrpc": "2.0",
                "id": 11,
                "method": "tools/call",
                "params": {
                    "name": "db_gateway_query",
                    "arguments": {"database": "prod", "sql": "select 1", "explain": True},
                },
            },
        )

    assert resp.status == 200
    assert query.await_args.kwargs["explain"] is True


async def test_mcp_tools_call_count_total_is_forwarded(mcp_client):
    result = GatewayQueryResult(success=True, csv="x\n1\n", columns=["x"])
    with patch("db_gateway.mcp.execute_gateway_query", AsyncMock(return_value=result)) as query:
        resp = await _post_mcp(
            mcp_client,
            {
                "jsonrpc": "2.0",
                "id": 12,
                "method": "tools/call",
                "params": {
                    "name": "db_gateway_query",
                    "arguments": {"database": "prod", "sql": "select 1", "count_total": True},
                },
            },
        )

    assert resp.status == 200
    assert query.await_args.kwargs["count_total"] is True


@pytest.mark.asyncio
async def test_mcp_tools_call_schema_tool(mcp_client):
    payload = {"success": True, "database": "prod", "tables": [{"table": "workflow_runs"}]}
    with patch("db_gateway.mcp.execute_schema_lookup", AsyncMock(return_value=payload)) as lookup:
        resp = await _post_mcp(
            mcp_client,
            {
                "jsonrpc": "2.0",
                "id": 12,
                "method": "tools/call",
                "params": {
                    "name": "db_gateway_schema",
                    "arguments": {"database": "prod", "tables": ["workflow_runs"]},
                },
            },
        )

    data = await resp.json()
    assert data["result"]["isError"] is False
    assert json.loads(data["result"]["content"][0]["text"]) == payload
    assert lookup.await_args.kwargs["database"] == "prod"
    assert lookup.await_args.kwargs["tables"] == ["workflow_runs"]


@pytest.mark.asyncio
async def test_mcp_tools_call_schema_error_sets_is_error(mcp_client):
    payload = {"success": False, "code": "INVALID_ARGUMENT", "error": "bad"}
    with patch("db_gateway.mcp.execute_schema_lookup", AsyncMock(return_value=payload)):
        resp = await _post_mcp(
            mcp_client,
            {
                "jsonrpc": "2.0",
                "id": 13,
                "method": "tools/call",
                "params": {"name": "db_gateway_schema", "arguments": {"database": "prod"}},
            },
        )

    assert (await resp.json())["result"]["isError"] is True


@pytest.mark.asyncio
async def test_http_schema_endpoint(mcp_client):
    payload = {"success": True, "database": "billing", "tables": []}
    with patch("db_gateway.app.execute_schema_lookup", AsyncMock(return_value=payload)) as lookup:
        resp = await mcp_client.post(
            "/schema",
            json={"database": "billing", "tables": ["orders"]},
            headers={"X-API-Key": "secret"},
        )
        unauthorized = await mcp_client.post(
            "/schema", json={"database": "billing", "tables": ["orders"]},
            headers={"X-API-Key": "bad"},
        )

    assert resp.status == 200
    assert await resp.json() == payload
    assert lookup.await_count == 1
    assert lookup.await_args.kwargs["tables"] == ["orders"]
    assert unauthorized.status == 401


@pytest.mark.asyncio
async def test_http_query_forwards_explain_and_ignores_ticket_id(mcp_client):
    result = GatewayQueryResult(success=True, csv="QUERY PLAN\n")
    with patch("db_gateway.app.execute_gateway_query", AsyncMock(return_value=result)) as query:
        resp = await mcp_client.post(
            "/query",
            json={"database": "prod", "sql": "select 1", "ticket_id": "1", "explain": True, "count_total": True},
            headers={"X-API-Key": "secret"},
        )

    assert resp.status == 200
    assert query.await_args.kwargs["explain"] is True
    assert query.await_args.kwargs["count_total"] is True
    assert "ticket_id" not in query.await_args.kwargs


# --- plugin_lambda_logs / kube_read ---------------------------------------------

LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}


def call(name, args):
    return {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": name, "arguments": args}}


@pytest.fixture
def mcp_client_factory():
    @asynccontextmanager
    async def factory(teleport_tools):
        cfg = MagicMock()
        cfg.db_gateway.api_key = "secret"
        cfg.teleport.proxy = "teleport.example:443"
        cfg.teleport_tools = teleport_tools
        async with TestClient(TestServer(create_db_gateway_app(cfg))) as client:
            yield client
    return factory


@pytest.fixture
async def teleport_mcp_client(mcp_client_factory):
    async with mcp_client_factory(AppConfig().teleport_tools) as client:
        yield client


async def test_tools_list_adds_teleport_tools(teleport_mcp_client):
    data = await (await _post_mcp(teleport_mcp_client, LIST)).json()
    tools = {t["name"]: t for t in data["result"]["tools"]}
    assert list(tools) == ["db_gateway_query", "db_gateway_schema", "plugin_lambda_logs", "kube_read"]
    for name in ("plugin_lambda_logs", "kube_read"):
        assert tools[name]["annotations"] == {"readOnlyHint": True, "destructiveHint": False,
                                              "idempotentHint": True, "openWorldHint": True}
    assert tools["kube_read"]["inputSchema"]["properties"]["cluster"]["enum"] == ["prod", "staging"]


async def test_tools_list_hides_disabled_tools(mcp_client_factory):
    cfg_tools = AppConfig(teleport_tools={"lambda_logs": {"enabled": False},
                                          "kube": {"enabled": False}}).teleport_tools
    async with mcp_client_factory(cfg_tools) as client:
        names = [t["name"] for t in (await (await _post_mcp(client, LIST)).json())["result"]["tools"]]
        assert names == ["db_gateway_query", "db_gateway_schema"]
        resp = await _post_mcp(client, call("kube_read", {"cluster": "prod", "args": ["get", "pods"]}))
        assert (await resp.json())["error"]["code"] == -32601


async def test_existing_mock_config_still_lists_two_tools(mcp_client):
    names = [t["name"] for t in (await (await _post_mcp(mcp_client, LIST)).json())["result"]["tools"]]
    assert names == ["db_gateway_query", "db_gateway_schema"]


async def test_call_plugin_lambda_logs_passes_arguments(teleport_mcp_client):
    payload = {"success": True, "code": "", "error": None, "events": []}
    with patch("db_gateway.mcp.execute_plugin_lambda_logs", AsyncMock(return_value=payload)) as fn:
        resp = await _post_mcp(teleport_mcp_client, call("plugin_lambda_logs",
            {"function_name": "f", "since_minutes": 10, "filter_pattern": '"ERROR"', "ticket_id": "1"}))
    result = (await resp.json())["result"]
    assert result["isError"] is False and json.loads(result["content"][0]["text"]) == payload
    assert fn.call_args.kwargs["function_name"] == "f" and "ticket_id" not in fn.call_args.kwargs


async def test_call_kube_read_error_sets_is_error(teleport_mcp_client):
    payload = {"success": False, "code": "INVALID_ARGUMENT", "error": "x"}
    with patch("db_gateway.mcp.execute_kube_read", AsyncMock(return_value=payload)) as fn:
        resp = await _post_mcp(teleport_mcp_client, call("kube_read", {"cluster": "prod", "args": ["delete"]}))
    assert (await resp.json())["result"]["isError"] is True
    assert fn.call_args.kwargs == {"cfg": ANY, "cluster": "prod", "args": ["delete"]}


async def test_query_tool_lists_knowledgefs(mcp_client):
    tools = {t["name"]: t for t in (await (await _post_mcp(mcp_client, LIST)).json())["result"]["tools"]}
    assert tools["db_gateway_query"]["inputSchema"]["properties"]["database"]["enum"][-1] == "knowledgefs"
    assert "knowledgefs" in tools["db_gateway_query"]["description"]


async def test_app_cleanup_stops_tsh_daemons(mcp_client_factory):
    stop = AsyncMock()
    with patch("db_gateway.app.stop_tsh_daemons", stop):
        async with mcp_client_factory(AppConfig().teleport_tools):
            pass
    assert stop.await_count == 1


async def test_app_runs_background_maintenance_while_serving(tmp_path):
    from db_gateway.app import MAINTENANCE_TASK_KEY

    cfg = AppConfig(teleport={"auth_home": str(tmp_path / "tsh")}, db_gateway={"api_key": "secret"})
    app = create_db_gateway_app(cfg)
    async with TestClient(TestServer(app)):
        task = app[MAINTENANCE_TASK_KEY]
        assert not task.done()
    assert task.cancelled() or task.done()


async def test_status_reports_the_session_and_each_database_route(tmp_path):
    cfg = AppConfig(db_gateway={"api_key": "secret"},
                    teleport={"sessions_dir": str(tmp_path / "sessions")})
    cfg.postgres.teleport_db_plugin = "lg-pgsql-read-01"      # step 2: plugin moved in
    async with TestClient(TestServer(create_db_gateway_app(cfg))) as client:
        assert (await client.get("/status", headers={"X-API-Key": "wrong"})).status == 401
        data = await (await client.get("/status", headers={"Authorization": "Bearer secret"})).json()

    assert data["databases"]["prod"] == {"via": "static", "port": 56061}
    assert data["databases"]["plugin"] == {"via": "db-gateway", "teleport_db": "lg-pgsql-read-01", "port": 56073}
    assert data["databases"]["knowledgefs"]["via"] == "db-gateway"
    assert data["ports"]["plugin"] == 56073
    session = data["teleport_session"]
    assert session["generation"] is None and session["auto_login"] is False and session["blocked"] is None


async def test_manual_login_alert_carries_a_fresh_session_link(tmp_path):
    from db_gateway.app import _teleport_alert
    from db_gateway.app_context import TELEPORT_AUTH_TOKEN_STORE_KEY
    from db_gateway.teleport_tokens import SCOPE_SESSION

    cfg = AppConfig(teleport={"public_base_url": "https://agent.example.com"},
                    feishu={"webhook_url": "https://open.feishu.example/hook"})
    app = create_db_gateway_app(cfg)
    send = AsyncMock()
    with patch("db_gateway.feishu.FeishuClient.send_alert", send):
        alert = _teleport_alert(app)
        await alert("title", "body", True)
        await alert("title2", "body2", False)

    first, second = (c.kwargs["message"] for c in send.await_args_list)
    url = first.split("认证链接（一次性，60 分钟内有效）：", 1)[1]
    entry = await app[TELEPORT_AUTH_TOKEN_STORE_KEY].lookup(url.split("t=", 1)[1])
    assert entry.scope == SCOPE_SESSION and entry.ttl_minutes == 60
    assert second == "body2"
    assert _teleport_alert(create_db_gateway_app(AppConfig())) is None    # no webhook: log only
