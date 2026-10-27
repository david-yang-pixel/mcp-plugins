import asyncio
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
    init_params = {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "test-client", "version": "1.0.0"}}
    assert rpc(client, "initialize", init_params).json()["result"]["serverInfo"]["name"] == "binance-mcp"
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
    for tool, args in [("account", {}), ("open_orders", {"symbol": "BTCUSDT"}), ("all_orders", {"symbol": "BTCUSDT"}),
                       ("wallet_balance", {}), ("funding_assets", {}), ("my_trades", {"symbol": "BTCUSDT"}),
                       ("position_cost", {"symbol": "BTCUSDT"})]:
        result = call(client, tool, args)
        assert result["isError"] and result["structuredContent"]["code"] == "CREDENTIALS_MISSING"
    assert len(requests) == 1 and "X-MBX-APIKEY" not in requests[0].headers
    monkeypatch.setenv("BINANCE_API_KEY", "fake-key")
    assert call(client, "account")["structuredContent"]["code"] == "CREDENTIALS_MISSING"


@pytest.mark.parametrize("name,args,path,signed,method", [
    ("exchange_info", {"symbol": "BTCUSDT"}, "/api/v3/exchangeInfo", False, "GET"),
    ("price", {"symbol": "BTCUSDT"}, "/api/v3/ticker/price", False, "GET"),
    ("ticker_24h", {"symbol": "BTCUSDT"}, "/api/v3/ticker/24hr", False, "GET"),
    ("depth", {"symbol": "BTCUSDT", "limit": 5000}, "/api/v3/depth", False, "GET"),
    ("klines", {"symbol": "BTCUSDT", "interval": "1M", "limit": 1000}, "/api/v3/klines", False, "GET"),
    ("account", {}, "/api/v3/account", True, "GET"),
    ("open_orders", {"symbol": "BTCUSDT"}, "/api/v3/openOrders", True, "GET"),
    ("all_orders", {"symbol": "BTCUSDT", "limit": 1000}, "/api/v3/allOrders", True, "GET"),
    ("wallet_balance", {"quoteAsset": "BTC"}, "/sapi/v1/asset/wallet/balance", True, "GET"),
    ("funding_assets", {"asset": "USDT"}, "/sapi/v1/asset/get-funding-asset", True, "POST"),
    ("my_trades", {"symbol": "BTCUSDT", "limit": 1000, "fromId": 0}, "/api/v3/myTrades", True, "GET"),
])
def test_endpoints_methods_and_signature(client, monkeypatch, name, args, path, signed, method):
    monkeypatch.setenv("BINANCE_API_KEY", "synthetic-binance-key")
    monkeypatch.setenv("BINANCE_API_SECRET", "synthetic-binance-secret")
    monkeypatch.setattr(service.time, "time", lambda: 1700000000.123)
    def handler(req):
        assert req.method == method and req.url.host == "api.binance.com" and req.url.scheme == "https" and req.url.path == path
        query = req.url.query.decode()
        if signed:
            raw, sig = query.rsplit("&signature=", 1)
            assert sig == hmac.new(b"synthetic-binance-secret", raw.encode(), hashlib.sha256).hexdigest()
            parsed = parse_qs(raw)
            assert parsed["timestamp"] == ["1700000000123"] and parsed["recvWindow"] == ["5000"]
            assert req.headers["X-MBX-APIKEY"] == "synthetic-binance-key" and req.content == b""
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
    ("wallet_balance", {"quoteAsset": "usdt"}), ("wallet_balance", {"quoteAsset": ""}),
    ("wallet_balance", {"quoteAsset": "USDT", "needBtcValuation": True}),
    ("funding_assets", {"asset": "USD T"}), ("funding_assets", {"asset": 1}),
    ("funding_assets", {"needBtcValuation": "true"}), ("funding_assets", {"needBtcValuation": 1}),
    ("my_trades", {}), ("my_trades", {"symbol": "BTCUSDT", "fromId": -1}),
    ("my_trades", {"symbol": "BTCUSDT", "fromId": "1"}), ("my_trades", {"symbol": "BTCUSDT", "fromId": True}),
    ("my_trades", {"symbol": "BTCUSDT", "limit": 1001}),
    ("position_cost", {}), ("position_cost", {"symbol": "BTCUSDT", "fromId": 0}),
])
def test_invalid_inputs_never_reach_network(client, monkeypatch, name, args):
    mock_binance(monkeypatch, lambda request: pytest.fail("unexpected network call"))
    resp = rpc(client, "tools/call", {"name": name, "arguments": args}).json()
    assert resp["error"]["code"] == -32602
    assert "Invalid tool arguments" in resp["error"]["message"]


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


