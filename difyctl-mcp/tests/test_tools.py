"""Acceptance items 8-13 and the tool contract over real HTTP with the fake difyctl."""
import base64
import os
from pathlib import Path

from conftest import calls, control


def op_calls(settings, op_prefix):
    return [c for c in calls(settings) if c["args"][:len(op_prefix)] == op_prefix]


def test_write_tool_refuses_destructive_operations(live):
    _, tokens = live.tokens()
    token = tokens["access_token"]
    for operation in ("delete.plugin", "reset.webapp", "restore.console_app.version", "noeffect.thing"):
        result = live.call(token, "difyctl_write", {"operation": operation, "input": {"plugin_id": "p"}})
        assert result["outcome"] == "failed" and result["error"]["code"] == "wrong_tool", operation
        assert "difyctl_destructive" in result["error"]["message"]
    assert op_calls(live.settings, ["delete", "plugin"]) == []
    assert live.call(token, "difyctl_read", {"operation": "create.console_app.workflow"})["error"]["code"] == "wrong_tool"
    # check.console_app.dsl is a POST, overridden to read.
    assert live.call(token, "difyctl_read", {"operation": "check.console_app.dsl", "input": {"yaml_content": "a: 1"}})["outcome"] == "success"
    done = live.call(token, "difyctl_destructive", {"operation": "delete plugin", "input": {"plugin_id": "p"}})
    assert done["outcome"] == "success" and done["result"]["op"] == "delete.plugin"


def test_read_scope_cannot_use_write_tools(live):
    _, tokens = live.tokens(scope="difyctl:read")
    token = tokens["access_token"]
    assert tokens["scope"] == "difyctl:read"
    for name, arguments in (("difyctl_write", {"operation": "create.console_app.workflow"}),
                            ("difyctl_put_file", {"path": "a.txt", "content": "x"}),
                            ("difyctl_use_workspace", {"workspace_id": "ws-2"}), ("difyctl_logout", {})):
        result = live.call(token, name, arguments)
        assert result["error"]["code"] == "insufficient_scope", name
    assert live.call(token, "difyctl_read", {"operation": "get.console_app"})["outcome"] == "success"


def test_file_fields_must_stay_in_the_account_workdir(live):
    _, tokens = live.tokens()
    token = tokens["access_token"]
    other = live.settings.users_dir / "acc-2" / "work"
    other.mkdir(parents=True)
    (other / "secret.txt").write_text("other account")
    work = live.settings.users_dir / "acc-1" / "work"
    work.mkdir(parents=True, exist_ok=True)
    os.symlink("/etc", work / "etc-link")
    for value in ("/etc/passwd", "../../acc-2/work/secret.txt", str(other / "secret.txt"), "etc-link/passwd", ".", ""):
        result = live.call(token, "difyctl_write", {"operation": "upload.console_app.file", "input": {"app_id": "a", "file": value}})
        assert result["outcome"] == "failed" and result["error"]["code"] in ("file_outside_workdir", "invalid_file_path"), value
    nested = {"app_id": "a", "files": {"doc": ["ok.txt", "/etc/shadow"]}, "inputs": {}}
    assert live.call(token, "difyctl_write", {"operation": "run.console_app.workflow", "input": nested})["error"]["code"] == "file_outside_workdir"
    attachments = {"app_id": "a", "attachments": ["/etc/hosts"], "inputs": {}}
    assert live.call(token, "difyctl_write", {"operation": "run.console_app.workflow", "input": attachments})["error"]["code"] == "file_outside_workdir"
    # '@path' in object/list fields would make difyctl read a local file itself.
    for value in ({"app_id": "a", "inputs": "@/etc/passwd"}, {"app_id": "a", "inputs": {}, "files": "@-"}):
        result = live.call(token, "difyctl_write", {"operation": "run.console_app.workflow", "input": value})
        assert result["error"]["code"] == "file_reference_rejected"
    assert op_calls(live.settings, ["upload"]) == [] and op_calls(live.settings, ["run"]) == []
    put = live.call(token, "difyctl_put_file", {"path": "uploads/report.txt", "content": base64.b64encode(b"hello").decode(), "encoding": "base64"})
    assert put["outcome"] == "success" and put["result"]["size"] == 5
    run = live.call(token, "difyctl_write", {"operation": "upload.console_app.file", "input": {"app_id": "a", "file": put["result"]["path"]}})
    assert run["outcome"] == "success"
    assert Path(run["result"]["cwd"]).resolve() == work.resolve()
    assert live.call(token, "difyctl_get_file", {"path": "uploads/report.txt"})["result"]["content"] == "hello"
    assert live.call(token, "difyctl_get_file", {"path": "../acc-2/work/secret.txt"})["error"]["code"] == "invalid_file_path"
    assert live.call(token, "difyctl_put_file", {"path": "uploads/report.txt", "content": "x"})["error"]["code"] == "file_exists"
    assert live.call(token, "difyctl_put_file", {"path": "etc-link/x", "content": "x"})["error"]["code"] == "invalid_file_path"


