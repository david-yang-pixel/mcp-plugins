from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from github_mcp import mcp as mcp_module

SECRET_QUERY = "very-private-customer-query tenant 7f3c"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("GITHUB_MCP_API_KEY", "github-secret")
    from github_mcp.settings import get_settings

    get_settings.cache_clear()
    from github_mcp.app import create_service_app

    yield TestClient(create_service_app("github-mcp"))
    get_settings.cache_clear()


def _call(client: TestClient, name: str, arguments) -> dict:
    response = client.post(
        "/mcp",
        headers={"Authorization": "Bearer github-secret"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    assert response.status_code == 200
    return response.json()


def _audit_records(caplog) -> list[dict]:
    return [json.loads(r.getMessage()) for r in caplog.records if r.name == "github_mcp.audit"]


def test_successful_tool_call_emits_one_audit_line_without_query(client, caplog) -> None:
    caplog.set_level(logging.INFO, logger="github_mcp.audit")
    payload = {"success": True, "data": {"items": [], "query": SECRET_QUERY}}
    with patch("github_mcp.tools._HANDLERS", {"github_search_issues": AsyncMock(return_value=payload)}):
        body = _call(
            client,
            "github_search_issues",
            {"repo": "langgenius/dify", "query": SECRET_QUERY, "limit": 3},
        )

    records = _audit_records(caplog)
    assert len(records) == 1
    record = records[0]
    assert set(record) == {"time", "tool", "duration_ms", "outcome", "error", "response_chars", "args"}
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", record["time"])
    assert record["tool"] == "github_search_issues"
    assert isinstance(record["duration_ms"], int) and record["duration_ms"] >= 0
    assert record["outcome"] == "success"
    assert record["error"] is None
    assert record["response_chars"] == len(body["result"]["content"][0]["text"])
    assert record["args"] == {
        "keys": ["limit", "query", "repo"],
        "values": {"limit": 3, "repo": "langgenius/dify"},
    }
    # The search query must never reach the log, neither from args nor from the response.
    for r in caplog.records:
        assert SECRET_QUERY not in r.getMessage()
        assert "7f3c" not in r.getMessage()


def test_failed_tool_call_logs_error_code(client, caplog) -> None:
    caplog.set_level(logging.INFO, logger="github_mcp.audit")
    payload = {"success": False, "code": "REPO_NOT_ACCESSIBLE", "error": "nope"}
    with patch("github_mcp.tools._HANDLERS", {"github_code_search": AsyncMock(return_value=payload)}):
        _call(client, "github_code_search", {"query": SECRET_QUERY, "path": "api/secret", "scope": "o/r"})

    [record] = _audit_records(caplog)
    assert record["outcome"] == "failed"
    assert record["error"] == "REPO_NOT_ACCESSIBLE"
    assert record["args"] == {"keys": ["path", "query", "scope"], "values": {"scope": "o/r"}}
    assert SECRET_QUERY not in caplog.text and "api/secret" not in caplog.text


def test_unknown_tool_and_bad_arguments_are_audited(client, caplog) -> None:
    caplog.set_level(logging.INFO, logger="github_mcp.audit")
    _call(client, "github_nope", {})
    _call(client, "github_get_pr", ["not", "an", "object"])

    unknown, bad = _audit_records(caplog)
    assert unknown["tool"] == "github_nope" and unknown["outcome"] == "failed"
    assert unknown["error"] == "UNKNOWN_TOOL"
    assert bad["error"] == "BAD_ARGUMENTS" and bad["args"] == {"keys": [], "values": {}}


def test_audit_failure_never_breaks_the_tool_call(client, caplog, monkeypatch) -> None:
    def explode(*args, **kwargs):
        raise RuntimeError("log sink down")

    monkeypatch.setattr(mcp_module.audit, "info", explode)
    payload = {"success": True, "data": {}}
    with patch("github_mcp.tools._HANDLERS", {"github_get_pr": AsyncMock(return_value=payload)}):
        body = _call(client, "github_get_pr", {"repo": "o/r", "pr_number": 1})
    assert body["result"]["isError"] is False


def test_handler_exception_is_still_audited(caplog) -> None:
    caplog.set_level(logging.INFO, logger="github_mcp.audit")
    import time

    mcp_module.audit_tool_call(tool="github_get_pr", arguments={"repo": "o/r"}, started=time.monotonic(), response=None)
    [record] = _audit_records(caplog)
    assert record["outcome"] == "failed" and record["error"] == "INTERNAL_ERROR"


def test_configure_logging_emits_audit_lines_on_stdout_at_info() -> None:
    # Run the real entrypoint logging setup in a fresh interpreter, then start uvicorn's
    # logging config the same way main_github() does, and check the line lands on stdout.
    code = (
        "import logging.config, time, uvicorn.config\n"
        "from github_mcp.mcp import configure_logging, audit_tool_call\n"
        "configure_logging()\n"
        "logging.config.dictConfig(uvicorn.config.LOGGING_CONFIG)\n"
        "resp = {'jsonrpc': '2.0', 'id': 1, 'result': {'content': [{'type': 'text', 'text': 'xx'}],"
        " 'structuredContent': {'success': True}, 'isError': False}}\n"
        "audit_tool_call(tool='github_get_pr', arguments={'repo': 'o/r'}, started=time.monotonic(), response=resp)\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    lines = [json.loads(line) for line in proc.stdout.splitlines() if line.startswith("{")]
    assert len(lines) == 1
    assert lines[0]["tool"] == "github_get_pr" and lines[0]["response_chars"] == 2
    assert "github_get_pr" not in proc.stderr


def test_configure_logging_keeps_httpx_request_urls_out_of_stdout() -> None:
    # httpx logs "HTTP Request: GET <url>" at INFO; the search query sits in that URL.
    code = (
        "import logging, httpx\n"
        "from github_mcp.mcp import configure_logging\n"
        "configure_logging()\n"
        "configure_logging()\n"
        "t = httpx.MockTransport(lambda r: httpx.Response(200, json={}))\n"
        "httpx.Client(transport=t).get('https://api.github.com/search/issues', params={'q': 'secret-term'})\n"
        "assert logging.getLogger().getEffectiveLevel() == logging.WARNING\n"
        "assert len(logging.getLogger('github_mcp.audit').handlers) == 1\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert "secret-term" not in proc.stdout + proc.stderr


def test_main_github_configures_logging_before_serving(monkeypatch) -> None:
    from github_mcp import app as app_module

    calls: list[str] = []
    monkeypatch.setattr(app_module, "configure_logging", lambda: calls.append("logging"))
    monkeypatch.setattr(app_module, "_run", lambda *a, **k: calls.append("run"))
    app_module.main_github()
    assert calls == ["logging", "run"]