@pytest.mark.parametrize("status,body,code,hint", [
    (400, {"code": -1021, "msg": "synthetic-binance-key Timestamp outside recvWindow"}, -1021, "server clock"),
    (401, {"code": -2015, "msg": "Invalid API-key, IP, or permissions for action."}, -2015, "IP allowlist"),
    (400, {"code": -1100, "msg": "Illegal characters"}, -1100, None),
    (451, None, None, "this server's location"),
    (400, {"code": "-1021", "msg": "x"}, None, None),
    (400, {"code": True, "msg": "x"}, None, None),
    (400, [{"code": -1021}], None, None),
])
def test_http_errors_return_binance_code_without_message(client, monkeypatch, status, body, code, hint):
    monkeypatch.setenv("BINANCE_API_KEY", "synthetic-binance-key")
    monkeypatch.setenv("BINANCE_API_SECRET", "synthetic-binance-secret")
    mock_binance(monkeypatch, lambda req: httpx.Response(status, json=body) if body is not None else httpx.Response(status))
    payload = call(client, "account")["structuredContent"]
    assert payload["success"] is False and payload["code"] == "BINANCE_HTTP_ERROR"
    assert payload["http_status"] == status and payload.get("binance_code") == code
    assert (hint in payload["error"]) if hint else (":" not in payload["error"])
    assert "msg" not in payload and "synthetic-binance-key" not in json.dumps(payload)
    assert "Timestamp outside" not in json.dumps(payload) and "Illegal" not in json.dumps(payload)


def test_oversized_error_body_is_not_parsed(client, monkeypatch):
    body = json.dumps({"code": -1021, "pad": "x" * 70_000})
    mock_binance(monkeypatch, lambda req: httpx.Response(400, text=body))
    payload = call(client, "price", {"symbol": "BTCUSDT"})["structuredContent"]
    assert payload["http_status"] == 400 and "binance_code" not in payload


def test_success_status_with_error_body_returns_binance_code(client, monkeypatch):
    mock_binance(monkeypatch, lambda req: httpx.Response(200, json={"code": -1121, "msg": "Invalid symbol."}))
    payload = call(client, "price", {"symbol": "BTCUSDT"})["structuredContent"]
    assert payload == {"success": False, "code": "BINANCE_RESPONSE_ERROR",
                       "error": "Binance returned an error", "binance_code": -1121}


@pytest.mark.parametrize("name,args,expected", [
    ("wallet_balance", {}, {"quoteAsset": ["USDT"]}),
    ("wallet_balance", {"quoteAsset": "EUR"}, {"quoteAsset": ["EUR"]}),
    ("funding_assets", {}, {"needBtcValuation": ["true"]}),
    ("funding_assets", {"asset": "MRVLB", "needBtcValuation": False}, {"asset": ["MRVLB"], "needBtcValuation": ["false"]}),
    ("funding_assets", {"asset": "T"}, {"asset": ["T"], "needBtcValuation": ["true"]}),
])
def test_wallet_tool_query_parameters(client, monkeypatch, name, args, expected):
    monkeypatch.setenv("BINANCE_API_KEY", "synthetic-binance-key")
    monkeypatch.setenv("BINANCE_API_SECRET", "synthetic-binance-secret")
    seen = []
    def handler(req):
        raw = req.url.query.decode().rsplit("&signature=", 1)[0]
        seen.append({k: v for k, v in parse_qs(raw).items() if k not in ("timestamp", "recvWindow")})
        return httpx.Response(200, json=[{"walletName": "Funding", "balance": "1"}])
    mock_binance(monkeypatch, handler)
    result = call(client, name, args)
    assert not result["isError"] and seen == [expected]


def test_wallet_tools_are_listed_read_only(client):
    tools = {t["name"]: t for t in rpc(client, "tools/list").json()["result"]["tools"]}
    for name in ("wallet_balance", "funding_assets"):
        assert tools[name]["annotations"]["readOnlyHint"] and not tools[name]["annotations"]["destructiveHint"]
    assert service.get_tool_weight("wallet_balance", {}) == 60
    assert service.get_tool_weight("funding_assets", {}) == 1


