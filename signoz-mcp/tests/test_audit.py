from __future__ import annotations

import json
import logging
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from test_app import AUTH_HEADER, _app

AUDIT_LOGGER = "signoz_mcp.audit"
TIME_FORMAT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


def _call(client, name, arguments, request_id=1):
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


def _audit_lines(caplog) -> list[dict]:
    return [json.loads(r.getMessage()) for r in caplog.records if r.name == AUDIT_LOGGER]


@pytest.fixture
def audit_caplog(caplog):
    caplog.set_level(logging.INFO, logger=AUDIT_LOGGER)
    return caplog


def test_successful_sz_run_emits_one_audit_line(monkeypatch, audit_caplog):
    client = TestClient(_app(monkeypatch))

    async def fake_run_sz(args, settings):
        return {"success": True, "data": [{"body": "secret log body"}]}

    monkeypatch.setattr("signoz_mcp.app.run_sz", fake_run_sz)
    sql = "SELECT body FROM signoz_logs.distributed_logs_v2 WHERE tenant_id = 'tenant-abc-123'"
    result = _call(client, "sz_run", {"args": ["query", sql, "--hours", "3"]})

    [line] = _audit_lines(audit_caplog)
    assert set(line) == {"time", "tool", "duration_ms", "outcome", "error", "response_chars", "args"}
    assert TIME_FORMAT.fullmatch(line["time"])
    assert line["tool"] == "sz_run"
    assert isinstance(line["duration_ms"], int) and line["duration_ms"] >= 0
    assert line["outcome"] == "success"
    assert line["error"] is None
    assert line["response_chars"] == len(result["content"][0]["text"])
    assert line["args"] == {"keys": ["args"], "values": {"command": "query"}}
    raw = audit_caplog.records[-1].getMessage()
    for secret in ("SELECT", "tenant-abc-123", "secret log body", "--hours"):
        assert secret not in raw


def test_structured_tool_logs_only_allowlisted_values(monkeypatch, audit_caplog):
    client = TestClient(_app(monkeypatch))

    async def fake_run_sz(args, settings):
        return {"success": True, "data": []}

    monkeypatch.setattr("signoz_mcp.app.run_sz", fake_run_sz)
    arguments = {
        "timeRange": "1h",
        "environment": "production",
        "deployment": "dify-api",
        "tenant": "tenant-secret-42",
        "traceId": "a" * 32,
        "searchText": "password reset for alice@example.com",
        "severity": "ERROR",
        "limit": 5,
    }
    result = _call(client, "signoz_search_logs", arguments)
    assert result["isError"] is False

    [line] = _audit_lines(audit_caplog)
    assert line["tool"] == "signoz_search_logs"
    assert line["outcome"] == "success"
    assert line["args"]["keys"] == sorted(arguments)
    assert line["args"]["values"] == {
        "timeRange": "1h",
        "environment": "production",
        "deployment": "dify-api",
    }
    raw = audit_caplog.records[-1].getMessage()
    for secret in ("tenant-secret-42", "a" * 32, "alice@example.com", "password"):
        assert secret not in raw


def test_service_value_is_logged_for_traces(monkeypatch, audit_caplog):
    client = TestClient(_app(monkeypatch))

    async def fake_run_sz(args, settings):
        return {"success": True, "data": []}

    monkeypatch.setattr("signoz_mcp.app.run_sz", fake_run_sz)
    _call(client, "signoz_search_traces", {"service": "api", "operation": "POST /secret", "timeRange": "1h"})
    [line] = _audit_lines(audit_caplog)
    assert line["args"]["values"] == {"service": "api", "timeRange": "1h"}
    assert "/secret" not in audit_caplog.records[-1].getMessage()


def test_bad_arguments_are_logged_as_failed_with_code(monkeypatch, audit_caplog):
    client = TestClient(_app(monkeypatch))
    result = _call(client, "sz_run", {"args": "query SELECT 1"})
    assert result["isError"] is True

    [line] = _audit_lines(audit_caplog)
    assert line["outcome"] == "failed"
    assert line["error"] == "BAD_ARGUMENTS"
    assert line["response_chars"] == len(result["content"][0]["text"])
    assert line["args"] == {"keys": ["args"], "values": {}}
    assert "SELECT" not in audit_caplog.records[-1].getMessage()


def test_sz_failure_code_is_logged(monkeypatch, audit_caplog):
    client = TestClient(_app(monkeypatch))

    async def fake_run_sz(args, settings):
        return {"success": False, "code": "SZ_NONZERO", "error": "upstream said tenant-xyz"}

    monkeypatch.setattr("signoz_mcp.app.run_sz", fake_run_sz)
    _call(client, "sz_run", {"args": ["errors", "--env", "production"]})
    [line] = _audit_lines(audit_caplog)
    assert (line["outcome"], line["error"]) == ("failed", "SZ_NONZERO")
    assert line["args"]["values"] == {"command": "errors"}
    assert "tenant-xyz" not in audit_caplog.records[-1].getMessage()


