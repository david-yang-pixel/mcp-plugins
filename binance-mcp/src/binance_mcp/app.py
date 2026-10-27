from __future__ import annotations

import asyncio
from collections import deque
from decimal import Decimal
import hashlib
import hmac
import json
import logging
import os
import time
from typing import Annotated, Any, Literal
from urllib.parse import urlencode, urlsplit

import httpx
import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

# httpcore emits wire-level headers and signed targets from child loggers, too.
for logger_name in ("httpx", "httpcore", "httpcore.http11", "httpcore.http2",
                    "httpcore.connection", "httpcore.proxy", "httpcore.socks"):
    logging.getLogger(logger_name).disabled = True

BASE_URL = "https://api.binance.com"
Symbol = Annotated[str, Field(strict=True, min_length=2, max_length=20, pattern=r"^[A-Z0-9]+$")]
Asset = Annotated[str, Field(strict=True, min_length=1, max_length=20, pattern=r"^[A-Z0-9]+$")]
Limit = Annotated[int, Field(strict=True, ge=1, le=1000)]


class InvalidToolArguments(Exception):
    def __init__(self, message: str = "Invalid tool arguments; see the tool input schema"):
        self.message = message
        super().__init__(message)


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SymbolArgs(Arguments):
    symbol: Symbol


class DepthArgs(SymbolArgs):
    limit: Literal[5, 10, 20, 50, 100, 500, 1000, 5000] = 100

    @field_validator("limit", mode="before")
    @classmethod
    def strict_limit(cls, value):
        if type(value) is not int:
            raise ValueError("limit must be an integer")
        return value


class KlineArgs(SymbolArgs):
    interval: Literal["1s", "1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "3d", "1w", "1M"]
    limit: Limit = 500


class OrdersArgs(SymbolArgs):
    limit: Limit = 500


class TradesArgs(SymbolArgs):
    limit: Limit = 500
    fromId: Annotated[int, Field(strict=True, ge=0)] | None = None


class WalletBalanceArgs(Arguments):
    quoteAsset: Asset = "USDT"


class FundingArgs(Arguments):
    asset: Asset | None = None
    needBtcValuation: Annotated[bool, Field(strict=True)] = True


# Every path is fixed and read-only. No generic request tool. get-funding-asset is a
# query that Binance only serves over POST; it is the one non-GET endpoint.
# position_cost has no path: it is computed from my_trades and price.
TOOLS = {
    "exchange_info": ("Get trading pair information", "/api/v3/exchangeInfo", SymbolArgs, False, "GET"),
    "price": ("Get the latest price for a pair", "/api/v3/ticker/price", SymbolArgs, False, "GET"),
    "ticker_24h": ("Get 24-hour statistics for a pair", "/api/v3/ticker/24hr", SymbolArgs, False, "GET"),
    "depth": ("Get the order book for a pair", "/api/v3/depth", DepthArgs, False, "GET"),
    "klines": ("Get candlesticks for a pair", "/api/v3/klines", KlineArgs, False, "GET"),
    "account": ("Get Spot account information and balances", "/api/v3/account", Arguments, True, "GET"),
    "open_orders": ("Get current orders for a pair", "/api/v3/openOrders", SymbolArgs, True, "GET"),
    "all_orders": ("Get historical orders for a pair", "/api/v3/allOrders", OrdersArgs, True, "GET"),
    "my_trades": ("Get your trade fills for a pair, including fees; page with fromId",
                  "/api/v3/myTrades", TradesArgs, True, "GET"),
    "position_cost": ("Compute FIFO cost basis, open lots with buy times, and realized/unrealized P&L "
                      "for a pair from your full trade history", None, SymbolArgs, True, "GET"),
    "wallet_balance": ("Get the total balance of every wallet (Spot, Funding, Earn, Futures, ...) in a quote asset",
                       "/sapi/v1/asset/wallet/balance", WalletBalanceArgs, True, "GET"),
    "funding_assets": ("Get Funding wallet balances, optionally for one asset",
                       "/sapi/v1/asset/get-funding-asset", FundingArgs, True, "POST"),
}


PUBLIC_ORIGIN_HOST = "zendesk.smlershou.top"
LOCAL_ORIGIN_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _origin_key(origin: str) -> tuple[str, str, int] | None:
    """scheme, hostname, and effective port for a pure origin (no path, query, fragment, or userinfo)."""
    if not isinstance(origin, str) or not origin or any(ch.isspace() for ch in origin):
        return None
    # urlsplit drops an empty query/fragment and treats an empty username as missing.
    if any(ch in origin for ch in "?#@\\"):
        return None
    try:
        parsed = urlsplit(origin)
        if (
            parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.netloc.endswith(":")
        ):
            return None
        scheme = parsed.scheme
        host = parsed.hostname
        if scheme not in ("http", "https") or not host:
            return None
        port = parsed.port
    except Exception:
        return None
    if port is None:
        port = 443 if scheme == "https" else 80
    if not 1 <= port <= 65535:
        return None
    return scheme, host, port


def is_allowed_origin(origin: str) -> bool:
    key = _origin_key(origin)
    if key is None:
        return False
    scheme, host, port = key
    custom_origins = os.environ.get("BINANCE_MCP_ALLOWED_ORIGINS")
    if custom_origins:
        for item in custom_origins.split(","):
            allowed = _origin_key(item.strip())
            if allowed is not None and allowed == key:
                return True
    if scheme == "https" and host == PUBLIC_ORIGIN_HOST and port == 443:
        return True
    # Browsers put the page port in Origin. Local dev uses whatever port the server bound.
    if host in LOCAL_ORIGIN_HOSTS:
        return True
    return False


def tool_list():
    return [{"name": name, "description": description, "inputSchema": model.model_json_schema(),
             "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": True}}
            for name, (description, _, model, _, _) in TOOLS.items()]


