import asyncio
import json

import httpx
import pytest
from starlette.testclient import TestClient

from zendesk_kg_mcp.app import create_app
from zendesk_kg_mcp.models import RetrievalRequest
from zendesk_kg_mcp.retrieval import ENDPOINT, RetrievalClient, RetrievalError
from zendesk_kg_mcp.settings import Settings

MCP_KEY = "synthetic-mcp-only-key"
UPSTREAM_KEY = "synthetic-upstream-only-key"
HEADERS = {"Authorization": "Bearer " + MCP_KEY, "Accept": "application/json"}
SETTINGS = Settings(api_key=MCP_KEY, upstream_api_key=UPSTREAM_KEY)
RESULT = {"results": [{"ticketId": "test-123", "subject": "Redis timeout", "issueSummary": "Issue",
    "solutionSummary": "Check connectivity", "confidence": 0.4, "confidenceLevel": "low"}],
    "recommendedLinks": [{"url": "https://docs.example.test/redis"}], "searchId": "synthetic-search-id",
    "queryVectorDimension": 3072, "constraintsApplied": {}, "fallbackSuggestion": None,
    "parser": {"keywords": ["redis"]}, "graph": {"nodes": [], "edges": []}, "trace": {"steps": []}}


@pytest.fixture
def probe():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=RESULT)
    backend = RetrievalClient(SETTINGS, httpx.MockTransport(handler))
    with TestClient(create_app(SETTINGS, backend), base_url="http://localhost") as client:
        yield client, calls


def rpc(client, method, params=None):
    response = client.post("/mcp", headers=HEADERS,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
    assert response.status_code == 200
    return response.json()


def call(client, arguments):
    return rpc(client, "tools/call", {"name": "search_historical_tickets", "arguments": arguments})


def test_initialize_discovery_and_health_do_not_contact_upstream(probe):
    client, calls = probe
    result = rpc(client, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
        "clientInfo": {"name": "test", "version": "1"}})
    assert result["result"]["serverInfo"]["name"] == "zendesk-kg-mcp"
    tool = rpc(client, "tools/list")["result"]["tools"][0]
    assert tool["name"] == "search_historical_tickets"
    assert set(tool["inputSchema"]["properties"]) == set(RetrievalRequest.model_fields)
    assert client.get("/health", headers=HEADERS).json() == {"ok": True, "service": "zendesk-kg-mcp"}
    assert client.post("/mcp", headers=HEADERS, json={"jsonrpc": "2.0", "method": "notifications/initialized"}).status_code == 202
    assert calls == []


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"X-API-Key": "wrong"},
    {"Authorization": "Bearer wrong", "X-API-Key": MCP_KEY}, {"Authorization": "Bearer " + UPSTREAM_KEY}])
def test_unauthorized_requests_never_reach_upstream(probe, headers):
    client, calls = probe
    for path in ("/mcp", "/health"):
        assert client.post(path, headers=headers, json={}).status_code == 401
    assert client.post("/mcp", headers=headers | {"Accept": "application/json"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}).status_code == 401
    assert calls == []


def test_api_key_header_is_supported(probe):
    client, _ = probe
    assert client.get("/health", headers={"X-API-Key": MCP_KEY}).status_code == 200
    response = client.post("/mcp", headers={"X-API-Key": MCP_KEY, "Accept": "application/json"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    assert response.status_code == 200 and response.json()["result"]["tools"]


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"X-API-Key": "wrong"}])
def test_health_is_public_and_exposes_no_sensitive_data(probe, headers):
    client, calls = probe
    response = client.get("/health", headers=headers)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"ok": True, "service": "zendesk-kg-mcp"}
    assert MCP_KEY not in response.text and UPSTREAM_KEY not in response.text
    assert "www-authenticate" not in response.headers
    assert client.head("/health", headers=headers).status_code == 200
    assert calls == []


