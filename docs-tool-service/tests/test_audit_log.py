"""One JSON audit line per tools/call (same shape as difyctl-mcp's audit log)."""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import subprocess
import sys
import time
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from docs_tool_service import app as app_module
from docs_tool_service.mcp import failure, success

KEY = "audit-test-key"
TIME_FORMAT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
FIELDS = {"time", "tool", "duration_ms", "outcome", "error", "response_chars", "args"}


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


@pytest.fixture
def audit_lines(monkeypatch):
    monkeypatch.setenv("DOCS_TOOL_SERVICE_API_KEY", KEY)
    from docs_tool_service.settings import get_settings

    get_settings.cache_clear()
    handler = _ListHandler()
    logger = logging.getLogger("docs_tool_service.audit")
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    yield handler.lines
    logger.removeHandler(handler)
    logger.setLevel(previous_level)
    get_settings.cache_clear()


def _use_handler(monkeypatch, handler) -> None:
    monkeypatch.setitem(app_module.SERVICE_DEFS["docs-tool-service"], "handler", handler)


def _call(name: str, arguments: Any, *, client: TestClient | None = None) -> httpx.Response:
    client = client or TestClient(app_module.create_app())
    return client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {KEY}"},
        json={
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )


def _only_line(lines: list[str]) -> dict[str, Any]:
    assert len(lines) == 1, lines
    entry = json.loads(lines[0])
    assert set(entry) == FIELDS
    assert TIME_FORMAT.match(entry["time"])
    assert isinstance(entry["duration_ms"], int) and entry["duration_ms"] >= 0
    return entry


def test_successful_call_logs_one_line_without_query_text(monkeypatch, audit_lines) -> None:
    async def handler(name, arguments, settings):
        return success({"results": [{"title": "知识库"}]})

    _use_handler(monkeypatch, handler)
    response = _call(
        "official_docs_search",
        {"query": "secret customer question", "edition": "cloud", "language": "zh", "limit": 3},
    )

    assert response.status_code == 200
    entry = _only_line(audit_lines)
    assert entry["tool"] == "official_docs_search"
    assert entry["outcome"] == "success"
    assert entry["error"] is None
    assert entry["response_chars"] == len(response.json()["result"]["content"][0]["text"])
    assert entry["args"] == {"edition": "cloud", "language": "zh", "limit": 3, "query": None}
    assert "secret customer question" not in audit_lines[0]


def test_failed_tool_result_logs_error_code(monkeypatch, audit_lines) -> None:
    async def handler(name, arguments, settings):
        return failure("BAD_ARGUMENT", "only Dify official hosts are allowed")

    _use_handler(monkeypatch, handler)
    response = _call("official_docs_read", {"url": "https://example.com/private?token=x"})

    assert response.json()["result"]["isError"] is True
    entry = _only_line(audit_lines)
    assert entry["outcome"] == "failed"
    assert entry["error"] == "BAD_ARGUMENT"
    assert entry["args"] == {"url": None}
    assert "example.com" not in audit_lines[0]


def test_unknown_tool_and_bad_arguments_are_logged(audit_lines) -> None:
    _call("github_search_prs", {})
    _call("official_docs_search", ["not", "an", "object"])

    unknown, bad = (json.loads(line) for line in audit_lines)
    assert (unknown["tool"], unknown["outcome"], unknown["error"]) == (
        "github_search_prs",
        "failed",
        -32601,
    )
    assert (bad["outcome"], bad["error"], bad["args"]) == ("failed", "BAD_ARGUMENTS", None)


def test_long_or_structured_allowlisted_values_are_not_logged(monkeypatch, audit_lines) -> None:
    async def handler(name, arguments, settings):
        return success({})

    _use_handler(monkeypatch, handler)
    _call("official_site_list", {"section": "x" * 200, "language": {"nested": 1}, "cursor": 0})

    assert _only_line(audit_lines)["args"] == {"cursor": 0, "language": None, "section": None}


def test_handler_exception_is_logged_and_still_raised(monkeypatch, audit_lines) -> None:
    async def handler(name, arguments, settings):
        raise RuntimeError("boom")

    _use_handler(monkeypatch, handler)
    client = TestClient(app_module.create_app(), raise_server_exceptions=False)
    response = _call("official_docs_search", {"query": "x"}, client=client)

    assert response.status_code == 500
    entry = _only_line(audit_lines)
    assert (entry["outcome"], entry["error"]) == ("failed", "INTERNAL_ERROR")


def test_logging_failure_never_breaks_the_call(monkeypatch, audit_lines) -> None:
    async def handler(name, arguments, settings):
        return success({"ok": 1})

    def broken(*args, **kwargs):
        raise RuntimeError("log sink down")

    _use_handler(monkeypatch, handler)
    monkeypatch.setattr(app_module.audit, "info", broken)
    response = _call("official_docs_search", {"query": "x"})

    assert response.status_code == 200
    assert response.json()["result"]["structuredContent"] == {"success": True, "data": {"ok": 1}}


def test_non_tool_methods_are_not_logged(audit_lines) -> None:
    client = TestClient(app_module.create_app())
    client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {KEY}"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )

    assert audit_lines == []


def test_configure_audit_logging_writes_info_lines_to_given_stream(monkeypatch) -> None:
    import io

    logger = logging.getLogger("docs_tool_service.audit")
    monkeypatch.setattr(logger, "handlers", [])
    monkeypatch.setattr(logger, "propagate", True)
    monkeypatch.setattr(logger, "level", logging.NOTSET)
    stream = io.StringIO()

    app_module.configure_audit_logging(stream)
    app_module.configure_audit_logging(stream)  # idempotent: still one handler
    logger.info('{"tool": "x"}')

    assert stream.getvalue() == '{"tool": "x"}\n'
    assert len(logger.handlers) == 1
    assert logger.propagate is False


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_real_server_startup_prints_audit_line_to_stdout(tmp_path) -> None:
    """Start the service exactly like the Dockerfile CMD and read its stdout."""
    port = _free_port()
    env = {
        **os.environ,
        "DOCS_TOOL_SERVICE_API_KEY": KEY,
        "DOCS_TOOL_SERVICE_HOST": "127.0.0.1",
        "DOCS_TOOL_SERVICE_PORT": str(port),
        "PYTHONUNBUFFERED": "1",
    }
    process = subprocess.Popen(
        [sys.executable, "-c", "from docs_tool_service.app import main_docs; main_docs()"],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        base = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 20
        while True:
            try:
                if httpx.get(f"{base}/health", timeout=1, trust_env=False).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            assert process.poll() is None, process.communicate()
            assert time.monotonic() < deadline, "server did not start"
            time.sleep(0.1)
        # A non-official URL fails validation before any network access.
        response = httpx.post(
            f"{base}/mcp",
            headers={"Authorization": f"Bearer {KEY}"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "official_docs_read",
                    "arguments": {"url": "https://example.com/x", "max_chars": 10},
                },
            },
            timeout=10,
            trust_env=False,
        )
        assert response.json()["result"]["structuredContent"]["code"] == "BAD_ARGUMENT"
    finally:
        process.terminate()
        stdout, stderr = process.communicate(timeout=10)

    entries = [json.loads(line) for line in stdout.splitlines() if line.startswith("{")]
    assert len(entries) == 1, (stdout, stderr)
    entry = entries[0]
    assert set(entry) == FIELDS
    assert entry["tool"] == "official_docs_read"
    assert (entry["outcome"], entry["error"]) == ("failed", "BAD_ARGUMENT")
    assert entry["args"] == {"max_chars": 10, "url": None}
    assert '"tool"' not in stderr
