from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient


AUTH_HEADER = {"Authorization": "Bearer " + "x" * 32}


def _app(
    monkeypatch,
    *,
    max_concurrent_runs: str = "4",
    cache_ttl_seconds: str = "300",
    cache_max_entries: str = "128",
    singleflight_enabled: str = "true",
):
    monkeypatch.setenv("SIGNOZ_MCP_API_KEY", "x" * 32)
    monkeypatch.setenv("SIGNOZ_MCP_SZ_PATH", "sz")
    monkeypatch.setenv("SIGNOZ_MCP_MAX_CONCURRENT_RUNS", max_concurrent_runs)
    monkeypatch.setenv("SIGNOZ_MCP_CACHE_TTL_SECONDS", cache_ttl_seconds)
    monkeypatch.setenv("SIGNOZ_MCP_CACHE_MAX_ENTRIES", cache_max_entries)
    monkeypatch.setenv("SIGNOZ_MCP_CACHEABLE_COMMANDS", "services,schema")
    monkeypatch.setenv("SIGNOZ_MCP_SINGLEFLIGHT_ENABLED", singleflight_enabled)
    monkeypatch.setenv("SIGNOZ_URL", "https://signoz.example.com")
    monkeypatch.setenv("SIGNOZ_API_KEY", "sn_test")

    from signoz_mcp.settings import get_settings

    get_settings.cache_clear()
    from signoz_mcp.app import _reset_runtime_state, create_app

    _reset_runtime_state()
    return create_app()


def _client(monkeypatch):
    return TestClient(_app(monkeypatch))


async def _call_sz(client: httpx.AsyncClient, request_id: object, args: list[str]) -> dict:
    response = await client.post(
        "/mcp",
        headers=AUTH_HEADER,
        json={
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {
                "name": "sz_run",
                "arguments": {"args": args},
            },
        },
    )
    assert response.status_code == 200
    return response.json()["result"]["structuredContent"]


def test_mcp_rejects_missing_bearer_token(monkeypatch):
    client = _client(monkeypatch)

    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )

    assert response.status_code == 401