def error(code, message, **extra):
    res = {"success": False, "code": code, "error": message}
    res.update(extra)
    return res


def get_tool_weight(name: str, params: dict) -> int:
    # Spot REQUEST_WEIGHT. depth follows the documented brackets, including 250 for limit 5000.
    if name == "depth":
        limit = params.get("limit", 100)
        if limit <= 100:
            return 5
        if limit <= 500:
            return 25
        if limit <= 1000:
            return 50
        return 250
    weights = {
        "exchange_info": 20,
        "price": 2,
        "ticker_24h": 2,
        "klines": 2,
        "account": 20,
        "open_orders": 6,
        "all_orders": 20,
        "my_trades": 20,
        # SAPI weight is a separate Binance pool; counting it here only makes local limiting stricter.
        "wallet_balance": 60,
        "funding_assets": 1,
    }
    return weights.get(name, 1)


# Binance IP bans last from 2 minutes up to 3 days. Retry-After is a delta in seconds.
MAX_RETRY_AFTER = 3 * 24 * 60 * 60


def parse_retry_after(header_val: str | None, default: int = 60) -> int:
    if not header_val:
        return default
    token = header_val.strip()
    if len(token) > 16:
        return default
    try:
        val = int(token)
    except (ValueError, TypeError):
        return default
    if val < 1:
        return default
    return min(val, MAX_RETRY_AFTER)


class RateLimiter:
    def __init__(self, max_weight: int | None = None, max_concurrency: int | None = None):
        self._custom_max_weight = max_weight
        self._custom_concurrency = max_concurrency
        self.semaphore = asyncio.Semaphore(self._get_concurrency())
        self.history: deque[tuple[float, int]] = deque()
        self.circuit_break_until: float = 0.0
        self.is_banned: bool = False
        self._lock = asyncio.Lock()

    def _get_max_weight(self) -> int:
        if self._custom_max_weight is not None:
            return self._custom_max_weight
        return int(os.environ.get("BINANCE_MAX_WEIGHT_PER_MINUTE", "1200"))

    def _get_concurrency(self) -> int:
        if self._custom_concurrency is not None:
            return self._custom_concurrency
        return int(os.environ.get("BINANCE_MAX_CONCURRENCY", "10"))

    def reset(self):
        self.history.clear()
        self.circuit_break_until = 0.0
        self.is_banned = False
        self.semaphore = asyncio.Semaphore(self._get_concurrency())

    def _cooldown_error(self, now: float) -> dict | None:
        if now < self.circuit_break_until:
            remaining = max(1, int(self.circuit_break_until - now))
            if self.is_banned:
                return error("IP_BANNED", f"Binance IP ban active (HTTP 418); retry after {remaining}s", retry_after=remaining)
            return error("RATE_LIMITED", f"Binance rate limit cooldown active (HTTP 429); retry after {remaining}s", retry_after=remaining)
        return None

    def _reserve_weight(self, now: float, weight: int) -> dict | None:
        while self.history and self.history[0][0] <= now - 60:
            self.history.popleft()
        current_weight = sum(w for _, w in self.history)
        max_w = self._get_max_weight()
        if current_weight + weight > max_w:
            oldest_ts = self.history[0][0] if self.history else now
            retry_after = max(1, int(60 - (now - oldest_ts)))
            return error("RATE_LIMITED", "Binance request weight rate limit reached", retry_after=retry_after)
        self.history.append((now, weight))
        return None

    async def check_circuit_breaker(self) -> dict | None:
        async with self._lock:
            return self._cooldown_error(time.time())

    async def acquire_admission(self, weight: int) -> dict | None:
        # Cooldown and weight are decided together after the caller holds the concurrency slot.
        async with self._lock:
            now = time.time()
            blocked = self._cooldown_error(now)
            if blocked:
                return blocked
            return self._reserve_weight(now, weight)

    def record_cooldown(self, status_code: int, retry_after: int):
        now = time.time()
        until = now + retry_after
        ban_active = self.is_banned and now < self.circuit_break_until
        self.is_banned = status_code == 418 or ban_active
        if until > self.circuit_break_until:
            self.circuit_break_until = until


