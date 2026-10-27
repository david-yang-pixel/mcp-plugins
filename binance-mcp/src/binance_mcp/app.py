from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from typing import Annotated, Literal
from urllib.parse import urlencode

import httpx
import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request, Response
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


def tool_list():
    return [{"name": name, "description": description, "inputSchema": model.model_json_schema(),
             "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": True}}
            for name, (description, _, model, _) in TOOLS.items()]


def error(code, message):
    return {"success": False, "code": code, "error": message}


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
        async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False) as client:
            async with client.stream("GET", BASE_URL + path + ("?" + query if query else ""), headers=headers) as response:
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


@app.post("/mcp")
async def mcp(request: Request, authorization: str | None = Header(default=None)):
    key = os.environ.get("BINANCE_MCP_API_KEY", "")
    if not key:
        raise HTTPException(503, "MCP authentication is not configured")
    if not authorization or not hmac.compare_digest(authorization.encode(), ("Bearer " + key).encode()):
        raise HTTPException(401, "Unauthorized")
    try:
        body = await request.json()
    except (ValueError, UnicodeError):
        return rpc_error(None, -32700, "Invalid JSON")
    if not isinstance(body, dict) or body.get("jsonrpc") != "2.0" or not isinstance(body.get("method"), str):
        return rpc_error(None, -32600, "Invalid request")
    method, request_id = body["method"], body.get("id")
    if method.startswith("notifications/") and "id" not in body:
        return Response(status_code=202)
    if method == "initialize":
        return rpc_result(request_id, {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
                                       "serverInfo": {"name": "binance-mcp", "version": "0.1.0"}})
    if method == "ping":
        return rpc_result(request_id, {})
    if method == "tools/list":
        return rpc_result(request_id, {"tools": tool_list()})
    if method != "tools/call":
        return rpc_error(request_id, -32601, "Method not found")
    params = body.get("params")
    if not isinstance(params, dict) or not isinstance(params.get("name"), str) or not isinstance(params.get("arguments", {}), dict):
        return rpc_error(request_id, -32602, "Invalid parameters")
    payload = await call_tool(params["name"], params.get("arguments", {}))
    return rpc_result(request_id, {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
                                   "structuredContent": payload, "isError": not payload["success"]})


def main():
    uvicorn.run("binance_mcp.app:app", host=os.environ.get("BINANCE_MCP_HOST", "0.0.0.0"),
                port=int(os.environ.get("BINANCE_MCP_PORT", "8124")), access_log=False)
