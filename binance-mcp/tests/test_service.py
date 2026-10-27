import hashlib
import hmac
import json
import logging
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient

from binance_mcp import app as service


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("BINANCE_MCP_API_KEY", "synthetic-mcp")
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)
    with TestClient(service.app) as c:
        yield c


def rpc(client, method, params=None, token="synthetic-mcp"):
    return client.post("/mcp", headers={"Authorization": "Bearer " + token},
                       json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})


def call(client, name, arguments=None):
    return rpc(client, "tools/call", {"name": name, "arguments": arguments or {}}).json()["result"]


def mock_binance(monkeypatch, handler):
    original = httpx.AsyncClient
    def make_client(**kwargs):
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        return original(transport=httpx.MockTransport(handler), **kwargs)
    monkeypatch.setattr(service.httpx, "AsyncClient", make_client)


def test_auth_protocol_and_discovery(client, monkeypatch):
    assert client.get("/health").json() == {"ok": True, "service": "binance-mcp"}
    assert rpc(client, "tools/list", token="wrong").status_code == 401
    assert client.post("/mcp", json={}).status_code == 401
    listing = rpc(client, "tools/list").json()["result"]["tools"]
    assert {t["name"] for t in listing} == set(service.TOOLS)
    assert all(t["annotations"]["readOnlyHint"] and t["inputSchema"]["additionalProperties"] is False for t in listing)
    assert rpc(client, "initialize").json()["result"]["serverInfo"]["name"] == "binance-mcp"
    assert rpc(client, "ping").json()["result"] == {}
    assert rpc(client, "bad").json()["error"]["code"] == -32601
    assert client.post("/mcp", headers={"Authorization": "Bearer synthetic-mcp"}, content="[").json()["error"]["code"] == -32700
    assert client.post("/mcp", headers={"Authorization": "Bearer synthetic-mcp"}, json=[]).json()["error"]["code"] == -32600
    monkeypatch.delenv("BINANCE_MCP_API_KEY")
    assert rpc(client, "tools/list").status_code == 503


def test_public_works_without_credentials_and_accounts_fail(client, monkeypatch):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"symbol": "BTCUSDT", "price": "42"})
    mock_binance(monkeypatch, handler)
    assert call(client, "price", {"symbol": "BTCUSDT"})["structuredContent"]["data"]["price"] == "42"
    for tool, args in [("account", {}), ("open_orders", {"symbol": "BTCUSDT"}), ("all_orders", {"symbol": "BTCUSDT"})]:
        result = call(client, tool, args)
        assert result["isError"] and result["structuredContent"]["code"] == "CREDENTIALS_MISSING"
    assert len(requests) == 1 and "X-MBX-APIKEY" not in requests[0].headers
    monkeypatch.setenv("BINANCE_API_KEY", "fake-key")
    assert call(client, "account")["structuredContent"]["code"] == "CREDENTIALS_MISSING"


@pytest.mark.parametrize("name,args,path,signed", [
    ("exchange_info", {"symbol": "BTCUSDT"}, "/api/v3/exchangeInfo", False),
    ("price", {"symbol": "BTCUSDT"}, "/api/v3/ticker/price", False),
    ("ticker_24h", {"symbol": "BTCUSDT"}, "/api/v3/ticker/24hr", False),
    ("depth", {"symbol": "BTCUSDT", "limit": 5000}, "/api/v3/depth", False),
    ("klines", {"symbol": "BTCUSDT", "interval": "1M", "limit": 1000}, "/api/v3/klines", False),
    ("account", {}, "/api/v3/account", True),
    ("open_orders", {"symbol": "BTCUSDT"}, "/api/v3/openOrders", True),
    ("all_orders", {"symbol": "BTCUSDT", "limit": 1000}, "/api/v3/allOrders", True),
])
def test_all_endpoints_are_get_and_signature(client, monkeypatch, name, args, path, signed):
    monkeypatch.setenv("BINANCE_API_KEY", "synthetic-binance-key")
    monkeypatch.setenv("BINANCE_API_SECRET", "synthetic-binance-secret")
    monkeypatch.setattr(service.time, "time", lambda: 1700000000.123)
    def handler(req):
        assert req.method == "GET" and req.url.host == "api.binance.com" and req.url.scheme == "https" and req.url.path == path
        query = req.url.query.decode()
        if signed:
            raw, sig = query.rsplit("&signature=", 1)
            assert sig == hmac.new(b"synthetic-binance-secret", raw.encode(), hashlib.sha256).hexdigest()
            parsed = parse_qs(raw)
            assert parsed["timestamp"] == ["1700000000123"] and parsed["recvWindow"] == ["5000"]
            assert req.headers["X-MBX-APIKEY"] == "synthetic-binance-key"
        else:
            assert "signature=" not in query and "X-MBX-APIKEY" not in req.headers
        return httpx.Response(200, json={"data": [1, 2]})
    mock_binance(monkeypatch, handler)
    assert not call(client, name, args)["isError"]