rate_limiter = RateLimiter()


# Only Binance's numeric error code is returned; its msg text is never echoed.
BINANCE_ERROR_HINTS = {
    -1021: "request timestamp is outside recvWindow; check the server clock",
    -1022: "signature rejected; check BINANCE_API_SECRET",
    -2008: "API key rejected; check BINANCE_API_KEY",
    -2014: "API key format rejected; check BINANCE_API_KEY",
    -2015: "API key, IP allowlist, or permissions rejected",
}
HTTP_ERROR_HINTS = {
    403: "request blocked by Binance WAF",
    451: "Binance is unavailable from this server's location",
}


def binance_error_code(raw: bytes) -> int | None:
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        return None
    code = body.get("code") if isinstance(body, dict) else None
    if isinstance(code, int) and not isinstance(code, bool) and -99999 <= code < 0:
        return code
    return None


def binance_error(message: str, status: int | None, code: int | None) -> dict:
    hint = BINANCE_ERROR_HINTS.get(code) or HTTP_ERROR_HINTS.get(status)
    extra = {}
    if status is not None:
        extra["http_status"] = status
    if code is not None:
        extra["binance_code"] = code
    if hint:
        message += f": {hint}"
    return error("BINANCE_HTTP_ERROR" if status is not None else "BINANCE_RESPONSE_ERROR", message, **extra)


def contains_sensitive(value, secrets):
    if isinstance(value, str):
        return any(secret and secret in value for secret in secrets)
    if isinstance(value, list):
        return any(contains_sensitive(v, secrets) for v in value)
    if isinstance(value, dict):
        return any(contains_sensitive(k, secrets) or contains_sensitive(v, secrets) for k, v in value.items())
    return False


async def call_tool(name, arguments):
    if name not in TOOLS:
        return error("UNKNOWN_TOOL", "Unknown tool")
    _, path, model, signed, method = TOOLS[name]
    try:
        params = model.model_validate(arguments).model_dump(exclude_none=True)
    except ValidationError:
        raise InvalidToolArguments("Invalid tool arguments; see the tool input schema")
    api_key = os.environ.get("BINANCE_API_KEY", "")
    api_secret = os.environ.get("BINANCE_API_SECRET", "")
    if signed and (not api_key or not api_secret):
        return error("CREDENTIALS_MISSING", "Binance API key and secret are required for account tools")
    if path is None:
        return await position_cost(params["symbol"])

    cb_error = await rate_limiter.check_circuit_breaker()
    if cb_error:
        return cb_error

    weight = get_tool_weight(name, params)

    headers = {}
    signature = ""
    try:
        # Disable environment proxy configuration and redirects to keep credentials on the official origin.
        async with rate_limiter.semaphore:
            admit_error = await rate_limiter.acquire_admission(weight)
            if admit_error:
                return admit_error
            call_params = {k: str(v).lower() if isinstance(v, bool) else v for k, v in params.items()}
            if signed:
                call_params["timestamp"] = int(time.time() * 1000)
                call_params["recvWindow"] = 5000
                query = urlencode(call_params)
                signature = hmac.new(api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
                query += "&signature=" + signature
                headers["X-MBX-APIKEY"] = api_key
            else:
                query = urlencode(call_params)
            async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False) as client:
                async with client.stream(method, BASE_URL + path + ("?" + query if query else ""), headers=headers) as response:
                    if response.status_code in (418, 429):
                        retry_after = parse_retry_after(response.headers.get("retry-after"), default=120 if response.status_code == 418 else 60)
                        rate_limiter.record_cooldown(response.status_code, retry_after)
                        if response.status_code == 418:
                            return error("IP_BANNED", "Binance IP ban triggered (HTTP 418)", retry_after=retry_after)
                        return error("RATE_LIMITED", "Binance rate limit exceeded (HTTP 429)", retry_after=retry_after)
                    if response.status_code != 200:
                        raw = bytearray()
                        async for chunk in response.aiter_bytes():
                            raw.extend(chunk)
                            if len(raw) > 64_000:
                                break
                        code = binance_error_code(raw) if len(raw) <= 64_000 else None
                        return binance_error(f"Binance request failed (HTTP {response.status_code})",
                                             response.status_code, code)
                    chunks = bytearray()
                    async for chunk in response.aiter_bytes():
                        chunks.extend(chunk)
                        if len(chunks) > 5_000_000:
                            return error("BINANCE_RESPONSE_ERROR", "Binance response is too large")
        data = json.loads(chunks)
        if not isinstance(data, (dict, list)) or contains_sensitive(data, (api_key, api_secret, signature)):
            return error("BINANCE_RESPONSE_ERROR", "Invalid Binance response")
        if isinstance(data, dict) and isinstance(data.get("code"), int) and data["code"] < 0:
            return binance_error("Binance returned an error", None, binance_error_code(chunks))
        return {"success": True, "data": data}
    except (httpx.HTTPError, ValueError, UnicodeError, RecursionError):
        return error("BINANCE_REQUEST_ERROR", "Binance request or response failed")


