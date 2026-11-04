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


def test_text_chunks_never_split_utf8(tmp_path):
    work = files.workdir(tmp_path, "acc")
    (work / "zh.txt").write_text("你好，世界 ok", encoding="utf-8")
    for length in (1, 2, 4, 5, 7):
        offset, parts = 0, []
        while offset is not None:
            part = files.get_file(work, "zh.txt", offset, length, "text")
            assert "�" not in part["content"]
            parts.append(part["content"])
            offset = part["next_offset"]
        assert "".join(parts) == "你好，世界 ok", length


def test_put_file_never_replaces_a_concurrent_upload(tmp_path, monkeypatch):
    work = files.workdir(tmp_path, "acc")
    real_mkstemp = files.tempfile.mkstemp

    def racing_mkstemp(*args, **kwargs):
        (work / "same.txt").write_text("first upload")  # another request finished in between
        return real_mkstemp(*args, **kwargs)
    monkeypatch.setattr(files.tempfile, "mkstemp", racing_mkstemp)
    with pytest.raises(PolicyError, match="exists"):
        files.put_file(work, "same.txt", "second upload", "text", False, 1024, 60)
    assert (work / "same.txt").read_text() == "first upload"
    assert [p.name for p in work.iterdir()] == ["same.txt"]


def test_omitted_redirect_uri_is_checked_against_the_current_allowlist(tmp_path):
    settings = Settings(public_url="https://mcp.example/difyctl-mcp", allowed_emails=["a@example.com"],
                        allowed_redirect_uris=["https://new.example/cb"], api_key="k", data_dir=tmp_path)
    provider = Provider(settings, Store(settings.database))
    from mcp.shared.auth import InvalidRedirectUriError
    old = provider._client({"client_id": "c", "redirect_uris": ["https://old.example/cb"]})
    with pytest.raises(InvalidRedirectUriError):
        old.validate_redirect_uri(None)
    current = provider._client({"client_id": "c2", "redirect_uris": ["https://new.example/cb"]})
    assert str(current.validate_redirect_uri(None)) == "https://new.example/cb"


def test_calls_waiting_on_a_login_switch_use_the_new_config_dir(tmp_path):
    from types import SimpleNamespace
    from difyctl_mcp.runner import Run
    settings = Settings(public_url="https://mcp.example/difyctl-mcp", allowed_emails=["a@example.com"],
                        allowed_redirect_uris=["https://c.example/cb"], api_key="k", data_dir=tmp_path)
    store = Store(settings.database)
    store.adopt_session("acc", "a@example.com", str(tmp_path / "old"), int(time.time()) + 3600)
    used = []

    class Recorder:
        async def run(self, args, *, config_dir, cwd=None, stdin=None, timeout=90):
            used.append(str(config_dir))
            return Run(0, '{"logged_out": true}', "")
    service = Service(settings, store, Recorder())
    token = SimpleNamespace(subject="acc", client_id="client", scopes=["difyctl:destructive"])

    async def scenario():
        async with service.locks["acc"].write():
            waiting = asyncio.create_task(service.refresh_catalog(token))
            await asyncio.sleep(0.05)
            store.adopt_session("acc", "a@example.com", str(tmp_path / "new"), int(time.time()) + 3600)
        return await waiting
    assert asyncio.run(scenario())["outcome"] == "success"
    assert used == [str(tmp_path / "new")]


def test_waiting_writer_blocks_new_readers_and_cancellation_releases_them():
    from difyctl_mcp.service import RWLock

    async def scenario():
        lock, order = RWLock(), []
        release = asyncio.Event()

        async def reader(name, hold=None):
            async with lock.read():
                order.append(name)
                if hold:
                    await hold.wait()

        async def writer():
            async with lock.write():
                order.append("writer")
        first = asyncio.create_task(reader("r1", release))
        await asyncio.sleep(0.01)
        pending_writer = asyncio.create_task(writer())
        await asyncio.sleep(0.01)
        late = asyncio.create_task(reader("r2"))
        await asyncio.sleep(0.01)
        assert order == ["r1"]  # the late reader queues behind the waiting writer
        release.set()
        await asyncio.gather(first, pending_writer, late)
        assert order == ["r1", "writer", "r2"]
        # A writer cancelled while waiting must not leave readers blocked.
        release.clear()
        holder = asyncio.create_task(reader("r3", release))
        await asyncio.sleep(0.01)
        cancelled = asyncio.create_task(writer())
        await asyncio.sleep(0.01)
        cancelled.cancel()
        await asyncio.wait_for(reader("r4"), 1)
        release.set()
        await holder
        return order
    assert asyncio.run(scenario())[-2:] == ["r3", "r4"]