def trade(trade_id, is_buyer, qty, quote_qty, fee, fee_asset, at=1789436098292):
    return {"id": trade_id, "isBuyer": is_buyer, "qty": qty, "quoteQty": quote_qty, "price": "0",
            "commission": fee, "commissionAsset": fee_asset, "time": at}


def mock_position(monkeypatch, pages, price="233.76", requests=None):
    monkeypatch.setenv("BINANCE_API_KEY", "synthetic-binance-key")
    monkeypatch.setenv("BINANCE_API_SECRET", "synthetic-binance-secret")
    pages = list(pages)
    def handler(req):
        if req.url.path == "/api/v3/exchangeInfo":
            return httpx.Response(200, json={"symbols": [{"symbol": "MRVLBUSDT", "baseAsset": "MRVLB", "quoteAsset": "USDT"}]})
        if req.url.path == "/api/v3/myTrades":
            if requests is not None:
                requests.append(parse_qs(req.url.query.decode().rsplit("&signature=", 1)[0]))
            return httpx.Response(200, json=pages.pop(0))
        if req.url.path == "/api/v3/ticker/price":
            return httpx.Response(200, json={"symbol": "MRVLBUSDT", "price": price}) if price else httpx.Response(500)
        pytest.fail(f"unexpected path {req.url.path}")
    mock_binance(monkeypatch, handler)


def test_position_cost_fifo_lots_fees_and_pnl(client, monkeypatch):
    from decimal import Decimal as D
    trades = [
        trade(1, True, "0.707", "156.21872", "0.000707", "MRVLB", 1789436098292),
        trade(2, False, "0.042", "9.6957", "0.0096957", "USDT", 1789567618653),
        trade(3, False, "0.393", "90.72405", "0.0001", "BNB", 1789567618654),
        trade(4, False, "0.271", "62.56035", "0", "BNB", 1789567618655),
        trade(5, True, "0.042", "9.88428", "0.000042", "MRVLB", 1792378252000),
        trade(6, True, "0.134", "31.53556", "0", "BNB", 1792387698092),
        trade(7, True, "0.122", "28.71148", "0.0002", "BNB", 1792387698870),
        trade(8, True, "0.235", "55.30490", "0", "BNB", 1792387698871),
    ]
    requests = []
    mock_position(monkeypatch, [list(reversed(trades))], requests=requests)
    result = call(client, "position_cost", {"symbol": "MRVLBUSDT"})
    assert not result["isError"]
    data = result["structuredContent"]["data"]
    assert requests == [{"symbol": ["MRVLBUSDT"], "limit": ["1000"], "fromId": ["0"],
                         "timestamp": requests[0]["timestamp"], "recvWindow": ["5000"]}]

    first_unit = D("156.21872") / D("0.706293")
    realized = D("9.6957") - D("0.0096957") + D("90.72405") + D("62.56035") - first_unit * D("0.706")
    open_cost = first_unit * D("0.000293") + D("9.88428") + D("31.53556") + D("28.71148") + D("55.30490")
    open_qty = D("0.000293") + D("0.041958") + D("0.134") + D("0.122") + D("0.235")
    q = lambda d: str(d.quantize(D("0.00000001")))
    assert data["base_asset"] == "MRVLB" and data["quote_asset"] == "USDT" and data["method"] == "FIFO"
    assert data["trade_count"] == 8 and data["first_trade_at"] == "2026-09-15T01:34:58Z"
    assert data["open_quantity"] == q(open_qty) and data["open_cost"] == q(open_cost)
    assert data["average_cost"] == q(open_cost / open_qty) and data["realized_pnl"] == q(realized)
    assert data["unmatched_sell_quantity"] == "0.00000000"
    assert data["fees_in_other_assets"] == {"BNB": "0.00030000"}
    assert [lot["bought_at"] for lot in data["open_lots"]] == [
        "2026-09-15T01:34:58Z", "2026-10-19T02:50:52Z", "2026-10-19T05:28:18Z",
        "2026-10-19T05:28:18Z", "2026-10-19T05:28:18Z"]
    assert data["open_lots"][0]["quantity"] == "0.00029300" and data["open_lots"][1]["quantity"] == "0.04195800"
    value = open_qty * D("233.76")
    assert data["current_price"] == "233.76000000" and data["market_value"] == q(value)
    assert data["unrealized_pnl"] == q(value - open_cost)
    assert data["unrealized_pnl_pct"] == q((value - open_cost) / open_cost * 100)


