from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

_KEY = "x" * 32


def _make_repo(base: Path) -> None:
    repo = base / "dify"
    repo.mkdir(parents=True)
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "test@example.com"),
        ("config", "user.name", "Test User"),
    ):
        subprocess.run(("git", *args), cwd=repo, check=True, capture_output=True)
    (repo / "app.py").write_text("SECRET_PATTERN_TARGET = 1\n", encoding="utf-8")
    subprocess.run(("git", "add", "."), cwd=repo, check=True, capture_output=True)
    subprocess.run(("git", "commit", "-q", "-m", "init"), cwd=repo, check=True, capture_output=True)


def _client(monkeypatch, tmp_path: Path) -> TestClient:
    _make_repo(tmp_path)
    monkeypatch.setenv("SOURCE_MCP_API_KEY", _KEY)
    monkeypatch.setenv("SOURCE_MCP_REPOS_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("SOURCE_MCP_REPOS", "dify")
    monkeypatch.setenv("SOURCE_MCP_RUNTIME_REF_OVERRIDES", '{"dify":"HEAD"}')
    from source_mcp.app import create_app
    from source_mcp.settings import get_settings

    get_settings.cache_clear()
    return TestClient(create_app())


def _call(client: TestClient, name: str, arguments: Any) -> dict[str, Any]:
    response = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {_KEY}"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    assert response.status_code == 200
    return response.json()


def _audit_lines(text: str) -> list[dict[str, Any]]:
    lines = []
    for line in text.splitlines():
        if line.startswith("{"):
            record = json.loads(line)
            if "tool" in record and "duration_ms" in record:
                lines.append(record)
    return lines


def test_successful_call_emits_one_json_line(monkeypatch, tmp_path, capsys):
    client = _client(monkeypatch, tmp_path)
    capsys.readouterr()

    body = _call(
        client,
        "source_search",
        {"repo": "dify", "pattern": "SECRET_PATTERN_TARGET", "glob": "*.py", "ref": "HEAD", "limit": 5},
    )

    records = _audit_lines(capsys.readouterr().out)
    assert len(records) == 1
    record = records[0]
    assert set(record) == {
        "time", "tool", "duration_ms", "outcome", "error", "response_chars", "args",
    }
    assert record["tool"] == "source_search"
    assert record["outcome"] == "success"
    assert record["error"] is None
    assert isinstance(record["duration_ms"], int) and record["duration_ms"] >= 0
    assert time.strptime(record["time"], "%Y-%m-%dT%H:%M:%SZ")
    assert record["response_chars"] == len(body["result"]["content"][0]["text"])
    assert record["args"]["keys"] == ["glob", "limit", "pattern", "ref", "repo"]
    assert record["args"]["values"] == {"repo": "dify", "ref": "HEAD", "limit": 5}
    # Search patterns and globs are logged by key name only.
    raw = json.dumps(record)
    assert "SECRET_PATTERN_TARGET" not in raw
    assert "*.py" not in raw


def test_failed_call_logs_error_code(monkeypatch, tmp_path, capsys):
    client = _client(monkeypatch, tmp_path)
    capsys.readouterr()

    body = _call(client, "source_read", {"repo": "nope", "filepath": "tenant/abc.txt"})

    assert body["result"]["isError"] is True
    (record,) = _audit_lines(capsys.readouterr().out)
    assert record["outcome"] == "failed"
    assert record["error"] == "UNKNOWN_REPO"
    assert record["args"] == {"keys": ["filepath", "repo"], "values": {"repo": "nope"}}
    assert "tenant/abc.txt" not in json.dumps(record)


def test_unknown_tool_and_non_object_arguments_are_logged(monkeypatch, tmp_path, capsys):
    client = _client(monkeypatch, tmp_path)
    capsys.readouterr()

    _call(client, "no_such_tool", ["a", "b"])

    (record,) = _audit_lines(capsys.readouterr().out)
    assert record["tool"] == "no_such_tool"
    assert record["error"] == "UNKNOWN_TOOL"
    assert record["args"]["keys"] == []


def test_non_tool_methods_are_not_logged(monkeypatch, tmp_path, capsys):
    client = _client(monkeypatch, tmp_path)
    capsys.readouterr()

    client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {_KEY}"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )

    assert _audit_lines(capsys.readouterr().out) == []


def test_audit_failure_never_breaks_tool_call(monkeypatch, tmp_path):
    from source_mcp import app as app_module

    client = _client(monkeypatch, tmp_path)

    def boom(*_args, **_kwargs):
        raise RuntimeError("log sink down")

    monkeypatch.setattr(app_module._AUDIT, "info", boom)

    body = _call(client, "repo_status", {})

    assert body["result"]["structuredContent"]["success"] is True


def test_configure_audit_logging_is_idempotent():
    from source_mcp import app as app_module

    app_module.configure_audit_logging()
    app_module.configure_audit_logging()

    handlers = [h for h in app_module._AUDIT.handlers if isinstance(h, app_module._StdoutHandler)]
    assert len(handlers) == 1
    assert app_module._AUDIT.level == 20  # logging.INFO
    assert app_module._AUDIT.propagate is False


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _post(port: int, payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/mcp",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {_KEY}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read())


def test_real_server_startup_writes_audit_line_to_stdout(tmp_path):
    port = _free_port()
    env = {
        **os.environ,
        "SOURCE_MCP_API_KEY": _KEY,
        "SOURCE_MCP_HOST": "127.0.0.1",
        "SOURCE_MCP_PORT": str(port),
        "SOURCE_MCP_REPOS_BASE_DIR": str(tmp_path / "repos"),
        "SOURCE_MCP_REPOS": "dify",
        "SOURCE_MCP_BACKGROUND_REFRESH_SECONDS": "0",
        "SOURCE_MCP_SIGNOZ_MCP_URL": "",
    }
    proc = subprocess.Popen(
        [sys.executable, "-c", "from source_mcp.app import main; main()"],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2).read()
                break
            except (urllib.error.URLError, ConnectionError):
                if proc.poll() is not None or time.monotonic() > deadline:
                    raise AssertionError(proc.stderr.read() if proc.stderr else "server did not start")
                time.sleep(0.2)
        body = _post(
            port,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "list_repo_tags", "arguments": {"repo": "rbac", "pattern": "v1*"}},
            },
        )
        assert body["result"]["isError"] is True
    finally:
        proc.terminate()
        stdout, _stderr = proc.communicate(timeout=15)

    records = _audit_lines(stdout)
    assert len(records) == 1, stdout
    assert records[0]["tool"] == "list_repo_tags"
    assert records[0]["outcome"] == "failed"
    assert records[0]["error"] == "UNKNOWN_REPO"
    assert records[0]["args"] == {"keys": ["pattern", "repo"], "values": {"repo": "rbac"}}