def test_mcp_still_requires_credentials_when_health_is_public(probe):
    client, calls = probe
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    assert client.post("/mcp", headers={"Accept": "application/json"}, json=body).status_code == 401
    assert client.get("/mcp").status_code == 401
    for headers in ({"Authorization": "Bearer wrong"}, {"X-API-Key": "wrong"}):
        assert client.post("/mcp", headers=headers | {"Accept": "application/json"}, json=body).status_code in (401, 403)
    assert calls == []


@pytest.mark.parametrize("path", ["/health/", "/healthz", "/health/x", "/health/../mcp", "/Health",
    "/mcp/health", "/health%2F"])
def test_health_bypass_is_exact_path_only(probe, path):
    client, calls = probe
    assert client.get(path).status_code == 401
    assert calls == []


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
def test_health_bypass_is_read_only(probe, method):
    client, _ = probe
    assert client.request(method, "/health").status_code == 401


def test_full_request_mapping_and_response_preservation(probe):
    client, calls = probe
    arguments = {"query": "redis cluster timeout", "topK": 5, "vectorK": 20, "fulltextK": 10,
        "rrfK": 70, "channels": ["issue_vector", "fulltext"], "channelWeights": {"issue_vector": 1.5},
        "filter": {"versions": ["v2.1"], "priority": "high", "status": "closed", "handledBy": "agent@example.test",
            "keywords": ["redis"], "createdAfter": "2025-01-01", "createdBefore": "2025-12-31"},
        "include": ["parser", "fusion", "graph", "embedding2d", "trace"], "orderBy": "createdAt",
        "allowVersionFamilyFallback": True, "recommendedLinkK": 4}
    result = call(client, arguments)["result"]
    assert not result.get("isError")
    assert result["structuredContent"] == RESULT
    assert json.loads(result["content"][0]["text"]) == RESULT
    assert len(calls) == 1 and str(calls[0].url) == ENDPOINT
    assert json.loads(calls[0].content) == arguments
    assert calls[0].headers["authorization"] == "Bearer " + UPSTREAM_KEY
    assert MCP_KEY not in str(calls[0].headers)


def test_defaults_and_optional_fields(probe):
    client, calls = probe
    assert not call(client, {"query": "redis"})["result"].get("isError")
    sent = json.loads(calls[0].content)
    assert sent == {"query": "redis", "topK": 3, "vectorK": 15, "fulltextK": 15, "rrfK": 60,
        "orderBy": "rrfScore", "allowVersionFamilyFallback": False, "recommendedLinkK": 3}


@pytest.mark.parametrize("arguments", [
    {"query": "   "}, {"query": "x", "topK": 21}, {"query": "x", "topK": True},
    {"query": "x", "topK": "3"}, {"query": "x", "vectorK": 51}, {"query": "x", "fulltextK": 0},
    {"query": "x", "rrfK": 0}, {"query": "x", "recommendedLinkK": 11},
    {"query": "x", "channels": ["unknown"]}, {"query": "x", "channels": []},
    {"query": "x", "include": ["unknown"]}, {"query": "x", "orderBy": "unknown"},
    {"query": "x", "channelWeights": {"fulltext": -1}},
    {"query": "x", "allowVersionFamilyFallback": "true"},
    {"query": "x", "filter": {"unknown": "ignored?"}},
    {"query": "x", "filter": {"priority": "critical"}},
    {"query": "x", "filter": {"createdAfter": "2025-13-01"}},
    {"query": "x", "filter": {"createdAfter": "2025-12-31", "createdBefore": "2025-01-01"}},
])
def test_invalid_input_is_rejected_before_upstream(probe, arguments):
    client, calls = probe
    result = call(client, arguments)
    assert "error" in result or result["result"]["isError"]
    assert calls == []


@pytest.mark.parametrize("status,code", [(400, "UPSTREAM_BAD_REQUEST"), (403, "UPSTREAM_AUTH_FAILED"),
    (429, "UPSTREAM_RATE_LIMITED"), (500, "UPSTREAM_HTTP_ERROR"), (302, "UPSTREAM_HTTP_ERROR")])
