from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from zendesk_mcp.mcp_schema import (
    READ_ONLY_TOOL_NAMES,
    TOOL_NAMES,
    TOOL_SCHEMAS,
    resolve_disabled_tools,
)
from zendesk_mcp.settings import Settings, get_settings
from zendesk_mcp.tools import _write_audit, handle_tool_call

ROOT = Path(__file__).resolve().parents[1]
API_KEY = "z" * 32
WRITE_TOOLS = {"add_internal_note", "update_ticket_status", "update_ticket_metadata"}
LOGIN_TOOLS = {"zendesk_auth_start", "zendesk_auth_complete"}
ANNOTATION_KEYS = {"readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"}


@pytest.fixture
def client_factory(monkeypatch, tmp_path):
    def make(disabled: str | None = None) -> TestClient:
        monkeypatch.setenv("ZENDESK_MCP_API_KEY", API_KEY)
        monkeypatch.setenv("ZENDESK_SUBDOMAIN", "example")
        monkeypatch.setenv("ZENDESK_AUTH_MODE", "api_token")
        monkeypatch.setenv("ZENDESK_MCP_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
        if disabled is None:
            monkeypatch.delenv("ZENDESK_MCP_DISABLED_TOOLS", raising=False)
        else:
            monkeypatch.setenv("ZENDESK_MCP_DISABLED_TOOLS", disabled)
        get_settings.cache_clear()
        from zendesk_mcp.app import create_app

        return TestClient(create_app())

    yield make
    get_settings.cache_clear()


def _rpc(client: TestClient, method: str, params: dict | None = None) -> dict:
    response = client.post(
        "/mcp",
        headers={"Authorization": "Bearer " + API_KEY},
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
    )
    assert response.status_code == 200
    return response.json()


def _listed(client: TestClient) -> dict[str, dict]:
    return {tool["name"]: tool for tool in _rpc(client, "tools/list")["result"]["tools"]}


def _settings(tmp_path: Path, **overrides) -> Settings:
    base = Settings(
        api_key=API_KEY, host="127.0.0.1", port=8100, workers=1,
        audit_log=str(tmp_path / "audit.jsonl"), allowed_custom_field_ids="",
        request_timeout_seconds=30.0, connect_timeout_seconds=3.0, read_timeout_seconds=20.0,
        max_retries=0, cache_ttl_seconds=0.0, cache_max_entries=0, download_max_bytes=1024,
        zendesk_subdomain="example", zendesk_email="", zendesk_api_token="",
    )
    return dataclasses.replace(base, **overrides)


# ----------------------------------------------------------------- annotations
def test_read_only_set_names_real_tools_and_includes_auth_status():
    assert READ_ONLY_TOOL_NAMES <= TOOL_NAMES
    assert "zendesk_auth_status" in READ_ONLY_TOOL_NAMES
    assert not (WRITE_TOOLS | LOGIN_TOOLS) & READ_ONLY_TOOL_NAMES
    assert TOOL_NAMES == READ_ONLY_TOOL_NAMES | WRITE_TOOLS | LOGIN_TOOLS


def test_every_tool_has_annotations_derived_from_read_only_set():
    for tool in TOOL_SCHEMAS:
        annotations = tool["annotations"]
        assert set(annotations) == ANNOTATION_KEYS, tool["name"]
        read_only = tool["name"] in READ_ONLY_TOOL_NAMES
        assert annotations["readOnlyHint"] is read_only
        assert annotations["destructiveHint"] is False
        assert annotations["idempotentHint"] is read_only
        assert annotations["openWorldHint"] is True


def test_tools_list_exposes_annotations(client_factory):
    tools = _listed(client_factory())
    assert set(tools) == TOOL_NAMES
    assert tools["zendesk_auth_status"]["annotations"]["readOnlyHint"] is True
    assert tools["get_ticket"]["annotations"]["readOnlyHint"] is True
    for name in WRITE_TOOLS | LOGIN_TOOLS:
        assert tools[name]["annotations"]["readOnlyHint"] is False, name


# ---------------------------------------------------------------- disabled tools
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", set()),
        (" , ,", set()),
        ("search_help_center,get_article", {"search_help_center", "get_article"}),
        ("  search_help_center ,\tget_article , ", {"search_help_center", "get_article"}),
        ("zendesk_get_article", {"get_article"}),
        ("auth_start, zendesk_auth_complete", {"zendesk_auth_start", "zendesk_auth_complete"}),
        ("no_such_tool", set()),
    ],
)
def test_resolve_disabled_tools(raw, expected):
    assert resolve_disabled_tools(raw) == expected


def test_disabled_tools_are_hidden_and_rejected_like_unknown_tools(client_factory):
    client = client_factory(" search_help_center , get_article ")
    tools = _listed(client)
    assert "search_help_center" not in tools and "get_article" not in tools
    assert set(tools) == TOOL_NAMES - {"search_help_center", "get_article"}

    unknown = _rpc(client, "tools/call", {"name": "does_not_exist", "arguments": {}})
    assert unknown["error"] == {"code": -32601, "message": "unknown tool: does_not_exist"}
    for name in ("search_help_center", "get_article"):
        disabled = _rpc(client, "tools/call", {"name": name, "arguments": {"query": "x", "article_id": 1}})
        assert disabled["error"] == {"code": -32601, "message": f"unknown tool: {name}"}
        assert "result" not in disabled


def test_empty_disabled_tools_keeps_all_tools(client_factory):
    assert set(_listed(client_factory(""))) == TOOL_NAMES


def test_handle_tool_call_rejects_disabled_tool_like_unknown(tmp_path, monkeypatch):
    settings = _settings(tmp_path, disabled_tools="get_article")
    import zendesk_mcp.tools as tools_module

    monkeypatch.setattr(tools_module, "_get_article", lambda *a: pytest.fail("disabled tool ran"))
    unknown = {"success": False, "code": "UNKNOWN_TOOL"}
    assert handle_tool_call("no_such_tool", {}, settings=settings) == unknown | {"error": "unknown tool: no_such_tool"}
    assert handle_tool_call("get_article", {"article_id": 1}, settings=settings) == unknown | {
        "error": "unknown tool: get_article"
    }


def test_settings_read_disabled_tools_env(monkeypatch):
    monkeypatch.setenv("ZENDESK_MCP_DISABLED_TOOLS", "search_help_center, get_article")
    get_settings.cache_clear()
    try:
        assert get_settings().disabled_tool_set == {"search_help_center", "get_article"}
    finally:
        get_settings.cache_clear()


# --------------------------------------------------------------- deploy config
def test_runtime_config_disables_help_center_and_persists_audit_log():
    config = json.loads((ROOT / "deploy" / "runtime.json").read_text())
    env = config["environment"]
    assert env["ZENDESK_MCP_DISABLED_TOOLS"] == "search_help_center,get_article"
    assert env["ZENDESK_MCP_AUDIT_LOG"] == "/var/lib/mcp/logs/audit.jsonl"
    assert env["ZENDESK_AUTH_MODE"] == "oauth"
    assert "ZENDESK_API_TOKEN" not in config["secrets"]
    assert "ZENDESK_API_TOKEN" not in env


def test_audit_log_creates_missing_parent_directories(tmp_path):
    audit = tmp_path / "data" / "logs" / "audit.jsonl"
    audit_id = _write_audit(_settings(tmp_path, audit_log=str(audit)), {"action": "probe"})
    line = json.loads(audit.read_text())
    assert line["audit_id"] == audit_id and line["action"] == "probe"