def test_concurrency_limits_fail_fast(tmp_path):
    from types import SimpleNamespace
    from difyctl_mcp.runner import Run, RunnerBusy
    settings = Settings(public_url="https://mcp.example/difyctl-mcp", allowed_emails=["a@example.com"],
                        allowed_redirect_uris=["https://c.example/cb"], api_key="k", data_dir=tmp_path, max_account_runs=2)
    store = Store(settings.database)
    store.adopt_session("acc", "a@example.com", str(tmp_path / "cfg"), int(time.time()) + 3600)
    gate = asyncio.Event()

    class Slow:
        async def run(self, args, **_):
            await gate.wait()
            return Run(0, "{}", "")
    service = Service(settings, store, Slow())
    token = SimpleNamespace(subject="acc", client_id="client", scopes=["difyctl:read"])

    async def scenario():
        running = [asyncio.create_task(service.refresh_catalog(token)) for _ in range(2)]
        await asyncio.sleep(0.01)
        rejected = await service.refresh_catalog(token)
        gate.set()
        done = await asyncio.gather(*running)
        return rejected, done
    rejected, done = asyncio.run(scenario())
    assert rejected["error"]["code"] == "busy" and rejected["retry_after_seconds"] == 5
    assert [d["outcome"] for d in done] == ["success", "success"] and service.running["acc"] == 0

    runner = Runner("/bin/sleep", tmp_path, max_concurrent=1)

    async def global_limit():
        first = asyncio.create_task(runner.run(["1"], config_dir=tmp_path))
        await asyncio.sleep(0.05)
        with pytest.raises(RunnerBusy):
            await runner.run(["0"], config_dir=tmp_path)
        approved = await runner.run(["0"], config_dir=tmp_path, pool="approved")
        await first
        return approved
    assert asyncio.run(global_limit()).exit_code == 0 and runner.active == 0

    # Login steps have their own global pool: tool load cannot block them, nor they the tools.
    logins = Runner("/bin/sleep", tmp_path, max_concurrent=1, max_login=1)

    async def login_limit():
        first = asyncio.create_task(logins.run(["1"], config_dir=tmp_path, pool="login"))
        await asyncio.sleep(0.05)
        with pytest.raises(RunnerBusy):
            await logins.run(["0"], config_dir=tmp_path, pool="login")
        tool = await logins.run(["0"], config_dir=tmp_path)
        await first
        return tool
    assert asyncio.run(login_limit()).exit_code == 0 and logins.login_active == 0


def test_cleanup_during_completion_never_adopts_a_removed_login(tmp_path):
    import json as _json
    from difyctl_mcp.runner import Run
    settings = Settings(public_url="https://mcp.example/difyctl-mcp", allowed_emails=["a@example.com"],
                        allowed_redirect_uris=["https://c.example/cb"], api_key="k", data_dir=tmp_path)
    store = Store(settings.database)
    holder = {}

    class Runner_:
        async def run(self, args, *, config_dir, **_):
            if args[:3] == ["describe", "account", "--input"]:
                return Run(0, _json.dumps({"id": "acc", "email": "a@example.com"}), "")
            if args[:3] == ["get", "account", "session"]:
                # The pending login expires and maintenance runs while Dify is being queried.
                store._run("UPDATE pending SET expires_at = ?", (int(time.time()) - 3600,))
                holder["login"].cleanup()
                return Run(0, "{}", "")
            return Run(0, "{}", "")
    login = Login(Service(settings, store, Runner_()), Provider(settings, store))
    holder["login"] = login
    login.ensure_dirs()
    previous, fresh = settings.sessions_dir / "previous", settings.sessions_dir / "fresh"
    previous.mkdir()
    fresh.mkdir()
    store.adopt_session("acc", "a@example.com", str(previous), int(time.time()) + 3600)
    store.create_pending("q" * 32, "client", {"redirect_uri": "https://c.example/cb", "state": None, "scopes": ["difyctl:read"],
                                              "code_challenge": "x", "explicit": True, "resource": settings.resource}, "cookie", ttl=900)
    store.update_pending("q" * 32, status="completing", config_dir=str(fresh))
    response = asyncio.run(login.complete(store.pending("q" * 32), fresh, {"email": "a@example.com"}))
    assert response.status_code == 404
    assert store.user("acc")["config_dir"] == str(previous) and previous.is_dir() and not fresh.exists()
    assert store._one("SELECT COUNT(*) FROM auth_codes")[0] == 0