def test_include_secret_and_forbidden_operations(live):
    _, tokens = live.tokens()
    token = tokens["access_token"]
    for value in (True, "true", 1):
        result = live.call(token, "difyctl_read", {"operation": "export.console_app.dsl", "input": {"app_id": "a", "include_secret": value}})
        assert result["error"]["code"] == "forbidden_input"
    assert live.call(token, "difyctl_read", {"operation": "export.console_app.dsl", "input": {"app_id": "a", "include_secret": False}})["outcome"] == "success"
    result = live.call(token, "difyctl_destructive", {"operation": "delete.account.current_session"})
    assert result["error"]["code"] == "forbidden_operation" and "difyctl_logout" in result["error"]["message"]
    for operation in ("login", "use workspace", "install skills", "set config", "refresh.cache"):
        assert live.call(token, "difyctl_destructive", {"operation": operation})["outcome"] == "failed", operation
    assert live.call(token, "difyctl_read", {"operation": "get.console_app; rm -rf /"})["error"]["code"] == "invalid_operation"
    assert live.call(token, "difyctl_read", {"operation": "get.console_app --insecure"})["error"]["code"] == "invalid_operation"


def test_timeout_interrupts_and_returns_run_id(live):
    _, tokens = live.tokens()
    result = live.call(tokens["access_token"], "difyctl_write", {"operation": "run.console_app.workflow",
                                                                 "input": {"app_id": "app-1", "inputs": {}, "sleep": 30}, "timeout_seconds": 1})
    assert result["timed_out"] is True and result["outcome"] == "needs_attention"
    assert result["result"]["status"] == "incomplete" and result["run_id"] == "run-1"
    assert "describe.run" in result["next_step"] and "app-1" in result["next_step"]
    hard = live.call(tokens["access_token"], "difyctl_write", {"operation": "run.console_app.workflow",
                                                               "input": {"app_id": "a", "inputs": {}, "sleep": 30, "ignore_interrupt": True},
                                                               "timeout_seconds": 1})
    assert hard["timed_out"] is True and hard["outcome"] == "failed"


def test_file_downloads_get_distinct_output_paths(live):
    _, tokens = live.tokens()
    for _ in range(2):
        assert live.call(tokens["access_token"], "difyctl_read", {"operation": "download.thing"})["outcome"] == "success"
    outputs = [c["args"][c["args"].index("--output") + 1] for c in op_calls(live.settings, ["download", "thing"])]
    assert len(outputs) == 2 and outputs[0] != outputs[1]
    assert all(Path(o).parent == (live.settings.users_dir / "acc-1" / "work" / "downloads") for o in outputs)


def test_streaming_returns_task_id_for_stop_run(live):
    _, tokens = live.tokens()
    token = tokens["access_token"]
    result = live.call(token, "difyctl_write", {"operation": "run.console_app.workflow", "input": {"app_id": "a", "inputs": {}},
                                                "stream": True, "only": ["workflow_started"]})
    assert result["result"]["task_id"] == "task-1" and result["result"]["run_id"] == "run-1"
    assert [e["event"] for e in result["result"]["events"]] == ["workflow_started"]
    assert op_calls(live.settings, ["run"])[-1]["args"][-4:] == ["--json", "--stream", "--only", "workflow_started"]
    assert live.call(token, "difyctl_read", {"operation": "get.console_app", "stream": True})["error"]["code"] == "invalid_input"