def test_position_cost_pages_through_full_history(client, monkeypatch):
    first = [trade(i, True, "1", "10", "0", "USDT") for i in range(1, 1001)]
    second = [trade(5000, False, "1000", "20000", "0", "USDT"), trade(5001, True, "2", "60", "0", "USDT")]
    requests = []
    mock_position(monkeypatch, [first, second], price="40", requests=requests)
    data = call(client, "position_cost", {"symbol": "MRVLBUSDT"})["structuredContent"]["data"]
    assert [r["fromId"] for r in requests] == [["0"], ["1001"]]
    assert data["trade_count"] == 1002 and data["realized_pnl"] == "10000.00000000"
    assert data["open_quantity"] == "2.00000000" and data["average_cost"] == "30.00000000"
    assert data["unrealized_pnl"] == "20.00000000"


def test_position_cost_unmatched_sells_and_missing_price(client, monkeypatch):
    mock_position(monkeypatch, [[trade(1, True, "1", "10", "0", "USDT"), trade(2, False, "3", "60", "0", "USDT")]], price=None)
    data = call(client, "position_cost", {"symbol": "MRVLBUSDT"})["structuredContent"]["data"]
    # Only the 1 unit bought here has a cost; the other 2 came from elsewhere.
    assert data["realized_pnl"] == "10.00000000" and data["unmatched_sell_quantity"] == "2.00000000"
    assert data["open_quantity"] == "0.00000000" and data["average_cost"] is None and data["open_lots"] == []
    assert data["current_price"] is None and data["unrealized_pnl"] is None and data["price_error"] == "BINANCE_HTTP_ERROR"


def test_position_cost_without_trades(client, monkeypatch):
    mock_position(monkeypatch, [[]])
    data = call(client, "position_cost", {"symbol": "MRVLBUSDT"})["structuredContent"]["data"]
    assert data["trade_count"] == 0 and data["first_trade_at"] is None and data["open_quantity"] == "0.00000000"
    assert data["unrealized_pnl"] == "0.00000000" and data["unrealized_pnl_pct"] is None


def test_position_cost_truncates_lots_keeping_fifo_head(client, monkeypatch):
    count = service.MAX_OPEN_LOTS + 50
    mock_position(monkeypatch, [[trade(i, True, "1", str(i + 1), "0", "USDT", 1789436098000 + i * 1000) for i in range(count)]])
    data = call(client, "position_cost", {"symbol": "MRVLBUSDT"})["structuredContent"]["data"]
    assert data["open_lots_truncated"] is True and len(data["open_lots"]) == service.MAX_OPEN_LOTS
    assert data["open_lots"][0]["unit_cost"] == "1.00000000"
    assert data["open_lots"][-1]["unit_cost"] == f"{service.MAX_OPEN_LOTS}.00000000"
    assert data["open_quantity"] == f"{count}.00000000"


def test_position_cost_stops_at_page_limit(client, monkeypatch):
    pages = [[trade(p * 1000 + i, True, "1", "1", "0", "USDT") for i in range(1000)] for p in range(service.MAX_TRADE_PAGES)]
    requests = []
    mock_position(monkeypatch, pages, requests=requests)
    payload = call(client, "position_cost", {"symbol": "MRVLBUSDT"})["structuredContent"]
    assert payload["code"] == "TOO_MANY_TRADES" and len(requests) == service.MAX_TRADE_PAGES


@pytest.mark.parametrize("bad", [
    {"qty": "abc"}, {"isBuyer": "true"}, {"time": "1789436098292"}, {"qty": "-1"}, {"commissionAsset": None, "commission": None},
])
def test_position_cost_rejects_malformed_trades(client, monkeypatch, bad):
    mock_position(monkeypatch, [[{**trade(1, True, "1", "10", "0", "USDT"), **bad}]])
    payload = call(client, "position_cost", {"symbol": "MRVLBUSDT"})["structuredContent"]
    assert payload == {"success": False, "code": "BINANCE_RESPONSE_ERROR", "error": "Invalid Binance trade data"}