MAX_TRADE_PAGES = 10
MAX_OPEN_LOTS = 100


def _num(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.00000001")), "f")


def _utc(ms: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ms / 1000))


def fifo_position(trades: list[dict], base: str, quote: str) -> dict:
    """Match sells against the oldest buys. Fees in the base or quote asset adjust quantity
    or cost; fees in any other asset (such as BNB) are reported separately, not as cost."""
    lots: deque[list] = deque()  # [time_ms, remaining_qty, unit_cost]
    realized = Decimal(0)
    unmatched = Decimal(0)
    other_fees: dict[str, Decimal] = {}
    for trade in trades:
        qty, quote_qty, fee = Decimal(trade["qty"]), Decimal(trade["quoteQty"]), Decimal(trade["commission"])
        fee_asset, is_buyer, at = trade["commissionAsset"], trade["isBuyer"], trade["time"]
        if not isinstance(is_buyer, bool) or not isinstance(at, int) or min(qty, quote_qty, fee) < 0:
            raise ValueError("invalid trade")
        base_fee = fee if fee_asset == base else Decimal(0)
        quote_fee = fee if fee_asset == quote else Decimal(0)
        if fee and fee_asset not in (base, quote):
            other_fees[fee_asset] = other_fees.get(fee_asset, Decimal(0)) + fee
        if is_buyer:
            net = qty - base_fee
            if net > 0:
                lots.append([at, net, (quote_qty + quote_fee) / net])
            continue
        consume = qty + base_fee
        remaining, cost = consume, Decimal(0)
        while remaining > 0 and lots:
            lot = lots[0]
            used = min(lot[1], remaining)
            cost += used * lot[2]
            lot[1] -= used
            remaining -= used
            if lot[1] <= 0:
                lots.popleft()
        # Units sold beyond recorded buys came from elsewhere (transfer, Convert); no P&L for them.
        unmatched += remaining
        if consume > 0:
            realized += (quote_qty - quote_fee) * (consume - remaining) / consume - cost
    return {
        "open_quantity": sum((lot[1] for lot in lots), Decimal(0)),
        "open_cost": sum((lot[1] * lot[2] for lot in lots), Decimal(0)),
        "realized_pnl": realized,
        "unmatched_sell_quantity": unmatched,
        "fees_in_other_assets": {asset: _num(amount) for asset, amount in other_fees.items()},
        "open_lots": [{"bought_at": _utc(at), "quantity": _num(qty), "unit_cost": _num(unit), "cost": _num(qty * unit)}
                      for at, qty, unit in lots],
    }