def test_outputs_are_redacted_and_large_results_saved(live):
    _, tokens = live.tokens()
    token = tokens["access_token"]
    result = live.call(token, "difyctl_read", {"operation": "describe.console_app", "input": {"app_id": "a"}})
    assert result["result"]["token"] == "dfoa_[REDACTED]"
    # The child environment is an allow-list: no DIFY_TOKEN or DIFY_WORKSPACE_ID, no server secrets.
    proxies = {"HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "no_proxy", "all_proxy", "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE"}
    assert set(result["result"]["env"]) <= {"PATH", "HOME", "DIFY_CONFIG_DIR", "DIFY_OUTPUT", "LC_CTYPE", "PWD", "SHLVL", "_"} | proxies
    assert result["result"]["env"].count("DIFY_TOKEN") == 0 and "DIFYCTL_MCP_API_KEY" not in result["result"]["env"]
    big = live.call(token, "difyctl_read", {"operation": "describe.console_app", "input": {"app_id": "a", "big": 150000}})
    saved = big["result"]["output_file"]
    assert big["result"]["size_chars"] > live.settings.max_output_chars and len(big["result"]["preview"]) == 2000
    part = live.call(token, "difyctl_get_file", {"path": saved, "offset": 0, "length": 50})["result"]
    assert part["length"] == 50 and part["next_offset"] == 50 and not part["eof"]


def test_auth_expired_revokes_tokens(live):
    _, tokens = live.tokens()
    token = tokens["access_token"]
    result = live.call(token, "difyctl_read", {"operation": "get.console_app", "input": {"fail": "auth_expired"}})
    assert result["exit_code"] == 4 and result["error"]["code"] == "auth_expired" and "reconnect" in result["next_step"]
    assert result["notices"] == ["notice: something happened"]
    assert live.rpc(token, "tools/list").status_code == 401


def test_access_denied_keeps_tokens_and_other_exit_codes(live):
    _, tokens = live.tokens()
    token = tokens["access_token"]
    denied = live.call(token, "difyctl_read", {"operation": "get.console_app", "input": {"fail": "access_denied"}})
    assert denied["exit_code"] == 4 and "Tokens are unchanged" in denied["next_step"]
    assert live.rpc(token, "tools/list").status_code == 200
    limited = live.call(token, "difyctl_read", {"operation": "get.console_app", "input": {"fail": "rate_limited"}})
    assert limited["exit_code"] == 7 and limited["retry_after_seconds"] == 60
    catalog = live.call(token, "difyctl_read", {"operation": "get.console_app", "input": {"fail": "unknown_op"}})
    assert catalog["exit_code"] == 6 and "difyctl_refresh_catalog" in catalog["next_step"]
    attention = live.call(token, "difyctl_read", {"operation": "get.console_app", "input": {"status": "suspended"}})
    assert attention["outcome"] == "needs_attention"
    failed = live.call(token, "difyctl_write", {"operation": "create.console_app.workflow", "input": {"fail": "server_error"}})
    assert failed["outcome"] == "failed" and failed["result"] == {"status": "failed"}
    # write operations are never retried automatically
    assert len([c for c in op_calls(live.settings, ["create"])]) == 1


def test_help_annotates_tools_and_guide(live):
    _, tokens = live.tokens()
    token = tokens["access_token"]
    overview = live.call(token, "difyctl_help")
    assert "mcp_usage" in overview
    descriptor = live.call(token, "difyctl_help", {"query": "delete.plugin"})["result"]
    assert descriptor["mcp_tool"] == "difyctl_destructive"
    assert descriptor["mcp_examples"][0]["arguments"]["operation"] == "delete.plugin"
    search = live.call(token, "difyctl_help", {"query": "console_app"})["result"]
    tools = {e["id"]: e["mcp_tool"] for e in search["entries"]}
    assert tools["check console_app dsl"] == "difyctl_read" and tools["run console_app workflow"] == "difyctl_write"
    assert live.call(token, "difyctl_help", {"query": "--all"})["error"]["code"] == "invalid_input"
    guide = live.call(token, "difyctl_guide")["result"]
    assert guide["documents"] == ["SKILL.md", "references/operations.md"] and "OAuth" in guide["mcp_mapping"]
    assert live.call(token, "difyctl_guide", {"document": "../../etc/passwd"})["error"]["code"] == "unknown_document"


def test_workspace_refresh_and_logout(live):
    _, tokens = live.tokens()
    token = tokens["access_token"]
    assert live.call(token, "difyctl_use_workspace", {"workspace_id": "ws-2"})["result"]["workspace_id"] == "ws-2"
    assert live.call(token, "difyctl_use_workspace", {"workspace_id": "ws 2;"})["error"]["code"] == "invalid_input"
    assert live.call(token, "difyctl_refresh_catalog")["outcome"] == "success"
    config = live.server.config.app.app.state.login.store.user("acc-1")["config_dir"]
    logout = live.call(token, "difyctl_logout")
    assert logout["outcome"] == "success" and not Path(config).exists()
    assert live.rpc(token, "tools/list").status_code == 401


def test_status_warns_before_expiry_and_on_version_mismatch(live):
    control(live.settings, server_version="1.19.0")
    import time
    control(live.settings, session_expires_at=int(time.time()) + 2 * 86400)
    _, tokens = live.tokens()
    status = live.call(tokens["access_token"], "difyctl_status")
    assert status["outcome"] == "needs_attention"
    assert any("expires" in w for w in status["notices"]) and any("1.19.0" in w for w in status["notices"])


def test_official_client_discovers_tools(live):
    import asyncio
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client
    _, tokens = live.tokens()

    async def verify():
        async with httpx2.AsyncClient(headers={"Authorization": "Bearer " + tokens["access_token"]}, trust_env=False) as http:
            async with Client(streamable_http_client(live.base + "/mcp", http_client=http)) as client:
                listed = await client.list_tools()
                assert "difyctl_read" in {t.name for t in listed.tools}
                result = await client.call_tool("difyctl_read", {"operation": "get.console_app"})
                assert result.structured_content["outcome"] == "success"
    asyncio.run(verify())