def test_position_cost_propagates_binance_errors(client, monkeypatch):
    monkeypatch.setenv("BINANCE_API_KEY", "synthetic-binance-key")
    monkeypatch.setenv("BINANCE_API_SECRET", "synthetic-binance-secret")
    def handler(req):
        if req.url.path == "/api/v3/exchangeInfo":
            return httpx.Response(200, json={"symbols": [{"baseAsset": "MRVLB", "quoteAsset": "USDT"}]})
        return httpx.Response(401, json={"code": -2015, "msg": "Invalid API-key"})
    mock_binance(monkeypatch, handler)
    payload = call(client, "position_cost", {"symbol": "MRVLBUSDT"})["structuredContent"]
    assert payload["code"] == "BINANCE_HTTP_ERROR" and payload["binance_code"] == -2015


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
    # Allowed origins. Local hosts keep any port; the public host is compared as scheme/host/effective port.
    resp = client.post("/mcp", headers={"Authorization": "Bearer synthetic-mcp"},
                       json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert resp.status_code == 200
    for origin in ["https://zendesk.smlershou.top", "https://zendesk.smlershou.top:443",
                   "https://ZENDESK.smlershou.top", "https://zendesk.smlershou.top:0443",
                   "http://localhost", "http://localhost:8124", "https://127.0.0.1",
                   "https://127.0.0.1:8124", "http://[::1]", "http://[::1]:8124"]:
        assert service.is_allowed_origin(origin)
        resp = client.post("/mcp", headers={"Authorization": "Bearer synthetic-mcp", "Origin": origin},
                           json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert resp.status_code == 200, origin
    # Custom origin via env, including default-port equivalence and host case.
    monkeypatch.setenv("BINANCE_MCP_ALLOWED_ORIGINS", "https://Custom.Example.com")
    for origin in ["https://custom.example.com", "https://custom.example.com:443", "https://Custom.Example.com"]:
        assert service.is_allowed_origin(origin)
        resp = client.post("/mcp", headers={"Authorization": "Bearer synthetic-mcp", "Origin": origin},
                           json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert resp.status_code == 200, origin
    # Disallowed origins: wrong port, scheme, path, query, userinfo, or domain.
    for origin in ["https://attacker.example.com", "http://malicious.org", "null",
                   "http://zendesk.smlershou.top", "https://zendesk.smlershou.top:8443",
                   "https://zendesk.smlershou.top/", "https://zendesk.smlershou.top/mcp",
                   "https://zendesk.smlershou.top?foo=bar", "https://zendesk.smlershou.top?",
                   "https://zendesk.smlershou.top#", "https://zendesk.smlershou.top:443?",
                   "https://user:pass@zendesk.smlershou.top", "https://@zendesk.smlershou.top",
                   "https://zendesk.smlershou.top.evil.com", "http://[::1]:99999",
                   "http://localhost:8124/mcp", "http://localhost:", "http://localhost:0",
                   " https://zendesk.smlershou.top", "https://custom.example.com:8443",
                   "https://custom.example.com/mcp", "https://evil.com\\@zendesk.smlershou.top"]:
        assert not service.is_allowed_origin(origin)
        resp = client.post("/mcp", headers={"Authorization": "Bearer synthetic-mcp", "Origin": origin},
                           json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert resp.status_code == 403, origin

    # Default bind host in main is 127.0.0.1
    calls = []
    monkeypatch.delenv("BINANCE_MCP_HOST", raising=False)
    monkeypatch.delenv("BINANCE_MCP_PORT", raising=False)
    monkeypatch.setattr(service.uvicorn, "run", lambda app, host, port, access_log: calls.append((host, port)))
    service.main()
    assert calls == [("127.0.0.1", 8124)]


def test_jsonrpc_batch_and_notifications(client):
    # Batch with multiple valid calls (no initialize)
    batch_req = [
        {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    resp = client.post("/mcp", headers={"Authorization": "Bearer synthetic-mcp"}, json=batch_req)
    assert resp.status_code == 200
    results = resp.json()
    assert len(results) == 2
    assert results[0] == {"jsonrpc": "2.0", "id": 1, "result": {}}
    assert "tools" in results[1]["result"]

    # Batch containing initialize is rejected entirely without executing any entry
    batch_with_init = [
        {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "c", "version": "1"}
        }},
    ]
    resp_init = client.post("/mcp", headers={"Authorization": "Bearer synthetic-mcp"}, json=batch_with_init)
    assert resp_init.status_code == 200
    init_results = resp_init.json()
    assert init_results["id"] is None
    assert init_results["error"]["code"] == -32600
    assert "cannot be part of a batch" in init_results["error"]["message"]

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


def test_queued_request_rechecks_ban_before_calling_binance(monkeypatch):
    async def run_test():
        service.rate_limiter.reset()
        service.rate_limiter.semaphore = asyncio.Semaphore(1)
        binance_calls = 0
        entered = asyncio.Event()
        release = asyncio.Event()

        async def handler(_request):
            nonlocal binance_calls
            binance_calls += 1
            entered.set()
            await release.wait()
            return httpx.Response(418, headers={"Retry-After": "120"}, text="banned")

        original = getattr(service.httpx.AsyncClient, "_original", service.httpx.AsyncClient)

        def make_client(**kwargs):
            assert kwargs.get("trust_env") is False and kwargs.get("follow_redirects") is False

            class Transport(httpx.AsyncBaseTransport):
                async def handle_async_request(self, request):
                    return await handler(request)

            return original(transport=Transport(), **kwargs)

        make_client._original = original
        monkeypatch.setattr(service.httpx, "AsyncClient", make_client)

        first = asyncio.create_task(service.call_tool("price", {"symbol": "BTCUSDT"}))
        await asyncio.wait_for(entered.wait(), timeout=2)
        second = asyncio.create_task(service.call_tool("price", {"symbol": "BTCUSDT"}))
        queued = False
        for _ in range(100):
            if service.rate_limiter.semaphore._waiters:
                queued = True
                break
            await asyncio.sleep(0)
        assert queued, "second request never queued on the concurrency semaphore"
        release.set()
        result1, result2 = await asyncio.gather(first, second)
        assert binance_calls == 1
        assert result1["code"] == "IP_BANNED"
        assert result2["code"] == "IP_BANNED"
        assert len(service.rate_limiter.history) == 1

    try:
        asyncio.run(run_test())
    finally:
        service.rate_limiter.reset()


def test_signed_request_generates_timestamp_after_queue_wait(monkeypatch):
    async def run_test():
        service.rate_limiter.reset()
        service.rate_limiter.semaphore = asyncio.Semaphore(1)
        monkeypatch.setenv("BINANCE_API_KEY", "test-api-key")
        monkeypatch.setenv("BINANCE_API_SECRET", "test-api-secret")

        first_entered = asyncio.Event()
        release_first = asyncio.Event()
        captured_requests = []

        # Virtual clock starting at 1000.0s
        simulated_time = 1000.0

        def fake_time():
            return simulated_time

        monkeypatch.setattr(service.time, "time", fake_time)

        async def handler(request):
            if "api/v3/ticker/price" in str(request.url):
                first_entered.set()
                await release_first.wait()
                return httpx.Response(200, json={"symbol": "BTCUSDT", "price": "50000"})
            captured_requests.append(request)
            return httpx.Response(200, json={"makerCommission": 15, "takerCommission": 15})

        original = getattr(service.httpx.AsyncClient, "_original", service.httpx.AsyncClient)

        def make_client(**kwargs):
            class Transport(httpx.AsyncBaseTransport):
                async def handle_async_request(self, request):
                    return await handler(request)

            return original(transport=Transport(), **kwargs)

        make_client._original = original
        monkeypatch.setattr(service.httpx, "AsyncClient", make_client)

        # First request acquires slot
        first_task = asyncio.create_task(service.call_tool("price", {"symbol": "BTCUSDT"}))
        await asyncio.wait_for(first_entered.wait(), timeout=2)

        # Second task starts while semaphore is locked, queues for > 5 seconds (simulated 10s delay)
        second_task = asyncio.create_task(service.call_tool("account", {}))

        queued = False
        for _ in range(100):
            if service.rate_limiter.semaphore._waiters:
                queued = True
                break
            await asyncio.sleep(0)
        assert queued, "second request never queued on the concurrency semaphore"

        # Advance time by 10 seconds while queued (> recvWindow of 5000ms)
        simulated_time = 1010.0

        release_first.set()
        res1, res2 = await asyncio.gather(first_task, second_task)
        assert res1["success"] is True
        assert res2["success"] is True

        assert len(captured_requests) == 1
        signed_req = captured_requests[0]
        params = parse_qs(signed_req.url.query.decode("ascii"))
        ts = int(params["timestamp"][0])
        # Timestamp must be generated after queue wait, reflecting the new time 1010.0 * 1000
        assert ts == 1010000
        assert int(params["recvWindow"][0]) == 5000

        # Signature must match the query containing timestamp=1010000
        expected_query = f"timestamp={ts}&recvWindow=5000"
        expected_sig = hmac.new(b"test-api-secret", expected_query.encode(), hashlib.sha256).hexdigest()
        assert params["signature"][0] == expected_sig

    try:
        asyncio.run(run_test())
    finally:
        service.rate_limiter.reset()


def test_batch_with_initialize_rejects_without_executing_sibling_tools(client, monkeypatch):
    called = False

    def fake_handler(_request):
        nonlocal called
        called = True
        return httpx.Response(200, json={"symbol": "BTCUSDT", "price": "100"})

    mock_binance(monkeypatch, fake_handler)

    batch = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "c", "version": "1"}
        }},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": "price", "arguments": {"symbol": "BTCUSDT"}
        }}
    ]
    resp = client.post("/mcp", headers={"Authorization": "Bearer synthetic-mcp"}, json=batch)
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, dict)
    assert data["id"] is None
    assert data["error"]["code"] == -32600
    assert "cannot be part of a batch" in data["error"]["message"]
    assert not called, "Sibling tools/call in batch containing initialize must not be executed"