def test_mcp_initialize_and_tools_list(monkeypatch):
    client = _client(monkeypatch)
    headers = AUTH_HEADER

    init_response = client.post(
        "/mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    )
    tools_response = client.post(
        "/mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    )

    assert init_response.status_code == 200
    assert init_response.json()["result"]["capabilities"]["tools"] == {}
    tools = tools_response.json()["result"]["tools"]
    tools_by_name = {tool["name"]: tool for tool in tools}
    assert tools_by_name["sz_run"]["inputSchema"]["required"] == ["args"]
    assert {
        "signoz_list_services",
        "signoz_get_trace_details",
        "signoz_list_metrics",
        "signoz_get_field_keys",
        "signoz_get_field_values",
        "signoz_search_logs",
        "signoz_aggregate_logs",
        "signoz_search_traces",
        "signoz_aggregate_traces",
        "signoz_query_metrics",
    } <= tools_by_name.keys()
    assert not any(
        name.startswith(("signoz_create_", "signoz_update_", "signoz_delete_"))
        for name in tools_by_name
    )


@pytest.mark.parametrize(
    ("tool_name", "arguments", "expected_args"),
    [
        (
            "signoz_list_services",
            {"timeRange": "2h", "environment": "production", "limit": 7},
            ["services", "--hours", "2", "--limit", "7", "--env", "production"],
        ),
        (
            "signoz_get_trace_details",
            {"traceId": "a" * 32, "timeRange": "6h", "limit": 500},
            None,
        ),
        (
            "signoz_list_metrics",
            {"searchText": "k8s.pod.cpu", "timeRange": "30m", "limit": 10},
            None,
        ),
    ],
)
def test_mcp_structured_read_tools_build_safe_sz_calls(
    monkeypatch,
    tool_name,
    arguments,
    expected_args,
):
    client = _client(monkeypatch)
    captured = []

    async def fake_run_sz(args, settings):
        captured.append(args)
        return {"success": True, "data": [{"ok": True}]}

    monkeypatch.setattr("signoz_mcp.app.run_sz", fake_run_sz)

    response = client.post(
        "/mcp",
        headers=AUTH_HEADER,
        json={
            "jsonrpc": "2.0",
            "id": f"call-{tool_name}",
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["result"]["isError"] is False
    assert len(captured) == 1
    args = captured[0]
    if expected_args is not None:
        assert args == expected_args
    elif tool_name == "signoz_get_trace_details":
        assert args[0] == "query"
        assert "signoz_traces.distributed_signoz_index_v3" in args[1]
        assert "trace_id = '" + "a" * 32 + "'" in args[1]
        assert args[-2:] == ["--hours", "6"]
    else:
        assert args[0] == "query"
        assert "signoz_metrics.distributed_time_series_v4" in args[1]
        assert "k8s.pod.cpu" in args[1]
        assert args[-2:] == ["--hours", "0.5"]


def test_mcp_trace_details_rejects_invalid_trace_id_before_query(monkeypatch):
    client = _client(monkeypatch)

    async def fake_run_sz(args, settings):
        raise AssertionError("run_sz should not be called for invalid trace id")

    monkeypatch.setattr("signoz_mcp.app.run_sz", fake_run_sz)

    response = client.post(
        "/mcp",
        headers=AUTH_HEADER,
        json={
            "jsonrpc": "2.0",
            "id": "bad-trace",
            "method": "tools/call",
            "params": {
                "name": "signoz_get_trace_details",
                "arguments": {"traceId": "not-a-trace-id"},
            },
        },
    )

    body = response.json()
    assert body["result"]["isError"] is True
    assert body["result"]["structuredContent"]["code"] == "BAD_ARGUMENTS"


def test_mcp_accepts_initialized_notification(monkeypatch):
    client = _client(monkeypatch)

    response = client.post(
        "/mcp",
        headers=AUTH_HEADER,
        json={
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {},
        },
    )

    assert response.status_code == 202
    assert response.text == ""


def test_mcp_ping(monkeypatch):
    client = _client(monkeypatch)

    response = client.post(
        "/mcp",
        headers=AUTH_HEADER,
        json={"jsonrpc": "2.0", "id": 3, "method": "ping", "params": {}},
    )

    assert response.status_code == 200
    assert response.json() == {"jsonrpc": "2.0", "id": 3, "result": {}}


def test_mcp_unknown_method_returns_jsonrpc_error(monkeypatch):
    client = _client(monkeypatch)

    response = client.post(
        "/mcp",
        headers={"Authorization": "Bearer " + "x" * 32},
        json={"jsonrpc": "2.0", "id": 4, "method": "resources/list"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == 4
    assert body["error"]["code"] == -32601


def test_mcp_tool_call_runs_sz(monkeypatch):
    client = _client(monkeypatch)

    async def fake_run_sz(args, settings):
        assert args == ["errors", "--env", "production"]
        assert settings.signoz_api_key == "sn_test"
        return {"success": True, "data": [{"body": "boom"}]}

    monkeypatch.setattr("signoz_mcp.app.run_sz", fake_run_sz)

    response = client.post(
        "/mcp",
        headers=AUTH_HEADER,
        json={
            "jsonrpc": "2.0",
            "id": "call-1",
            "method": "tools/call",
            "params": {
                "name": "sz_run",
                "arguments": {"args": ["errors", "--env", "production"]},
            },
        },
    )

    body = response.json()
    assert response.status_code == 200
    assert body["id"] == "call-1"
    assert body["result"]["isError"] is False
    assert body["result"]["structuredContent"]["data"] == [{"body": "boom"}]


def test_mcp_tool_call_rejects_bad_args(monkeypatch):
    client = _client(monkeypatch)

    response = client.post(
        "/mcp",
        headers=AUTH_HEADER,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "sz_run", "arguments": {"args": "query SELECT 1"}},
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["result"]["isError"] is True
    assert body["result"]["structuredContent"]["code"] == "BAD_ARGUMENTS"


@pytest.mark.asyncio
async def test_sse_endpoint_advertises_mcp_endpoint(monkeypatch):
    from signoz_mcp.settings import get_settings
    app = _app(monkeypatch)
    route = next(r for r in app.routes if r.path == "/sse")
    response = await route.endpoint(
        authorization=AUTH_HEADER["Authorization"],
        x_mcp_endpoint=None,
        settings=get_settings(),
    )
    assert response.status_code == 200
    assert "text/event-stream" in response.headers["content-type"]
    assert await anext(response.body_iterator) == "event: endpoint\ndata: /signoz-mcp/mcp\n\n"
    await response.body_iterator.aclose()



@pytest.mark.asyncio
async def test_mcp_services_call_uses_cache(monkeypatch):
    app = _app(monkeypatch, cache_ttl_seconds="60")
    calls = 0

    async def fake_run_sz(args, settings):
        nonlocal calls
        calls += 1
        return {"success": True, "data": {"args": args, "call": calls}}

    monkeypatch.setattr("signoz_mcp.app.run_sz", fake_run_sz)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await _call_sz(client, "cache-1", ["services"])
        second = await _call_sz(client, "cache-2", ["services"])

    assert calls == 1
    assert first == second


@pytest.mark.asyncio
async def test_mcp_does_not_cache_failed_results(monkeypatch):
    app = _app(monkeypatch, cache_ttl_seconds="60")
    calls = 0

    async def fake_run_sz(args, settings):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"success": False, "code": "SZ_NONZERO", "error": "bad"}
        return {"success": True, "data": {"args": args, "call": calls}}

    monkeypatch.setattr("signoz_mcp.app.run_sz", fake_run_sz)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await _call_sz(client, "cache-fail-1", ["services", "--env", "prod"])
        second = await _call_sz(client, "cache-fail-2", ["services", "--env", "prod"])

    assert calls == 2
    assert first["success"] is False
    assert second["success"] is True


@pytest.mark.asyncio
async def test_mcp_cache_is_bounded(monkeypatch):
    app = _app(monkeypatch, cache_ttl_seconds="60", cache_max_entries="2")
    calls = 0

    async def fake_run_sz(args, settings):
        nonlocal calls
        calls += 1
        return {"success": True, "data": {"args": args, "call": calls}}

    monkeypatch.setattr("signoz_mcp.app.run_sz", fake_run_sz)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await _call_sz(client, "cache-bound-1", ["services", "--env", "a"])
        await _call_sz(client, "cache-bound-2", ["services", "--env", "b"])
        await _call_sz(client, "cache-bound-3", ["services", "--env", "c"])
        await _call_sz(client, "cache-bound-4", ["services", "--env", "a"])

    assert calls == 4


@pytest.mark.asyncio
async def test_mcp_singleflights_identical_concurrent_calls(monkeypatch):
    app = _app(monkeypatch, cache_ttl_seconds="0")
    calls = 0

    async def fake_run_sz(args, settings):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return {"success": True, "data": {"args": args}}

    monkeypatch.setattr("signoz_mcp.app.run_sz", fake_run_sz)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first, second = await asyncio.gather(
            _call_sz(client, "sf-1", ["query", "SELECT 1"]),
            _call_sz(client, "sf-2", ["query", "SELECT 1"]),
        )

    assert calls == 1
    assert first == second


@pytest.mark.asyncio
async def test_mcp_limits_concurrent_sz_runs(monkeypatch):
    app = _app(monkeypatch, max_concurrent_runs="2", cache_ttl_seconds="0")
    active = 0
    max_active = 0
    lock = asyncio.Lock()

    async def fake_run_sz(args, settings):
        nonlocal active, max_active
        async with lock:
            active += 1
            max_active = max(max_active, active)
        await asyncio.sleep(0.05)
        async with lock:
            active -= 1
        return {"success": True, "data": {"args": args}}

    monkeypatch.setattr("signoz_mcp.app.run_sz", fake_run_sz)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        results = await asyncio.gather(
            *[
                _call_sz(client, f"limit-{i}", ["query", f"SELECT {i}"])
                for i in range(5)
            ]
        )

    assert len(results) == 5
    assert max_active <= 2


# ---- compatibility, help, absolute windows, chunking -------------------------

import json  # noqa: E402


SOURCE_MCP_SQL = (
    "SELECT resources_string['container.image.tag'] AS image_tag, count() AS log_count "
    "FROM signoz_logs.distributed_logs_v2 WHERE timestamp >= {start_ns} "
    "AND resources_string['k8s.deployment.name'] = 'dify-api' GROUP BY image_tag"
)


def _tools_call(client, name, arguments, request_id=1):
    response = client.post(
        "/mcp",
        headers=AUTH_HEADER,
        json={
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    assert response.status_code == 200
    return response.json()["result"]


def test_source_mcp_sz_run_query_contract_is_unchanged(monkeypatch, tmp_path):
    """source-mcp calls sz_run ["query", <sql>, "--hours", "3"] and parses
    content[0].text as {"success": true, "data": [rows...]}. Keep it byte-stable."""
    argv_file = tmp_path / "argv.json"
    script = tmp_path / "fake_sz.py"
    script.write_text(
        "import json, sys\n"
        f"open({str(argv_file)!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
        "print(json.dumps([{'image_tag': '1.2.3', 'log_count': 42}]))\n"
    )
    app = _app(monkeypatch)
    monkeypatch.setenv("SIGNOZ_MCP_SZ_PATH", str(script))
    from signoz_mcp.settings import get_settings

    get_settings.cache_clear()
    client = TestClient(app)

    result = _tools_call(client, "sz_run", {"args": ["query", SOURCE_MCP_SQL, "--hours", "3"]})

    assert json.loads(argv_file.read_text()) == ["query", SOURCE_MCP_SQL, "--hours", "3", "--json", "-q"]
    assert result["isError"] is False
    assert len(result["content"]) == 1
    assert result["content"][0]["type"] == "text"
    expected = {"success": True, "data": [{"image_tag": "1.2.3", "log_count": 42}]}
    assert result["content"][0]["text"] == json.dumps(expected, ensure_ascii=False)
    assert result["structuredContent"] == expected


def test_source_mcp_query_runs_through_real_sz_script_with_hours(monkeypatch):
    """End-to-end through scripts/sz (HTTP replaced) to pin the --hours window."""
    from pathlib import Path

    sz_path = Path(__file__).resolve().parents[1] / "scripts" / "sz"
    app = _app(monkeypatch)
    monkeypatch.setenv("SIGNOZ_MCP_SZ_PATH", str(sz_path))
    from signoz_mcp.settings import get_settings

    get_settings.cache_clear()
    captured = {}

    async def fake_run_sz(args, settings):
        import importlib.machinery
        import importlib.util
        import io
        from contextlib import redirect_stdout

        loader = importlib.machinery.SourceFileLoader("sz_compat", str(sz_path))
        module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(module)
        monkeypatch.setattr(module.time, "time", lambda: 1_790_000_000)

        def fake_post(sql, start_ms, end_ms, panel_type="list"):
            captured.update(sql=sql, start_ms=start_ms, end_ms=end_ms)
            return {"data": {"result": [{"list": [{"data": {"image_tag": "1.2.3"}}]}]}}

        module.post_query = fake_post
        out = io.StringIO()
        with redirect_stdout(out):
            module.main([*args, "--json", "-q"])
        return {"success": True, "data": json.loads(out.getvalue())}

    monkeypatch.setattr("signoz_mcp.app.run_sz", fake_run_sz)
    client = TestClient(app)

    result = _tools_call(client, "sz_run", {"args": ["query", SOURCE_MCP_SQL, "--hours", "3"]})

    assert json.loads(result["content"][0]["text"]) == {"success": True, "data": [{"image_tag": "1.2.3"}]}
    assert f"timestamp >= {(1_790_000_000 - 3 * 3600) * 1_000_000_000} " in captured["sql"]
    assert captured["end_ms"] == 1_790_000_000 * 1000


@pytest.mark.parametrize("args", [["--help"], ["help"], ["services", "--help"]])
def test_sz_run_help_returns_command_list_without_running_sz(monkeypatch, args):
    client = _client(monkeypatch)

    async def fake_run_sz(args, settings):
        raise AssertionError("help must not spawn sz")

    monkeypatch.setattr("signoz_mcp.app.run_sz", fake_run_sz)

    result = _tools_call(client, "sz_run", {"args": args})

    assert result["isError"] is False
    names = [command["name"] for command in result["structuredContent"]["data"]["commands"]]
    assert names == (["services"] if args[0] == "services" else ["errors", "traceback", "query", "schema", "services"])


def test_sz_run_tool_description_lists_commands(monkeypatch):
    client = _client(monkeypatch)
    response = client.post(
        "/mcp", headers=AUTH_HEADER, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    )
    tools = {tool["name"]: tool for tool in response.json()["result"]["tools"]}

    description = tools["sz_run"]["description"]
    for name in ("errors", "traceback", "query <SQL>", "schema", "services"):
        assert name in description
    assert "--start ISO" in description


def test_sz_run_unsupported_command_explains_alternatives(monkeypatch):
    client = _client(monkeypatch)

    result = _tools_call(client, "sz_run", {"args": ["drop"]})

    assert result["isError"] is True
    assert result["structuredContent"]["code"] == "BAD_ARGUMENTS"
    assert "query" in result["structuredContent"]["error"]


def test_structured_tool_accepts_absolute_window_and_echoes_utc(monkeypatch):
    client = _client(monkeypatch)
    captured = []

    async def fake_run_sz(args, settings):
        captured.append(args)
        return {"success": True, "data": [{"value": 3}]}

    monkeypatch.setattr("signoz_mcp.app.run_sz", fake_run_sz)

    result = _tools_call(
        client,
        "signoz_aggregate_logs",
        {"start": "2026-10-01T08:00:00+08:00", "end": "2026-10-01T06:00:00"},
    )

    assert result["isError"] is False
    assert captured[0][-4:] == ["--start", "2026-10-01T00:00:00Z", "--end", "2026-10-01T06:00:00Z"]
    assert result["structuredContent"]["window"]["start"] == "2026-10-01T00:00:00Z"
    assert result["structuredContent"]["window"]["end"] == "2026-10-01T06:00:00Z"


def test_structured_tool_rejects_reversed_window(monkeypatch):
    client = _client(monkeypatch)

    result = _tools_call(
        client, "signoz_search_logs", {"start": "2026-10-02T00:00:00Z", "end": "2026-10-01T00:00:00Z"}
    )

    assert result["isError"] is True
    assert result["structuredContent"]["code"] == "BAD_ARGUMENTS"
    assert "earlier" in result["structuredContent"]["error"]


def test_long_aggregate_is_chunked_through_the_app(monkeypatch):
    client = _client(monkeypatch)
    calls = []

    async def fake_run_sz(args, settings):
        calls.append(args)
        return {"success": True, "data": [{"value": 1}]}

    monkeypatch.setattr("signoz_mcp.app.run_sz", fake_run_sz)

    result = _tools_call(
        client,
        "signoz_aggregate_logs",
        {"start": "2026-10-01T00:00:00Z", "end": "2026-10-31T00:00:00Z", "deployment": "dify-api-oltp"},
    )

    assert result["isError"] is False
    assert len(calls) == 30
    assert result["structuredContent"]["data"] == [{"value": 30}]
    assert result["structuredContent"]["chunking"]["chunks"] == 30


@pytest.mark.asyncio
async def test_run_sz_kills_process_when_cancelled(tmp_path, monkeypatch):
    from signoz_mcp.settings import Settings
    from signoz_mcp.sz_runner import run_sz

    marker = tmp_path / "marker.txt"
    script = tmp_path / "slow_sz.py"
    script.write_text(
        "import os, pathlib, time\n"
        "time.sleep(0.5)\n"
        "pathlib.Path(os.environ['MARKER']).write_text('still-running')\n"
    )
    monkeypatch.setenv("MARKER", str(marker))
    settings = Settings(
        SIGNOZ_MCP_API_KEY="x" * 32,
        SIGNOZ_MCP_SZ_PATH=str(script),
        SIGNOZ_MCP_TIMEOUT_SECONDS=5,
    )

    task = asyncio.create_task(run_sz(["services"], settings))
    await asyncio.sleep(0.15)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.6)

    assert not marker.exists()


def test_every_signoz_tool_is_annotated_read_only(monkeypatch):
    client = _client(monkeypatch)

    response = client.post(
        "/mcp",
        headers=AUTH_HEADER,
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )

    tools = response.json()["result"]["tools"]
    assert "sz_run" in {tool["name"] for tool in tools}
    for tool in tools:
        assert tool["annotations"]["readOnlyHint"] is True, tool["name"]
        assert tool["annotations"]["destructiveHint"] is False, tool["name"]