@pytest.mark.parametrize("name,args", [
    ("price", {}), ("price", {"symbol": "btcusdt"}), ("price", {"symbol": "A/B"}),
    ("price", {"symbol": "A"}), ("price", {"symbol": "A" * 21}),
    ("price", {"symbol": "BTCUSDT", "url": "http://127.0.0.1"}),
    ("account", {"api_key": "injected"}), ("depth", {"symbol": "BTCUSDT", "limit": 1}),
    ("depth", {"symbol": "BTCUSDT", "limit": 5.0}),
    ("depth", {"symbol": "BTCUSDT", "limit": "5"}),
    ("depth", {"symbol": "BTCUSDT", "limit": 5001}),
    ("klines", {"symbol": "BTCUSDT", "interval": "1H"}),
    ("klines", {"symbol": "BTCUSDT", "interval": "1m", "limit": 0}),
    ("klines", {"symbol": "BTCUSDT", "interval": "1m", "limit": 1001}),
    ("all_orders", {"symbol": "BTCUSDT", "limit": "1"}),
    ("all_orders", {"symbol": "BTCUSDT", "limit": True}),
])
def test_invalid_inputs_never_reach_network(client, monkeypatch, name, args):
    mock_binance(monkeypatch, lambda request: pytest.fail("unexpected network call"))
    result = call(client, name, args)
    assert result["isError"] and result["structuredContent"]["code"] == "BAD_ARGUMENTS"


def test_unknown_tool_is_not_executable(client):
    assert call(client, "place_order", {"symbol": "BTCUSDT"})["structuredContent"]["code"] == "UNKNOWN_TOOL"


@pytest.mark.parametrize("kind", ["http", "network", "json", "echo", "redirect", "huge"])
def test_errors_do_not_leak_credentials_or_signed_url(client, monkeypatch, caplog, kind):
    monkeypatch.setenv("BINANCE_API_KEY", "synthetic-binance-key")
    monkeypatch.setenv("BINANCE_API_SECRET", "synthetic-binance-secret")
    captured = []
    def handler(req):
        captured.append(str(req.url))
        if kind == "network":
            raise httpx.ConnectError("secret=" + str(req.url), request=req)
        if kind == "http":
            return httpx.Response(400, text="synthetic-binance-key " + str(req.url))
        if kind == "json":
            return httpx.Response(200, text="bad synthetic-binance-secret")
        if kind == "redirect":
            return httpx.Response(302, headers={"Location": "https://evil.invalid"})
        if kind == "huge":
            return httpx.Response(200, content=b" " * 5_000_001)
        return httpx.Response(200, json={"echo": str(req.url), "key": "synthetic-binance-key"})
    mock_binance(monkeypatch, handler)
    with caplog.at_level(logging.DEBUG):
        result = call(client, "account")
    assert result["isError"] and len(captured) == 1
    output = json.dumps(result) + caplog.text
    assert "synthetic-binance-key" not in output and "synthetic-binance-secret" not in output
    assert "signature=" not in output and captured[0] not in output


def test_httpcore_wire_loggers_are_disabled():
    for name in ("httpx", "httpcore.http11", "httpcore.http2", "httpcore.connection", "httpcore.proxy", "httpcore.socks"):
        assert logging.getLogger(name).disabled