def test_error_status_redaction_and_redirect_refusal(status, code):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, text=UPSTREAM_KEY + ' private response', headers={"location": "https://untrusted.example/"})
    backend = RetrievalClient(SETTINGS, httpx.MockTransport(handler))
    with TestClient(create_app(SETTINGS, backend), base_url="http://localhost") as client:
        result = call(client, {"query": "redis"})["result"]
    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == code
    assert UPSTREAM_KEY not in json.dumps(result)
    assert len(calls) == 1 and str(calls[0].url) == ENDPOINT


@pytest.mark.parametrize("exception,code", [(httpx.ReadTimeout("private " + UPSTREAM_KEY), "UPSTREAM_TIMEOUT"),
    (httpx.ConnectError("private " + UPSTREAM_KEY), "UPSTREAM_UNAVAILABLE")])
def test_network_failures_are_sanitized(exception, code):
    def handler(request): raise exception
    with pytest.raises(RetrievalError) as error:
        asyncio.run(RetrievalClient(SETTINGS, httpx.MockTransport(handler)).search(RetrievalRequest(query="redis")))
    assert error.value.code == code and UPSTREAM_KEY not in str(error.value)


@pytest.mark.parametrize("response", [httpx.Response(200, text="html"),
    httpx.Response(200, content=b'{invalid', headers={"content-type": "application/json"}),
    httpx.Response(200, json={"results": []}), httpx.Response(200, json={"results": "bad", "searchId": "x"})])
def test_invalid_upstream_response_fails(response):
    with pytest.raises(RetrievalError) as error:
        asyncio.run(RetrievalClient(SETTINGS, httpx.MockTransport(lambda request: response)).search(RetrievalRequest(query="x")))
    assert error.value.code == "UPSTREAM_INVALID_RESPONSE"


def test_oversized_response_is_bounded():
    settings = Settings(api_key=MCP_KEY, upstream_api_key=UPSTREAM_KEY, max_response_bytes=1024)
    response = httpx.Response(200, json={"results": [], "searchId": "x", "trace": "x" * 2048})
    with pytest.raises(RetrievalError) as error:
        asyncio.run(RetrievalClient(settings, httpx.MockTransport(lambda request: response)).search(RetrievalRequest(query="x")))
    assert error.value.code == "RESPONSE_TOO_LARGE"


def test_missing_credentials_fail_closed():
    with TestClient(create_app(Settings()), base_url="http://localhost") as client:
        response = client.get("/health")
        assert response.status_code == 503
        assert response.json() == {"ok": False, "service": "zendesk-kg-mcp"}
        assert client.post("/mcp", headers=HEADERS, json={}).status_code == 503
        assert client.get("/healthz").status_code == 503
    with TestClient(create_app(Settings(api_key=MCP_KEY)), base_url="http://localhost") as client:
        assert client.get("/health", headers=HEADERS).status_code == 503
        assert client.get("/health").json() == {"ok": False, "service": "zendesk-kg-mcp"}
        assert call(client, {"query": "x"})["result"]["structuredContent"]["error"]["code"] == "NOT_CONFIGURED"


def test_origin_host_and_request_size_protection(probe):
    client, calls = probe
    for headers, expected in [({"Origin": "https://untrusted.example"}, 403), ({"Host": "untrusted.example"}, 421)]:
        response = client.post("/mcp", headers=HEADERS | headers,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert response.status_code == expected
    assert client.post("/mcp", headers=HEADERS, json={"query": "x" * 70000}).status_code == 413
    assert calls == []


def test_empty_results_are_successful_and_search_id_is_preserved():
    empty = {"results": [], "searchId": "empty-search", "recommendedLinks": []}
    result = asyncio.run(RetrievalClient(SETTINGS, httpx.MockTransport(lambda request: httpx.Response(200, json=empty))).search(RetrievalRequest(query="unknown")))
    assert result == empty