def test_ping_tools_list_reject_non_object_array_params(client):
    headers = {"Authorization": "Bearer synthetic-mcp"}
    # Non-object, non-array params should be rejected with -32602
    for bad_params in [7, "invalid", True, False, 3.14]:
        for method in ["ping", "tools/list", "unknown_method"]:
            resp = client.post("/mcp", headers=headers, json={
                "jsonrpc": "2.0", "id": 1, "method": method, "params": bad_params
            }).json()
            assert resp["error"]["code"] == -32602
            assert resp["id"] == 1

    # Valid params: dict, array (or omitted)
    resp_ping_dict = client.post("/mcp", headers=headers, json={
        "jsonrpc": "2.0", "id": 2, "method": "ping", "params": {}
    }).json()
    assert resp_ping_dict["result"] == {}

    resp_ping_list = client.post("/mcp", headers=headers, json={
        "jsonrpc": "2.0", "id": 3, "method": "ping", "params": []
    }).json()
    assert resp_ping_list["result"] == {}

    resp_tools_dict = client.post("/mcp", headers=headers, json={
        "jsonrpc": "2.0", "id": 4, "method": "tools/list", "params": {}
    }).json()
    assert "tools" in resp_tools_dict["result"]


def test_initialize_requires_protocol_capabilities_clientinfo(client):
    valid_params = {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "test-client", "version": "1.0.0"}
    }
    # Success with all required fields
    ok_resp = rpc(client, "initialize", valid_params).json()
    assert ok_resp["result"]["serverInfo"]["name"] == "binance-mcp"

    # Missing, null, or non-object params. rpc() cannot send these because it substitutes {}.
    headers = {"Authorization": "Bearer synthetic-mcp"}
    for payload in (
        {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": None},
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": []},
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-03-26", "capabilities": [], "clientInfo": {"name": "c", "version": "1"}
        }},
    ):
        resp = client.post("/mcp", headers=headers, json=payload).json()
        assert resp["error"]["code"] == -32602

    # Missing protocolVersion
    bad1 = {"capabilities": {}, "clientInfo": {"name": "c", "version": "1"}}
    assert rpc(client, "initialize", bad1).json()["error"]["code"] == -32602

    # Missing capabilities
    bad2 = {"protocolVersion": "2025-03-26", "clientInfo": {"name": "c", "version": "1"}}
    assert rpc(client, "initialize", bad2).json()["error"]["code"] == -32602

    # Missing clientInfo or clientInfo fields
    bad3 = {"protocolVersion": "2025-03-26", "capabilities": {}}
    assert rpc(client, "initialize", bad3).json()["error"]["code"] == -32602

    bad4 = {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "c"}}
    assert rpc(client, "initialize", bad4).json()["error"]["code"] == -32602


