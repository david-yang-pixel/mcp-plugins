from __future__ import annotations

import asyncio
from collections import deque
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
Limit = Annotated[int, Field(strict=True, ge=1, le=1000)]


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


# Every path is fixed and is a Binance Spot GET endpoint. No generic request tool.
TOOLS = {
    "exchange_info": ("Get trading pair information", "/api/v3/exchangeInfo", SymbolArgs, False),
    "price": ("Get the latest price for a pair", "/api/v3/ticker/price", SymbolArgs, False),
    "ticker_24h": ("Get 24-hour statistics for a pair", "/api/v3/ticker/24hr", SymbolArgs, False),
    "depth": ("Get the order book for a pair", "/api/v3/depth", DepthArgs, False),
    "klines": ("Get candlesticks for a pair", "/api/v3/klines", KlineArgs, False),
    "account": ("Get account information and balances", "/api/v3/account", Arguments, True),
    "open_orders": ("Get current orders for a pair", "/api/v3/openOrders", SymbolArgs, True),
    "all_orders": ("Get historical orders for a pair", "/api/v3/allOrders", OrdersArgs, True),
}


DEFAULT_ALLOWED_HOSTS = {
    "zendesk.smlershou.top",
    "127.0.0.1",
    "localhost",
    "[::1]",
    "::1",
}


def is_allowed_origin(origin: str) -> bool:
    custom_origins = os.environ.get("BINANCE_MCP_ALLOWED_ORIGINS")
    if custom_origins:
        allowed = {o.strip() for o in custom_origins.split(",") if o.strip()}
        if origin in allowed:
            return True
    try:
        parsed = urlsplit(origin)
    except Exception:
        return False
    if parsed.scheme in ("http", "https"):
        if parsed.hostname in DEFAULT_ALLOWED_HOSTS:
            return True
    return False


def tool_list():
    return [{"name": name, "description": description, "inputSchema": model.model_json_schema(),
             "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": True}}
            for name, (description, _, model, _) in TOOLS.items()]


def error(code, message, **extra):
    res = {"success": False, "code": code, "error": message}
    res.update(extra)
    return res


def get_tool_weight(name: str, params: dict) -> int:
    if name == "depth":
        limit = params.get("limit", 100)
        if limit == 5000:
            return 50
        if limit > 500:
            return 25
        if limit > 100:
            return 10
        return 5
    weights = {
        "exchange_info": 20,
        "price": 2,
        "ticker_24h": 2,
        "klines": 2,
        "account": 20,
        "open_orders": 6,
        "all_orders": 20,
    }
    return weights.get(name, 1)


def parse_retry_after(header_val: str | None, default: int = 60) -> int:
    if not header_val:
        return default
    try:
        val = int(header_val.strip())
        if 1 <= val <= 86400:
            return val
    except (ValueError, TypeError):
        pass
    return default


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

    async def check_circuit_breaker(self) -> dict | None:
        async with self._lock:
            now = time.time()
            if now < self.circuit_break_until:
                remaining = max(1, int(self.circuit_break_until - now))
                if self.is_banned:
                    return error("IP_BANNED", f"Binance IP ban active (HTTP 418); retry after {remaining}s", retry_after=remaining)
                return error("RATE_LIMITED", f"Binance rate limit cooldown active (HTTP 429); retry after {remaining}s", retry_after=remaining)
            return None

    async def acquire_weight(self, weight: int) -> dict | None:
        async with self._lock:
            now = time.time()
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

    def record_cooldown(self, status_code: int, retry_after: int):
        now = time.time()
        self.circuit_break_until = now + retry_after
        self.is_banned = (status_code == 418)