def test_unknown_tool_name_is_not_echoed(monkeypatch, audit_caplog):
    client = TestClient(_app(monkeypatch))
    _call(client, "drop table users; secret", {"x": 1, "bad key!": 2})
    [line] = _audit_lines(audit_caplog)
    assert line["tool"] == "(unknown)"
    assert line["outcome"] == "failed"
    assert line["error"] == "BAD_ARGUMENTS"
    assert line["args"] == {"keys": ["x"], "values": {}}
    assert "secret" not in audit_caplog.records[-1].getMessage()


def test_non_tool_methods_do_not_emit_audit(monkeypatch, audit_caplog):
    client = TestClient(_app(monkeypatch))
    for method in ("initialize", "tools/list", "ping"):
        client.post("/mcp", headers=AUTH_HEADER, json={"jsonrpc": "2.0", "id": 1, "method": method})
    assert _audit_lines(audit_caplog) == []


def test_unexpected_exception_is_logged_and_still_raised(monkeypatch, audit_caplog):
    client = TestClient(_app(monkeypatch))

    async def broken_run_sz(args, settings):
        raise RuntimeError("boom with secret detail")

    monkeypatch.setattr("signoz_mcp.app.run_sz", broken_run_sz)
    with pytest.raises(RuntimeError):
        _call(client, "sz_run", {"args": ["errors"]})
    [line] = _audit_lines(audit_caplog)
    assert (line["outcome"], line["error"], line["response_chars"]) == ("failed", "INTERNAL_ERROR", 0)
    assert "secret detail" not in audit_caplog.records[-1].getMessage()


def test_audit_failure_never_breaks_tool_call(monkeypatch):
    client = TestClient(_app(monkeypatch))

    async def fake_run_sz(args, settings):
        return {"success": True, "data": [{"ok": True}]}

    def exploding_info(*args, **kwargs):
        raise OSError("stdout closed")

    monkeypatch.setattr("signoz_mcp.app.run_sz", fake_run_sz)
    monkeypatch.setattr("signoz_mcp.audit.audit_logger.info", exploding_info)
    result = _call(client, "sz_run", {"args": ["errors"]})
    assert result["isError"] is False
    assert result["structuredContent"]["data"] == [{"ok": True}]

    def exploding_summary(*args, **kwargs):
        raise ValueError("cannot summarize")

    monkeypatch.setattr("signoz_mcp.audit.summarize_arguments", exploding_summary)
    assert _call(client, "sz_run", {"args": ["errors"]})["isError"] is False


def test_configure_audit_logging_writes_json_to_stdout(capsys):
    from signoz_mcp.audit import audit_logger, configure_audit_logging, emit_tool_audit

    saved = (list(audit_logger.handlers), audit_logger.level, audit_logger.propagate)
    try:
        configure_audit_logging()
        configure_audit_logging()  # idempotent: still exactly one stdout handler
        assert len(audit_logger.handlers) == len(saved[0]) + 1
        emit_tool_audit(
            tool="sz_run",
            known_tools={"sz_run"},
            arguments={"args": ["help"]},
            started=time.monotonic(),
            payload={"success": True},
            response_text="{}",
        )
        out = capsys.readouterr().out.strip().splitlines()
        assert len(out) == 1
        line = json.loads(out[0])
        assert line["tool"] == "sz_run" and line["response_chars"] == 2
        assert line["args"]["values"] == {"command": "help"}
    finally:
        audit_logger.handlers[:] = saved[0]
        audit_logger.setLevel(saved[1])
        audit_logger.propagate = saved[2]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_real_server_startup_emits_audit_line_on_stdout(tmp_path: Path):
    port = _free_port()
    key = "k" * 32
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SIGNOZ", "PYTHONUNBUFFERED"))}
    env.update({
        "SIGNOZ_MCP_API_KEY": key,
        "SIGNOZ_MCP_HOST": "127.0.0.1",
        "SIGNOZ_MCP_PORT": str(port),
        "SIGNOZ_MCP_SZ_PATH": "/nonexistent/sz",
        "SIGNOZ_URL": "https://signoz.example.invalid",
        "SIGNOZ_API_KEY": "sn_synthetic",
    })
    proc = subprocess.Popen(
        [sys.executable, "-c", "from signoz_mcp.app import main; main()"],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        base = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 15
        with httpx.Client(trust_env=False, timeout=5) as http:
            while True:
                try:
                    if http.get(base + "/health").status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                assert proc.poll() is None and time.monotonic() < deadline, "server did not start"
                time.sleep(0.1)
            response = http.post(
                base + "/mcp",
                headers={"Authorization": "Bearer " + key},
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "sz_run", "arguments": {"args": ["help"]}},
                },
            )
            assert response.status_code == 200
            text = response.json()["result"]["content"][0]["text"]
    finally:
        proc.terminate()
        stdout, stderr = proc.communicate(timeout=10)

    audit = [json.loads(line) for line in stdout.splitlines() if line.startswith("{")]
    assert len(audit) == 1, (stdout, stderr)
    assert audit[0]["tool"] == "sz_run"
    assert audit[0]["outcome"] == "success"
    assert audit[0]["response_chars"] == len(text)
    assert key not in stdout and key not in stderr
