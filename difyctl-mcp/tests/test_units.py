import asyncio
import os
import time
from pathlib import Path

import pytest

from difyctl_mcp import files
from difyctl_mcp.login import Login, parse_time, session_expiry
from difyctl_mcp.oauth import Provider, redirect_allowed
from difyctl_mcp.policy import PolicyError, check_files, check_input, classify, tool_for, words
from difyctl_mcp.runner import Runner, redact, split_stderr
from difyctl_mcp.service import Service, needs_attention, stream_summary
from difyctl_mcp.settings import Settings
from difyctl_mcp.store import Store


@pytest.mark.parametrize("descriptor,expected", [
    ({"op": "get.console_app", "effect": "read"}, 0),
    ({"op": "create.console_app.workflow", "effect": "write"}, 1),
    ({"op": "check.console_app.dsl", "effect": "write"}, 0),
    ({"op": "knowledge_fs.diff", "effect": "write"}, 0),
    ({"op": "reset.webapp.agent", "effect": "write"}, 2),
    ({"op": "restore.console_app.version", "effect": "write"}, 2),
    ({"op": "remove.something", "effect": "read"}, 2),
    ({"op": "revoke.something", "effect": "write"}, 2),
    ({"op": "delete.plugin", "effect": "read"}, 2),
    ({"op": "mystery.op"}, 2),
    ({"op": "mystery.op", "effect": "unknown"}, 2),
])
def test_classification(descriptor, expected):
    assert classify(descriptor)[1] == expected


def test_only_catalog_descriptors_are_operations():
    for descriptor in ({"id": "logout", "effect": "write"}, {"entries": []}, {"op": "Bad Op"}, None):
        with pytest.raises(PolicyError):
            classify(descriptor)
    with pytest.raises(PolicyError, match="difyctl_logout"):
        classify({"op": "delete.account.current_session", "effect": "destructive"})


def test_operation_names():
    assert words("run.console_app.workflow") == ["run", "console_app", "workflow"]
    assert words("run console_app workflow") == ["run", "console_app", "workflow"]
    for bad in ("", "Run.x", "run..x", "run.x ", "-x", "run.x --insecure", "a/b", "run.x\n"):
        with pytest.raises(PolicyError):
            words(bad)


def test_tool_mapping_for_help_entries():
    assert tool_for("use workspace", "write") == "difyctl_use_workspace"
    assert tool_for("login", "write").startswith("Not available")
    assert tool_for("get console_app", "read") == "difyctl_read"
    assert tool_for("reset webapp", "write") == "difyctl_destructive"


def test_at_references_only_allowed_for_scalar_fields():
    descriptor = {"input": {"properties": {"name": {"type": "string"}, "inputs": {"type": "object"},
                                           "nullable": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                                           "union": {"anyOf": [{"type": "string"}, {"type": "object"}]}}}}
    check_input("x.y", descriptor, {"name": "@literal", "nullable": "@also literal"})
    for data in ({"inputs": "@/etc/passwd"}, {"union": "@-"}, {"unknown": "@file"}):
        with pytest.raises(PolicyError):
            check_input("x.y", descriptor, data)
    with pytest.raises(PolicyError):
        check_input("x.y", descriptor, ["not", "object"])


def test_file_checks(tmp_path):
    work = tmp_path / "work"
    (work / "folder").mkdir(parents=True)
    (work / "a.txt").write_text("a")
    (work / "folder" / "b.txt").write_text("b")
    descriptor = {"bind": {"file": "file", "files": "file"}}
    check_files(descriptor, {"file": "a.txt", "files": {"doc": ["a.txt", "folder/b.txt"]}}, work)
    check_files(descriptor, {"file": "folder"}, work)
    os.symlink("/etc/hostname", work / "folder" / "link")
    with pytest.raises(PolicyError, match="symlinks"):
        check_files(descriptor, {"file": "folder"}, work)
    for data in ({"file": "/etc/passwd"}, {"file": "../x"}, {"files": {"doc": "/etc/passwd"}}, {"files": [{"nested": 1}]},
                 {"file": 7}, {"files": {"doc": "folder"}}):
        with pytest.raises(PolicyError):
            check_files(descriptor, data, work)


def test_relative_paths():
    for bad in ("/abs", "../x", "a/../b", ".hidden", "a//b", "", "a\0b"):
        with pytest.raises(PolicyError):
            files.relative_path(bad)
    assert files.relative_path("uploads/report.pdf") == Path("uploads/report.pdf")


def test_redirect_allowlist():
    allowed = ["https://claude.ai/api/mcp/auth_callback", "http://localhost/callback"]
    assert redirect_allowed("https://claude.ai/api/mcp/auth_callback", allowed)
    assert redirect_allowed("http://localhost:33418/callback", allowed)
    for uri in ("https://claude.ai/api/mcp/auth_callback/x", "http://localhost:33418/other", "https://localhost:1/callback",
                "http://evil.example/callback", "http://localhost.evil.example/callback", "http://localhost:1/callback#x"):
        assert not redirect_allowed(uri, allowed), uri


def test_redaction_and_stderr_split():
    assert redact("token dfoa_abc.DEF-123/x= end") == "token dfoa_[REDACTED] end"
    notices, error = split_stderr('open https://x/device\ncode AB\n{\n  "error": {"code": "auth_expired", "message": "m"}\n}')
    assert notices == ["open https://x/device", "code AB"] and error["code"] == "auth_expired"
    assert split_stderr("only a notice") == (["only a notice"], None)