def test_cleanup_gives_a_completing_login_a_grace_period(tmp_path):
    settings = Settings(public_url="https://mcp.example/difyctl-mcp", allowed_emails=["a@example.com"],
                        allowed_redirect_uris=["https://c.example/cb"], api_key="k", data_dir=tmp_path)
    store = Store(settings.database)
    login = Login(Service(settings, store, Runner("/bin/true", tmp_path)), Provider(settings, store))
    login.ensure_dirs()
    folder = settings.sessions_dir / "busy"
    folder.mkdir()
    store.create_pending("c" * 32, "client", {}, "cookie", ttl=-10)
    store.update_pending("c" * 32, status="completing", config_dir=str(folder))
    login.cleanup()
    assert folder.is_dir() and store.pending("c" * 32) is not None
    store._run("UPDATE pending SET expires_at = ?", (int(time.time()) - 3600,))
    login.cleanup()
    assert not folder.exists() and store.pending("c" * 32) is None


def test_storage_quotas_are_enforced_before_writing(tmp_path):
    users = tmp_path / "users"
    D = files.DIRECTORY_COST  # users/acc, users/acc/work, users/other, users/other/work count as directories
    quota = files.Quota(users, account_bytes=100, account_files=3, total_bytes=150 + 4 * D)
    work = files.workdir(users, "acc")
    files.put_file(work, "a.txt", "x" * 60, "text", False, 1024, 60, quota)
    with pytest.raises(PolicyError, match="limited to"):
        files.put_file(work, "b.txt", "x" * 41, "text", False, 1024, 60, quota)
    assert not (work / "b.txt").exists()
    # Overwriting counts the replaced file as freed.
    files.put_file(work, "a.txt", "y" * 90, "text", True, 1024, 60, quota)
    files.put_file(work, "c.txt", "z", "text", False, 1024, 60, quota)
    files.put_file(work, "d.txt", "z", "text", False, 1024, 60, quota)
    with pytest.raises(PolicyError, match="3 files"):
        files.put_file(work, "e.txt", "z", "text", False, 1024, 60, quota)
    other = files.workdir(users, "other")
    files.put_file(other, "big.txt", "w" * 50, "text", False, 1024, 60, quota)
    with pytest.raises(PolicyError, match="server's work storage is full"):
        files.put_file(other, "more.txt", "w" * 10, "text", False, 1024, 60, quota)
    with pytest.raises(PolicyError, match="limited to"):
        files.save_output(work, "op", "x" * 20, quota)
    assert not (work / "outputs").exists() or not any((work / "outputs").iterdir())


