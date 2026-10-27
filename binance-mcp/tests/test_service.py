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
    service.rate_limiter.reset()
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
    original = getattr(service.httpx.AsyncClient, "_original", service.httpx.AsyncClient)
    def make_client(**kwargs):
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        return original(transport=httpx.MockTransport(handler), **kwargs)
    make_client._original = original
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
    response = rpc(client, "tools/call", {"name": "place_order", "arguments": {"symbol": "BTCUSDT"}}).json()
    assert response["error"]["code"] == -32602
    assert "Unknown tool" in response["error"]["message"]


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


def test_bearer_scheme_case_insensitive(client):
    for header_val in ["Bearer synthetic-mcp", "bearer synthetic-mcp", "BEARER synthetic-mcp", "bEaReR synthetic-mcp"]:
        resp = client.post("/mcp", headers={"Authorization": header_val}, json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert resp.status_code == 200 and resp.json()["result"] == {}
    for header_val in ["Basic synthetic-mcp", "Token synthetic-mcp", "Bearer wrong-key", "Bearer"]:
        resp = client.post("/mcp", headers={"Authorization": header_val}, json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert resp.status_code == 401


def test_origin_validation_and_default_bind(client, monkeypatch):
    # Allowed origins
    resp = client.post("/mcp", headers={"Authorization": "Bearer synthetic-mcp"},
                       json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert resp.status_code == 200
    for origin in ["https://zendesk.smlershou.top", "http://localhost:8124", "https://127.0.0.1:8124", "http://[::1]:8124"]:
        resp = client.post("/mcp", headers={"Authorization": "Bearer synthetic-mcp", "Origin": origin},
                           json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert resp.status_code == 200
    # Custom origin via env
    monkeypatch.setenv("BINANCE_MCP_ALLOWED_ORIGINS", "https://custom.example.com")
    resp = client.post("/mcp", headers={"Authorization": "Bearer synthetic-mcp", "Origin": "https://custom.example.com"},
                       json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert resp.status_code == 200
    # Disallowed origins
    for origin in ["https://attacker.example.com", "http://malicious.org", "null",
                   "http://zendesk.smlershou.top", "https://zendesk.smlershou.top.evil.com"]:
        resp = client.post("/mcp", headers={"Authorization": "Bearer synthetic-mcp", "Origin": origin},
                           json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert resp.status_code == 403

    # Default bind host in main is 127.0.0.1
    calls = []
    monkeypatch.delenv("BINANCE_MCP_HOST", raising=False)
    monkeypatch.delenv("BINANCE_MCP_PORT", raising=False)
    monkeypatch.setattr(service.uvicorn, "run", lambda app, host, port, access_log: calls.append((host, port)))
    service.main()
    assert calls == [("127.0.0.1", 8124)]


def test_jsonrpc_batch_and_notifications(client):
    # Batch with multiple calls
    batch_req = [
        {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "id": 2, "method": "initialize"},
    ]
    resp = client.post("/mcp", headers={"Authorization": "Bearer synthetic-mcp"}, json=batch_req)
    assert resp.status_code == 200
    results = resp.json()
    assert len(results) == 2
    assert results[0] == {"jsonrpc": "2.0", "id": 1, "result": {}}
    assert results[1]["result"]["serverInfo"]["name"] == "binance-mcp"

    # Batch with mixed notifications and requests
    batch_mixed = [
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 10, "method": "ping"},
    ]
    resp = client.post("/mcp", headers={"Authorization": "Bearer synthetic-mcp"}, json=batch_mixed)
    assert resp.status_code == 200
    results = resp.json()
    assert len(results) == 1
    assert results[0] == {"jsonrpc": "2.0", "id": 10, "result": {}}

    # Batch with only notifications -> 202
    batch_notif = [
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "method": "notifications/progress"},
    ]
    resp = client.post("/mcp", headers={"Authorization": "Bearer synthetic-mcp"}, json=batch_notif)
    assert resp.status_code == 202

    # Single notification -> 202
    resp = client.post("/mcp", headers={"Authorization": "Bearer synthetic-mcp"},
                       json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert resp.status_code == 202

    # Batch with invalid items
    batch_invalid = [
        {"invalid": "item"},
        {"jsonrpc": "2.0", "id": 99, "method": "ping"},
    ]
    resp = client.post("/mcp", headers={"Authorization": "Bearer synthetic-mcp"}, json=batch_invalid)
    assert resp.status_code == 200
    results = resp.json()
    assert len(results) == 2
    assert results[0]["error"]["code"] == -32600
    assert results[1]["result"] == {}


def test_rate_limiting_retry_after_and_circuit_breaker(client, monkeypatch):
    # Test 429 returns RATE_LIMITED and safe retry_after
    calls = []
    def handler_429(req):
        calls.append(req)
        return httpx.Response(429, headers={"Retry-After": "45"}, text="Too Many Requests")
    mock_binance(monkeypatch, handler_429)

    res = call(client, "price", {"symbol": "BTCUSDT"})
    assert res["isError"]
    assert res["structuredContent"]["code"] == "RATE_LIMITED"
    assert res["structuredContent"]["retry_after"] == 45
    assert len(calls) == 1

    # While cooldown is active, subsequent requests return RATE_LIMITED without hitting network
    res2 = call(client, "price", {"symbol": "BTCUSDT"})
    assert res2["isError"]
    assert res2["structuredContent"]["code"] == "RATE_LIMITED"
    assert len(calls) == 1  # No new network call!

    # Test 418 IP ban triggers circuit breaker without auto-retry
    service.rate_limiter.reset()
    calls.clear()
    def handler_418(req):
        calls.append(req)
        return httpx.Response(418, headers={"Retry-After": "120"}, text="I'm a teapot (IP banned)")
    mock_binance(monkeypatch, handler_418)

    res_ban = call(client, "price", {"symbol": "BTCUSDT"})
    assert res_ban["isError"]
    assert res_ban["structuredContent"]["code"] == "IP_BANNED"
    assert res_ban["structuredContent"]["retry_after"] == 120
    assert len(calls) == 1

    # Circuit breaker blocks subsequent requests immediately without contacting Binance
    res_blocked = call(client, "price", {"symbol": "BTCUSDT"})
    assert res_blocked["isError"]
    assert res_blocked["structuredContent"]["code"] == "IP_BANNED"
    assert len(calls) == 1  # Still 1! No network call.


def test_request_weight_tracking_and_high_weight_limiting(client, monkeypatch):
    assert service.get_tool_weight("depth", {"limit": 100}) == 5
    assert service.get_tool_weight("depth", {"limit": 500}) == 25
    assert service.get_tool_weight("depth", {"limit": 1000}) == 50
    assert service.get_tool_weight("depth", {"limit": 5000}) == 250
    service.rate_limiter.reset()
    monkeypatch.setattr(service.rate_limiter, "_get_max_weight", lambda: 300)
    calls = []
    def handler(req):
        calls.append(req)
        return httpx.Response(200, json={"bids": [], "asks": []})
    mock_binance(monkeypatch, handler)

    # depth limit=5000 weighs 250, so one call fits in a 300 budget.
    res1 = call(client, "depth", {"symbol": "BTCUSDT", "limit": 5000})
    assert not res1["isError"]
    assert len(calls) == 1

    # A second depth call needs another 250, and only 50 remains.
    res2 = call(client, "depth", {"symbol": "BTCUSDT", "limit": 5000})
    assert res2["isError"]
    assert res2["structuredContent"]["code"] == "RATE_LIMITED"
    assert "rate limit reached" in res2["structuredContent"]["error"]
    assert len(calls) == 1

    # price weighs 2 and still fits in the remaining budget.
    def handler_price(req):
        calls.append(req)
        return httpx.Response(200, json={"symbol": "BTCUSDT", "price": "50000"})
    mock_binance(monkeypatch, handler_price)
    res3 = call(client, "price", {"symbol": "BTCUSDT"})
    assert not res3["isError"]
    assert len(calls) == 2


def test_binance_ban_duration_is_taken_from_retry_after(client, monkeypatch):
    service.rate_limiter.reset()
    calls = []
    def handler(req):
        calls.append(req)
        return httpx.Response(418, headers={"Retry-After": "172800"}, text="banned")
    mock_binance(monkeypatch, handler)
    banned = call(client, "price", {"symbol": "BTCUSDT"})
    assert banned["structuredContent"]["code"] == "IP_BANNED"
    assert banned["structuredContent"]["retry_after"] == 172800
    assert call(client, "price", {"symbol": "BTCUSDT"})["structuredContent"]["code"] == "IP_BANNED"
    assert len(calls) == 1

    service.rate_limiter.reset()
    calls.clear()
    def handler_missing(req):
        calls.append(req)
        return httpx.Response(429, text="slow down")
    mock_binance(monkeypatch, handler_missing)
    limited = call(client, "price", {"symbol": "BTCUSDT"})
    assert limited["structuredContent"]["code"] == "RATE_LIMITED"
    assert limited["structuredContent"]["retry_after"] == 60
    assert "slow down" not in json.dumps(limited)


def test_retry_after_header_is_parsed_safely(client, monkeypatch):
    service.rate_limiter.reset()
    def handler(req):
        return httpx.Response(429, headers={"Retry-After": "not-a-number"}, text="upstream-secret")
    mock_binance(monkeypatch, handler)
    result = call(client, "price", {"symbol": "BTCUSDT"})
    rendered = json.dumps(result)
    assert result["structuredContent"]["retry_after"] == 60
    assert "not-a-number" not in rendered and "upstream-secret" not in rendered
    assert service.parse_retry_after("999999999") == service.MAX_RETRY_AFTER
    assert service.parse_retry_after("45\r\nSet-Cookie: x") == 60
    assert service.parse_retry_after(None, default=120) == 120


def test_active_ip_ban_is_not_replaced_by_shorter_429():
    limiter = service.RateLimiter()
    limiter.record_cooldown(418, 300)
    until = limiter.circuit_break_until
    limiter.record_cooldown(429, 30)
    assert limiter.is_banned and limiter.circuit_break_until == until
    limiter.record_cooldown(429, 3000)
    assert limiter.is_banned and limiter.circuit_break_until > until


def test_missing_credentials_do_not_consume_weight_or_mask_ban(client, monkeypatch):
    service.rate_limiter.reset()
    service.rate_limiter.record_cooldown(418, 120)
    denied = call(client, "account")
    assert denied["isError"] and denied["structuredContent"]["code"] == "CREDENTIALS_MISSING"
    service.rate_limiter.reset()
    monkeypatch.setattr(service.rate_limiter, "_get_max_weight", lambda: 5)
    assert call(client, "account")["structuredContent"]["code"] == "CREDENTIALS_MISSING"
    def handler(req):
        return httpx.Response(200, json={"symbol": "BTCUSDT", "price": "42"})
    mock_binance(monkeypatch, handler)
    assert not call(client, "price", {"symbol": "BTCUSDT"})["isError"]


def test_concurrency_cap():
    limiter = service.RateLimiter(max_concurrency=2)
    assert limiter.semaphore._value == 2