def test_runner_environment_is_allow_listed(tmp_path):
    runner = Runner("/bin/true", tmp_path, environ={"PATH": "/bin", "DIFY_TOKEN": "x", "DIFY_WORKSPACE_ID": "w",
                                                    "HTTPS_PROXY": "http://proxy:3128", "DIFYCTL_MCP_API_KEY": "secret"})
    assert runner.environment(tmp_path / "cfg") == {"PATH": "/bin", "HOME": str(tmp_path), "DIFY_CONFIG_DIR": str(tmp_path / "cfg"),
                                                    "DIFY_OUTPUT": "json", "HTTPS_PROXY": "http://proxy:3128"}


def test_runner_kills_after_interrupt_grace(tmp_path, monkeypatch):
    import difyctl_mcp.runner as runner_module
    monkeypatch.setattr(runner_module, "INTERRUPT_GRACE_SECONDS", 0.5)
    script = tmp_path / "stubborn.sh"
    script.write_text("#!/bin/sh\ntrap '' INT\nsleep 30\n")
    script.chmod(0o755)
    started = time.monotonic()
    run = asyncio.run(Runner(str(script), tmp_path).run([], config_dir=tmp_path, timeout=0.5))
    assert run.timed_out and run.exit_code == -9 and time.monotonic() - started < 10


def test_results_needing_attention():
    assert needs_attention({"status": "suspended"}) and needs_attention({"valid": False})
    assert needs_attention({"ready": False}) and needs_attention({"leaked_dependencies": ["x"]})
    assert not needs_attention({"status": "ended", "valid": True, "leaked_dependencies": []})
    summary = stream_summary([{"event": "workflow_started", "task_id": "t", "workflow_run_id": "r"},
                              {"event": "workflow_finished", "data": {"status": "stopped"}}])
    assert (summary["task_id"], summary["run_id"], summary["status"]) == ("t", "r", "failed")


def test_session_expiry_matching():
    now = 1_800_000_000
    listing = {"data": [{"device_label": "difyctl-mcp", "expires_at": now + 100},
                        {"device_label": "difyctl-mcp", "expires_at": "2027-01-15T09:00:00Z"},
                        {"device_label": "laptop", "expires_at": now + 999_999}]}
    assert session_expiry(listing, "difyctl-mcp", now, 14 * 86400) == parse_time("2027-01-15T09:00:00Z") == now + 3600
    assert session_expiry([{"current": True, "expires_at": (now + 50) * 1000}], "x", now, 10) == now + 50
    assert session_expiry({"data": [{"device_label": "other", "expires_at": now + 5}]}, "difyctl-mcp", now, 10) == now + 10
    assert session_expiry({"data": [{"device_label": "difyctl-mcp", "expires_at": now - 5}]}, "difyctl-mcp", now, 10) == now + 10
    assert session_expiry(None, "x", now, 10) == now + 10


def test_cleanup_removes_expired_pending_and_orphans_but_keeps_adopted(tmp_path):
    settings = Settings(public_url="https://mcp.example/difyctl-mcp", allowed_emails=["a@example.com"],
                        allowed_redirect_uris=["https://c.example/cb"], api_key="k", data_dir=tmp_path, difyctl_bin="/bin/true")
    store = Store(settings.database)
    login = Login(Service(settings, store, Runner("/bin/true", tmp_path)), Provider(settings, store))
    login.ensure_dirs()
    adopted, expired, orphan, fresh = (settings.sessions_dir / name for name in ("adopted", "expired", "orphan", "fresh"))
    for path in (adopted, expired, orphan, fresh):
        path.mkdir()
    old = time.time() - 7200
    os.utime(orphan, (old, old))
    os.utime(adopted, (old, old))
    store.adopt_session("acc", "a@example.com", str(adopted), int(time.time()) + 100)
    store.create_pending("p" * 32, "client", {}, "cookie", ttl=-10)
    store.update_pending("p" * 32, config_dir=str(expired))
    login.cleanup()
    assert adopted.exists() and fresh.exists() and not expired.exists() and not orphan.exists()
    assert store.pending("p" * 32) is None


def test_file_retention(tmp_path):
    work = files.workdir(tmp_path, "acc")
    (work / "old.txt").write_text("x")
    (work / "new.txt").write_text("y")
    os.utime(work / "old.txt", (time.time() - 90000, time.time() - 90000))
    assert files.expire_files(tmp_path, 86400) == 1
    assert sorted(p.name for p in work.iterdir()) == ["new.txt"]
    assert files.account_directory(tmp_path, "../../etc").name.startswith("h-")


def test_settings_require_secure_public_url():
    with pytest.raises(ValueError):
        Settings.from_env({"MCP_PUBLIC_URL": "http://mcp.example/x"})
    with pytest.raises(ValueError):
        Settings.from_env({"DIFYCTL_MAX_EFFECT": "admin"})
    settings = Settings.from_env({"MCP_PUBLIC_URL": "https://mcp.example/difyctl-mcp/", "MCP_ALLOWED_EMAILS": "A@x.com, b@x.com",
                                  "MCP_ALLOWED_REDIRECT_URIS": "https://c.example/cb", "DIFYCTL_MCP_API_KEY": "k",
                                  "DIFYCTL_MAX_EFFECT": "write"})
    assert settings.configured and settings.allowed_emails == ["a@x.com", "b@x.com"]
    assert settings.resource == "https://mcp.example/difyctl-mcp/mcp" and settings.base_path == "/difyctl-mcp"
    assert settings.scopes == ["difyctl:read", "difyctl:write"]
    assert not Settings.from_env({}).configured