def test_concurrent_uploads_cannot_overshoot_the_quota(tmp_path):
    import threading as _threading
    users = tmp_path / "users"
    quota = files.Quota(users, account_bytes=100, account_files=100, total_bytes=10_000)
    work = files.workdir(users, "acc")
    barrier, outcomes = _threading.Barrier(8), []

    def upload(i):
        barrier.wait()
        try:
            files.put_file(work, f"f{i}.txt", "x" * 30, "text", False, 1024, 60, quota)
            outcomes.append("ok")
        except PolicyError:
            outcomes.append("rejected")
    threads = [_threading.Thread(target=upload, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert outcomes.count("ok") == 3 and files.usage(work)[0] == 90


def test_refresh_history_is_bounded_but_recent_reuse_is_detected(tmp_path):
    from difyctl_mcp.store import RETIRED_PER_FAMILY
    store = Store(tmp_path / "auth.db")
    tokens = ["r0"]
    store.save_tokens("a0", "r0", "client", "acc", ["difyctl:read"], "res", int(time.time()) + 60, int(time.time()) + 600)
    for i in range(1, 50):
        family = store.rotate_refresh(tokens[-1])
        assert family is not None
        tokens.append(f"r{i}")
        store.save_tokens(f"a{i}", f"r{i}", "client", "acc", ["difyctl:read"], "res", int(time.time()) + 60,
                          int(time.time()) + 600, family)
    assert store._one("SELECT COUNT(*) FROM refresh_tokens")[0] == RETIRED_PER_FAMILY + 1
    assert store._one("SELECT COUNT(*) FROM access_tokens")[0] == 1
    assert store.refresh_token(tokens[-2]).revoked_at is not None  # a recently retired token is still recognized
    assert store.refresh_token(tokens[0]) is None and store.rotate_refresh(tokens[-2]) is None


def test_large_mcp_requests_are_bounded_before_the_body_is_read(tmp_path):
    from types import SimpleNamespace
    from difyctl_mcp.app import LargeRequestLimit
    settings = Settings(max_concurrent_uploads=2, max_account_uploads=1)
    gate = asyncio.Event()
    read = []

    class Inner:
        state = SimpleNamespace()

        async def __call__(self, scope, receive, send):
            read.append(scope["headers"])
            await gate.wait()

    class Provider_:
        async def load_access_token(self, token):
            return SimpleNamespace(subject=token.split("-")[0], client_id="c")

    limiter = LargeRequestLimit(Inner(), Provider_(), settings)
    responses = []

    def request(token, length=b"5000000"):
        scope = {"type": "http", "path": "/mcp", "method": "POST",
                 "headers": [(b"authorization", b"Bearer " + token.encode()), (b"content-length", length)]}

        async def receive():
            raise AssertionError("a refused request must not be read")

        async def send(message):
            if message["type"] == "http.response.start":
                responses.append(message["status"])
        return limiter(scope, receive, send)

    async def scenario():
        first = asyncio.create_task(request("alice-1"))
        await asyncio.sleep(0.01)
        await request("alice-2")                  # same account: refused
        second = asyncio.create_task(request("bob-1"))
        await asyncio.sleep(0.01)
        await request("carol-1")                  # global limit of 2 reached: refused
        small = asyncio.create_task(request("alice-3", b"100"))  # small requests are not limited
        await asyncio.sleep(0.01)
        gate.set()
        await asyncio.gather(first, second, small)
    asyncio.run(scenario())
    assert responses == [503, 503] and len(read) == 3 and limiter.active == 0 and not limiter.per_account


def test_directories_count_against_the_quota_and_depth_is_capped(tmp_path):
    users = tmp_path / "users"
    quota = files.Quota(users, account_bytes=3 * files.DIRECTORY_COST, account_files=1000, total_bytes=10 ** 9)
    work = files.workdir(users, "acc")
    files.put_file(work, "a/b/x.txt", "1", "text", False, 1024, 60, quota)   # two directories, one file
    assert files.usage(work) == (2 * files.DIRECTORY_COST + 1, 3)
    with pytest.raises(PolicyError, match="limited to"):
        files.put_file(work, "c/d/e/y.txt", "1", "text", False, 1024, 60, quota)
    with pytest.raises(PolicyError, match="at most 8 segments"):
        files.relative_path("/".join(["d"] * 9))
    files.relative_path("/".join(["d"] * 8))


def test_client_registrations_are_bounded_and_reclaimed(tmp_path):
    store = Store(tmp_path / "auth.db")
    for i in range(3):
        assert store.save_client(f"c{i}", None, {"redirect_uris": []}, max_clients=3)
    store.mark_client_used("c1")
    # At capacity, the oldest never-used registration (c0) makes room; used ones are kept.
    assert store.save_client("c3", None, {"redirect_uris": []}, max_clients=3)
    assert store.client("c0") is None and store.client("c1") is not None
    store.mark_client_used("c2")
    store.mark_client_used("c3")
    assert not store.save_client("c4", None, {"redirect_uris": []}, max_clients=3)  # every client is in use
    store.save_client("idle", None, {"redirect_uris": []})
    store._run("UPDATE clients SET created_at = ? WHERE client_id = 'idle'", (int(time.time()) - 2 * 86400,))
    store.purge()
    assert store.client("idle") is None and store.client("c1") is not None


def test_login_steps_fail_soft_when_the_login_pool_is_full(tmp_path):
    from difyctl_mcp.runner import RunnerBusy
    settings = Settings(public_url="https://mcp.example/difyctl-mcp", allowed_emails=["a@example.com"],
                        allowed_redirect_uris=["https://c.example/cb"], api_key="k", data_dir=tmp_path)
    store = Store(settings.database)

    class Busy:
        async def run(self, args, **kwargs):
            assert kwargs.get("pool") == "login"
            raise RunnerBusy()
    login = Login(Service(settings, store, Busy()), Provider(settings, store))
    login.ensure_dirs()
    store.create_pending("w" * 32, "client", {}, "cookie", ttl=900)
    store.update_pending("w" * 32, status="waiting", config_dir=str(settings.sessions_dir / "x"))
    assert asyncio.run(login.check(store.pending("w" * 32))) is None
    assert store.pending("w" * 32)["status"] == "waiting"
