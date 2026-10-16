"""Use the official MCP client against a real HTTP listener with a synthetic upstream."""
import asyncio
import socket
import threading
import time

import httpx
import httpx2
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from zendesk_kg_mcp.app import create_app
from zendesk_kg_mcp.retrieval import RetrievalClient
from zendesk_kg_mcp.settings import Settings


def test_official_client_can_discover_and_search_over_http():
    settings = Settings(api_key="synthetic-sdk-key", upstream_api_key="synthetic-sdk-upstream")
    payload = {"results": [{"ticketId": "sdk-ticket", "issueSummary": "Redis error"}], "searchId": "sdk-search"}
    backend = RetrievalClient(settings, httpx.MockTransport(lambda request: httpx.Response(200, json=payload)))
    server = uvicorn.Server(uvicorn.Config(create_app(settings, backend), log_level="error", access_log=False))
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        url = f"http://127.0.0.1:{listener.getsockname()[1]}/mcp"
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while not server.started and time.monotonic() < deadline:
                time.sleep(0.02)
            assert server.started

            async def verify():
                async with httpx2.AsyncClient(headers={"Authorization": "Bearer synthetic-sdk-key"}, trust_env=False) as http:
                    async with Client(streamable_http_client(url, http_client=http)) as client:
                        tools = await client.list_tools()
                        assert tools.tools[0].name == "search_historical_tickets"
                        result = await client.call_tool("search_historical_tickets", {"query": "redis", "topK": 1})
                        body = result.model_dump(by_alias=True)
                        assert body["isError"] is False
                        assert body["structuredContent"] == payload
            asyncio.run(verify())
        finally:
            server.should_exit = True
            thread.join(timeout=5)
            assert not thread.is_alive()