async def position_cost(symbol: str) -> dict:
    info = await call_tool("exchange_info", {"symbol": symbol})
    if not info["success"]:
        return info
    try:
        meta = info["data"]["symbols"][0]
        base, quote = meta["baseAsset"], meta["quoteAsset"]
    except (KeyError, IndexError, TypeError):
        return error("BINANCE_RESPONSE_ERROR", "Invalid Binance response")

    trades: list[dict] = []
    from_id = 0
    for _ in range(MAX_TRADE_PAGES):
        page = await call_tool("my_trades", {"symbol": symbol, "limit": 1000, "fromId": from_id})
        if not page["success"]:
            return page
        batch = page["data"]
        if not isinstance(batch, list):
            return error("BINANCE_RESPONSE_ERROR", "Invalid Binance response")
        trades.extend(batch)
        if len(batch) < 1000:
            break
        try:
            from_id = int(batch[-1]["id"]) + 1
        except (KeyError, TypeError, ValueError):
            return error("BINANCE_RESPONSE_ERROR", "Invalid Binance response")
    else:
        return error("TOO_MANY_TRADES", f"More than {MAX_TRADE_PAGES * 1000} trades for {symbol}; cost basis not computed")

    try:
        trades.sort(key=lambda trade: int(trade["id"]))
        position = fifo_position(trades, base, quote)
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return error("BINANCE_RESPONSE_ERROR", "Invalid Binance trade data")

    qty, cost, lots = position["open_quantity"], position["open_cost"], position["open_lots"]
    data = {
        "symbol": symbol, "base_asset": base, "quote_asset": quote, "method": "FIFO",
        "trade_count": len(trades),
        "first_trade_at": _utc(trades[0]["time"]) if trades else None,
        "last_trade_at": _utc(trades[-1]["time"]) if trades else None,
        "open_quantity": _num(qty), "open_cost": _num(cost),
        "average_cost": _num(cost / qty) if qty > 0 else None,
        "realized_pnl": _num(position["realized_pnl"]),
        "unmatched_sell_quantity": _num(position["unmatched_sell_quantity"]),
        "fees_in_other_assets": position["fees_in_other_assets"],
        "open_lots": lots[-MAX_OPEN_LOTS:], "open_lots_truncated": len(lots) > MAX_OPEN_LOTS,
        "current_price": None, "market_value": None, "unrealized_pnl": None, "unrealized_pnl_pct": None,
        "note": ("Only trades on this pair are counted: transfers between wallets, Convert, and dust "
                 "conversion are not, so open_quantity can differ from wallet balances."),
    }
    ticker = await call_tool("price", {"symbol": symbol})
    try:
        price = Decimal(ticker["data"]["price"]) if ticker["success"] else None
    except (KeyError, TypeError, ArithmeticError):
        price = None
    if price is None:
        data["price_error"] = ticker.get("code", "BINANCE_RESPONSE_ERROR")
    else:
        value = qty * price
        data.update(current_price=_num(price), market_value=_num(value), unrealized_pnl=_num(value - cost),
                    unrealized_pnl_pct=_num((value - cost) / cost * 100) if cost > 0 else None)
    return {"success": True, "data": data}


def rpc_error(request_id, code, message):
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def rpc_result(request_id, result):
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def get_max_body_bytes() -> int:
    try:
        val = int(os.environ.get("BINANCE_MCP_MAX_BODY_BYTES", "1000000"))
        return val if val > 0 else 1000000
    except (ValueError, TypeError):
        return 1000000


def get_max_batch_size() -> int:
    try:
        val = int(os.environ.get("BINANCE_MCP_MAX_BATCH_SIZE", "50"))
        return val if val > 0 else 50
    except (ValueError, TypeError):
        return 50


def is_valid_rpc_id(val: Any) -> bool:
    return isinstance(val, (str, int)) and not isinstance(val, bool)