def test_jsonrpc_id_type_validation(client):
    # Invalid id types: array, object, bool, float, None (null)
    headers = {"Authorization": "Bearer synthetic-mcp"}
    invalid_ids = [[1, 2], {"key": "val"}, True, False, 3.14, 1.0, None]
    for bad_id in invalid_ids:
        resp = client.post("/mcp", headers=headers,
                           json={"jsonrpc": "2.0", "id": bad_id, "method": "ping"}).json()
        assert resp["error"]["code"] == -32600
        assert resp["id"] is None

    # Valid id types: int, string
    for good_id in [0, 1, -1, 42, "req-xyz"]:
        resp = client.post("/mcp", headers=headers,
                           json={"jsonrpc": "2.0", "id": good_id, "method": "ping"}).json()
        assert resp["result"] == {}
        assert resp["id"] == good_id

    batch = client.post("/mcp", headers=headers, json=[
        {"jsonrpc": "2.0", "id": {"bad": True}, "method": "ping"},
        {"jsonrpc": "2.0", "id": "ok", "method": "ping"},
    ]).json()
    assert batch[0]["error"]["code"] == -32600 and batch[0]["id"] is None
    assert "bad" not in json.dumps(batch[0])
    assert batch[1] == {"jsonrpc": "2.0", "id": "ok", "result": {}}


