"""DB Gateway MCP stdio smoke tests."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_db_gateway_mcp_starts_without_site_packages():
    """The MCP script is launched by shebang in Docker, so avoid third-party deps."""
    script = Path(__file__).resolve().parents[2] / "scripts" / "db_gateway_mcp.py"
    proc = subprocess.Popen(
        [sys.executable, "-S", str(script)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdin is not None
        assert proc.stdout is not None
        proc.stdin.write(json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {},
        }) + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
        assert line
        data = json.loads(line)
        assert data["id"] == 1
        assert data["result"]["serverInfo"]["name"] == "db-gateway-mcp"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)


def _load_stdio_module():
    import importlib.util

    script = Path(__file__).resolve().parents[2] / "scripts" / "db_gateway_mcp.py"
    spec = importlib.util.spec_from_file_location("db_gateway_mcp_stdio", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None


def test_stdio_forwards_paging_and_format(monkeypatch):
    module = _load_stdio_module()
    captured = {}

    def fake_urlopen(req, timeout):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        captured["timeout"] = timeout
        return _FakeResponse({"success": True, "row_count": 0})

    monkeypatch.setenv("DB_GATEWAY_URL", "http://gateway.invalid")
    monkeypatch.setenv("DB_GATEWAY_API_KEY", "k")
    monkeypatch.delenv("DB_GATEWAY_TIMEOUT", raising=False)
    monkeypatch.setattr(module.request, "urlopen", fake_urlopen)

    data = module._gateway_query(
        {"database": "prod", "sql": "SELECT 1", "offset": 10, "limit": 5, "format": "json"},
    )

    assert data["success"] is True
    assert captured["body"] == {
        "database": "prod",
        "sql": "SELECT 1",
        "ticket_id": "",
        "offset": 10,
        "limit": 5,
        "format": "json",
    }
    assert captured["timeout"] == 45.0


def test_stdio_omits_unset_optional_args(monkeypatch):
    module = _load_stdio_module()
    captured = {}

    def fake_urlopen(req, timeout):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResponse({"success": True})

    monkeypatch.setenv("DB_GATEWAY_URL", "http://gateway.invalid")
    monkeypatch.setenv("DB_GATEWAY_API_KEY", "k")
    monkeypatch.setattr(module.request, "urlopen", fake_urlopen)

    module._gateway_query({"database": "prod", "sql": "SELECT 1", "ticket_id": "77"})

    assert captured["body"] == {"database": "prod", "sql": "SELECT 1", "ticket_id": "77"}


def test_stdio_tools_list_matches_http_schema():
    from db_gateway.mcp_schema import TOOL_SCHEMA

    module = _load_stdio_module()
    assert module.TOOL_SCHEMA is TOOL_SCHEMA or module.TOOL_SCHEMA == TOOL_SCHEMA