app = FastAPI(title="binance-mcp", docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/health")
async def health():
    return {"ok": True, "service": "binance-mcp"}


async def process_rpc_item(item: Any, is_batch: bool = False) -> tuple[bool, dict | None]:
    if not isinstance(item, dict):
        return (False, rpc_error(None, -32600, "Invalid request"))
    if "id" in item:
        raw_id = item["id"]
        if not is_valid_rpc_id(raw_id):
            return (False, rpc_error(None, -32600, "Invalid request id: id must be a string or integer"))
        request_id = raw_id
        is_notification = False
    else:
        request_id = None
        is_notification = True

    if item.get("jsonrpc") != "2.0" or not isinstance(item.get("method"), str):
        return (False, rpc_error(request_id if not is_notification else None, -32600, "Invalid request"))

    if is_notification:
        return (True, None)

    if "params" in item and not isinstance(item["params"], (dict, list)):
        return (False, rpc_error(request_id, -32602, "Invalid params: params must be an object or array"))

    method = item["method"]

    if method == "initialize":
        if is_batch:
            return (False, rpc_error(request_id, -32600, "initialize cannot be part of a batch request"))
        init_params = item.get("params")
        if not isinstance(init_params, dict):
            return (False, rpc_error(request_id, -32602, "Invalid initialize parameters"))
        if not isinstance(init_params.get("protocolVersion"), str):
            return (False, rpc_error(request_id, -32602, "protocolVersion is required"))
        if not isinstance(init_params.get("capabilities"), dict):
            return (False, rpc_error(request_id, -32602, "capabilities is required"))
        client_info = init_params.get("clientInfo")
        if not isinstance(client_info, dict) or not isinstance(client_info.get("name"), str) or not isinstance(client_info.get("version"), str):
            return (False, rpc_error(request_id, -32602, "clientInfo must include name and version"))
        return (False, rpc_result(request_id, {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
                                               "serverInfo": {"name": "binance-mcp", "version": "0.1.0"}}))
    if method == "ping":
        return (False, rpc_result(request_id, {}))
    if method == "tools/list":
        return (False, rpc_result(request_id, {"tools": tool_list()}))
    if method != "tools/call":
        return (False, rpc_error(request_id, -32601, "Method not found"))
    params = item.get("params")
    if not isinstance(params, dict) or not isinstance(params.get("name"), str) or not isinstance(params.get("arguments", {}), dict):
        return (False, rpc_error(request_id, -32602, "Invalid parameters"))
    tool_name = params["name"]
    if tool_name not in TOOLS:
        return (False, rpc_error(request_id, -32602, f"Unknown tool: {tool_name}"))
    try:
        payload = await call_tool(tool_name, params.get("arguments", {}))
    except InvalidToolArguments as e:
        return (False, rpc_error(request_id, -32602, str(e)))
    return (False, rpc_result(request_id, {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
                                           "structuredContent": payload, "isError": not payload["success"]}))


@app.post("/mcp")
async def mcp(request: Request, authorization: str | None = Header(default=None)):
    origin = request.headers.get("origin")
    if origin is not None and not is_allowed_origin(origin):
        raise HTTPException(403, "Origin not allowed")

    key = os.environ.get("BINANCE_MCP_API_KEY", "")
    if not key:
        raise HTTPException(503, "MCP authentication is not configured")
    if not authorization:
        raise HTTPException(401, "Unauthorized")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(token.encode(), key.encode()):
        raise HTTPException(401, "Unauthorized")

    max_body_bytes = get_max_body_bytes()
    content_length_header = request.headers.get("content-length")
    if content_length_header is not None:
        token = content_length_header.strip()
        if len(token) > 16 or not token.isascii() or not token.isdigit():
            raise HTTPException(400, "Invalid Content-Length header")
        if int(token) > max_body_bytes:
            raise HTTPException(413, "Request body too large")

    chunks = bytearray()
    async for chunk in request.stream():
        chunks.extend(chunk)
        if len(chunks) > max_body_bytes:
            raise HTTPException(413, "Request body too large")

    try:
        body = json.loads(chunks)
    except (ValueError, UnicodeError):
        return rpc_error(None, -32700, "Invalid JSON")

    max_batch = get_max_batch_size()
    if isinstance(body, list):
        if len(body) == 0:
            return rpc_error(None, -32600, "Invalid request")
        if len(body) > max_batch:
            return rpc_error(None, -32600, f"Batch size exceeds limit of {max_batch}")
        if any(isinstance(item, dict) and item.get("method") == "initialize" for item in body):
            return rpc_error(None, -32600, "initialize cannot be part of a batch request")
        responses = []
        for item in body:
            is_notif, resp = await process_rpc_item(item, is_batch=True)
            if not is_notif and resp is not None:
                responses.append(resp)
        if len(responses) == 0:
            return Response(status_code=202)
        return JSONResponse(status_code=200, content=responses)

    if isinstance(body, dict):
        is_notif, resp = await process_rpc_item(body, is_batch=False)
        if is_notif:
            return Response(status_code=202)
        return resp

    return rpc_error(None, -32600, "Invalid request")


def main():
    uvicorn.run("binance_mcp.app:app", host=os.environ.get("BINANCE_MCP_HOST", "127.0.0.1"),
                port=int(os.environ.get("BINANCE_MCP_PORT", "8124")), access_log=False)