rate_limiter = RateLimiter()


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
    _, path, model, signed = TOOLS[name]
    try:
        params = model.model_validate(arguments).model_dump()
    except ValidationError:
        return error("BAD_ARGUMENTS", "Invalid tool arguments; see the tool input schema")

    cb_error = await rate_limiter.check_circuit_breaker()
    if cb_error:
        return cb_error

    weight = get_tool_weight(name, params)
    weight_error = await rate_limiter.acquire_weight(weight)
    if weight_error:
        return weight_error

    api_key = os.environ.get("BINANCE_API_KEY", "")
    api_secret = os.environ.get("BINANCE_API_SECRET", "")
    if signed and (not api_key or not api_secret):
        return error("CREDENTIALS_MISSING", "Binance API key and secret are required for account tools")
    headers = {}
    signature = ""
    if signed:
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = 5000
        query = urlencode(params)
        signature = hmac.new(api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        query += "&signature=" + signature
        headers["X-MBX-APIKEY"] = api_key
    else:
        query = urlencode(params)
    try:
        # Disable environment proxy configuration and redirects to keep credentials on the official origin.
        async with rate_limiter.semaphore:
            async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False) as client:
                async with client.stream("GET", BASE_URL + path + ("?" + query if query else ""), headers=headers) as response:
                    if response.status_code in (418, 429):
                        retry_after = parse_retry_after(response.headers.get("retry-after"), default=120 if response.status_code == 418 else 60)
                        rate_limiter.record_cooldown(response.status_code, retry_after)
                        if response.status_code == 418:
                            return error("IP_BANNED", "Binance IP ban triggered (HTTP 418)", retry_after=retry_after)
                        return error("RATE_LIMITED", "Binance rate limit exceeded (HTTP 429)", retry_after=retry_after)
                    if response.status_code != 200:
                        return error("BINANCE_HTTP_ERROR", f"Binance request failed (HTTP {response.status_code})")
                    chunks = bytearray()
                    async for chunk in response.aiter_bytes():
                        chunks.extend(chunk)
                        if len(chunks) > 5_000_000:
                            return error("BINANCE_RESPONSE_ERROR", "Binance response is too large")
        data = json.loads(chunks)
        if not isinstance(data, (dict, list)) or contains_sensitive(data, (api_key, api_secret, signature)):
            return error("BINANCE_RESPONSE_ERROR", "Invalid Binance response")
        if isinstance(data, dict) and isinstance(data.get("code"), int) and data["code"] < 0:
            return error("BINANCE_RESPONSE_ERROR", "Binance returned an error")
        return {"success": True, "data": data}
    except (httpx.HTTPError, ValueError, UnicodeError, RecursionError):
        return error("BINANCE_REQUEST_ERROR", "Binance request or response failed")


def rpc_error(request_id, code, message):
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def rpc_result(request_id, result):
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


app = FastAPI(title="binance-mcp", docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/health")
async def health():
    return {"ok": True, "service": "binance-mcp"}


async def process_rpc_item(item: Any) -> tuple[bool, dict | None]:
    if not isinstance(item, dict) or item.get("jsonrpc") != "2.0" or not isinstance(item.get("method"), str):
        request_id = item.get("id") if isinstance(item, dict) else None
        return (False, rpc_error(request_id, -32600, "Invalid request"))
    method = item["method"]
    request_id = item.get("id")
    is_notification = ("id" not in item)
    if is_notification:
        return (True, None)

    if method == "initialize":
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
    payload = await call_tool(tool_name, params.get("arguments", {}))
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

    try:
        body = await request.json()
    except (ValueError, UnicodeError):
        return rpc_error(None, -32700, "Invalid JSON")

    if isinstance(body, list):
        if len(body) == 0:
            return rpc_error(None, -32600, "Invalid request")
        responses = []
        for item in body:
            is_notif, resp = await process_rpc_item(item)
            if not is_notif and resp is not None:
                responses.append(resp)
        if len(responses) == 0:
            return Response(status_code=202)
        return JSONResponse(status_code=200, content=responses)

    if isinstance(body, dict):
        is_notif, resp = await process_rpc_item(body)
        if is_notif:
            return Response(status_code=202)
        return resp

    return rpc_error(None, -32600, "Invalid request")


def main():
    uvicorn.run("binance_mcp.app:app", host=os.environ.get("BINANCE_MCP_HOST", "127.0.0.1"),
                port=int(os.environ.get("BINANCE_MCP_PORT", "8124")), access_log=False)