def _asgi_post(headers, body: bytes, read_flag=None):
    status = {}

    async def receive():
        if read_flag is not None:
            read_flag["called"] = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            status["code"] = message["status"]
        elif message["type"] == "http.response.body":
            status.setdefault("body", bytearray()).extend(message.get("body", b""))

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/mcp",
        "raw_path": b"/mcp",
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
        "root_path": "",
    }

    async def run():
        await service.app(scope, receive, send)

    asyncio.run(run())
    return status["code"], bytes(status.get("body", b""))


def test_body_size_and_batch_limits(client, monkeypatch):
    headers = {"Authorization": "Bearer synthetic-mcp"}

    monkeypatch.setenv("BINANCE_MCP_MAX_BODY_BYTES", "100")
    assert client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 200

    # Content-Length exceeds limit -> 413
    large_payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping", "pad": "x" * 200})
    resp_cl = client.post("/mcp", headers={**headers, "Content-Length": str(len(large_payload))},
                          content=large_payload)
    assert resp_cl.status_code == 413

    # Declared Content-Length is over the limit even when the buffered body is tiny, and the body is not read.
    read_flag = {"called": False}
    code, _ = _asgi_post(
        [(b"authorization", b"Bearer synthetic-mcp"), (b"content-type", b"application/json"), (b"content-length", b"5000")],
        b"{}",
        read_flag,
    )
    assert code == 413 and read_flag["called"] is False

    # Invalid Content-Length is rejected before parsing.
    bad_len, _ = _asgi_post(
        [(b"authorization", b"Bearer synthetic-mcp"), (b"content-length", b"-1")],
        b"{}",
    )
    assert bad_len == 400

    # Streaming body exceeds limit without Content-Length -> 413
    code, _ = _asgi_post(
        [(b"authorization", b"Bearer synthetic-mcp"), (b"content-type", b"application/json")],
        large_payload.encode(),
    )
    assert code == 413
    def chunked():
        yield large_payload.encode()
    resp_stream = client.post("/mcp", headers=headers, content=chunked())
    assert resp_stream.status_code == 413

    # Batch count exceeds limit -> -32600; a batch at the limit still succeeds.
    monkeypatch.setenv("BINANCE_MCP_MAX_BODY_BYTES", "1000000")
    monkeypatch.setenv("BINANCE_MCP_MAX_BATCH_SIZE", "3")
    exact_batch = [{"jsonrpc": "2.0", "id": i, "method": "ping"} for i in range(3)]
    exact = client.post("/mcp", headers=headers, json=exact_batch)
    assert exact.status_code == 200 and len(exact.json()) == 3
    large_batch = [{"jsonrpc": "2.0", "id": i, "method": "ping"} for i in range(4)]
    resp_batch = client.post("/mcp", headers=headers, json=large_batch)
    assert resp_batch.status_code == 200
    batch_err = resp_batch.json()
    assert isinstance(batch_err, dict)
    assert batch_err["error"]["code"] == -32600
    assert "Batch size exceeds limit" in batch_err["error"]["message"]

