"""Account-scoped difyctl execution: authorization checks, locking, classification and results."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from .files import remove_tree, save_output, workdir
from .policy import (TOOL_FOR_LEVEL, PolicyError, check_files, check_input, classify, effect_name, tool_for, words)
from .runner import Run, Runner, RunnerBusy, redact_value
from .settings import LEVELS, SCOPES, Settings
from .store import Store

audit = logging.getLogger("difyctl_mcp.audit")
SCOPE_LEVELS = {scope: LEVELS[name] for name, scope in SCOPES.items()}
ATTENTION_STATUSES = {"suspended", "incomplete", "pending", "failed"}
EVENT_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
HELP_WORD = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}")
WORKSPACE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,63}")
SESSION_ENDED = {"auth_expired", "not_logged_in"}
PREVIEW_CHARS = 2000
WARN_BEFORE_SECONDS = 3 * 24 * 3600


class RWLock:
    """Many concurrent difyctl calls, or one exclusive login switch / logout / workspace change.

    Writer-preferring: once a writer waits, new readers queue behind it, so a stream of calls
    cannot keep a logout or re-authorization from ever running.
    """

    def __init__(self):
        self._readers = 0
        self._writer = False
        self._writers_waiting = 0
        self._condition = asyncio.Condition()

    @asynccontextmanager
    async def read(self):
        async with self._condition:
            await self._condition.wait_for(lambda: not self._writer and self._writers_waiting == 0)
            self._readers += 1
        try:
            yield
        finally:
            async with self._condition:
                self._readers -= 1
                self._condition.notify_all()

    @asynccontextmanager
    async def write(self):
        async with self._condition:
            self._writers_waiting += 1
            try:
                await self._condition.wait_for(lambda: not self._writer and self._readers == 0)
            finally:
                # Also on cancellation: readers queued behind this writer must be released.
                self._writers_waiting -= 1
                self._condition.notify_all()
            self._writer = True
        try:
            yield
        finally:
            async with self._condition:
                self._writer = False
                self._condition.notify_all()


@dataclass
class Account:
    account_id: str
    email: str
    client_id: str
    config_dir: Path
    workdir: Path
    session_expires_at: int
    level: int


class Failure(Exception):
    """A structured tool error that never includes input values or credentials."""

    def __init__(self, code: str, message: str, **extra):
        super().__init__(message)
        self.payload = {"outcome": "failed", "exit_code": None, "result": None,
                        "error": {"code": code, "message": message}, "next_step": extra.pop("next_step", None),
                        "notices": [], "timed_out": False, **extra}


def token_level(scopes) -> int:
    levels = [SCOPE_LEVELS[s] for s in scopes or [] if s in SCOPE_LEVELS]
    return max(levels) if levels else -1


def needs_attention(result) -> bool:
    if not isinstance(result, dict):
        return False
    return (result.get("status") in ATTENTION_STATUSES or result.get("valid") is False or
            result.get("ready") is False or bool(result.get("leaked_dependencies")))


def has_hints(result, error) -> bool:
    server = (error or {}).get("server") if isinstance(error, dict) else None
    return bool(isinstance(result, dict) and result.get("hints")) or bool(isinstance(server, dict) and server.get("hints"))


def stream_summary(events: list) -> dict:
    summary = {"status": "incomplete", "run_id": None, "task_id": None, "events": events}
    order = ["incomplete", "ended", "suspended", "failed"]

    def promote(status):
        if order.index(status) > order.index(summary["status"]):
            summary["status"] = status
    for event in events:
        if not isinstance(event, dict):
            continue
        for key, target in (("workflow_run_id", "run_id"), ("task_id", "task_id")):
            if isinstance(event.get(key), str) and not summary[target]:
                summary[target] = event[key]
        name = event.get("event")
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        if name == "error":
            promote("failed")
        elif name == "human_input_required":
            promote("suspended")
        elif name in ("workflow_finished", "message_end"):
            promote("failed" if data.get("status") in ("failed", "stopped") else "ended")
    return summary


class Service:
    def __init__(self, settings: Settings, store: Store, runner: Runner):
        self.settings, self.store, self.runner = settings, store, runner
        self.locks: dict[str, RWLock] = defaultdict(RWLock)
        self.running: dict[str, int] = defaultdict(int)
        self.descriptors: dict[tuple[str, tuple[str, ...]], tuple[float, dict]] = {}

    # Identity ------------------------------------------------------------------
    def account(self, token, required_level: int) -> Account:
        if token is None or not token.subject:
            raise Failure("oauth_required", "This tool needs an OAuth access token issued after a Dify login.")
        level = min(token_level(token.scopes), self.settings.max_level)
        if required_level > level:
            raise Failure("insufficient_scope", f"Token scope does not allow {effect_name(required_level)} tools.",
                          next_step="Reconnect and approve the " + SCOPES[effect_name(required_level)] + " scope.")
        user = self.store.user(token.subject)
        if not user or not user["config_dir"]:
            raise Failure("not_logged_in", "No Dify login is stored for this account.",
                          next_step="Reconnect this MCP server to authorize again.")
        return Account(token.subject, user["email"], token.client_id, Path(user["config_dir"]),
                       workdir(self.settings.users_dir, token.subject), user["session_expires_at"], level)

    def end_session(self, account: Account) -> None:
        """Dify rejected the stored login: revoke all MCP tokens so the next request gets 401."""
        self.store.revoke_account(account.account_id)
        self.clear_descriptors(account.account_id)

    def clear_descriptors(self, account_id: str) -> None:
        for key in [k for k in self.descriptors if k[0] == account_id]:
            self.descriptors.pop(key, None)

    # Running -------------------------------------------------------------------
    async def _run(self, account: Account, args: list[str], *, stdin: bytes | None = None,
                   timeout: float | None = None) -> Run:
        # Fail fast instead of queueing: each run holds a process and up to 64 MiB of captured output.
        if self.running[account.account_id] >= self.settings.max_account_runs:
            raise Failure("busy", f"This account already has {self.settings.max_account_runs} difyctl calls running.",
                          retry_after_seconds=5, next_step="Wait for running calls to finish, then retry.")
        self.running[account.account_id] += 1
        try:
            return await self.runner.run(args, config_dir=account.config_dir, cwd=account.workdir, stdin=stdin,
                                         timeout=timeout or self.settings.timeout_seconds)
        except RunnerBusy:
            raise Failure("busy", "The server is at its difyctl concurrency limit.", retry_after_seconds=5,
                          next_step="Retry in a few seconds.") from None
        finally:
            self.running[account.account_id] -= 1

    async def descriptor(self, account: Account, op_words: list[str]) -> dict:
        key = (account.account_id, tuple(op_words))
        cached = self.descriptors.get(key)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        run = await self._run(account, ["help", *op_words, "--json"], timeout=60)
        data = run.json()
        if run.exit_code != 0 or not isinstance(data, dict):
            raise Failure("help_failed", "Could not read the operation descriptor.",
                          **self.shape(account, "help", run, data))
        if "op" not in data:
            candidates = [e.get("id") for e in data.get("entries", []) if isinstance(e, dict)][:20]
            raise Failure("unknown_operation", "No single catalog operation matches; check difyctl_help.",
                          result={"candidates": candidates})
        self.descriptors[key] = (time.monotonic() + self.settings.descriptor_ttl_seconds, data)
        return data

    async def execute(self, token, tool_level: int, operation: str, data, *, timeout_seconds=None,
                      stream: bool = False, only=None, tool: str = "") -> dict:
        started = time.monotonic()
        account = None
        op = operation
        result = None
        try:
            account = self.account(token, tool_level)
            try:
                op_words = words(operation)
                data = {} if data is None else data
                if only is not None and (not isinstance(only, list) or len(only) > 20 or
                                         any(not isinstance(o, str) or not EVENT_NAME.fullmatch(o) for o in only)):
                    raise PolicyError("invalid_input", "only must be a list of event names such as workflow_started.")
                timeout = self.settings.timeout_seconds if timeout_seconds is None else timeout_seconds
                if type(timeout) is not int or not 1 <= timeout <= self.settings.max_timeout_seconds:
                    raise PolicyError("invalid_input", f"timeout_seconds must be 1-{self.settings.max_timeout_seconds}.")
                async with self.locks[account.account_id].read():
                    # A login switch may have replaced the config directory while this call waited.
                    account = self.account(token, tool_level)
                    descriptor = await self.descriptor(account, op_words)
                    op, level = classify(descriptor)
                    if level > self.settings.max_level:
                        raise PolicyError("forbidden_operation", f"{op} is {effect_name(level)}; this server allows at most {self.settings.max_effect}.")
                    if level > tool_level:
                        raise PolicyError("wrong_tool", f"{op} is a {effect_name(level)} operation; use {TOOL_FOR_LEVEL[level]}.",
                                          next_step=f"Call {TOOL_FOR_LEVEL[level]} after confirming with the user.")
                    if level > account.level:
                        raise PolicyError("insufficient_scope", f"{op} needs the {SCOPES[effect_name(level)]} scope.")
                    payload = check_input(op, descriptor, data)
                    check_files(descriptor, data, account.workdir)
                    args = [*op.split("."), "--input", "@-", "--json"]
                    kind = descriptor.get("kind")
                    if stream or only:
                        if kind != "sse":
                            raise PolicyError("invalid_input", "stream and only apply to streaming (sse) operations.")
                        args.append("--stream")
                        for name in only or []:
                            args += ["--only", name]
                    if kind == "file":
                        (account.workdir / "downloads").mkdir(exist_ok=True, mode=0o700)
                        # A unique name per call: concurrent downloads of the same op must not share a file.
                        name = f"{op}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{secrets.token_hex(6)}"
                        args += ["--output", str(account.workdir / "downloads" / name)]
                    run = await self._run(account, args, stdin=payload, timeout=timeout)
            except PolicyError as exc:
                raise Failure(exc.code, exc.message, **exc.extra) from None
            result = self.shape(account, op, run, None, stream=bool(stream or only), data=data)
            return result
        except Failure as failure:
            result = failure.payload
            return result
        finally:
            self.audit(tool, account, token, op, data, result, started)

    def shape(self, account: Account, op: str, run: Run, parsed, *, stream: bool = False, data=None) -> dict:
        """Turn a difyctl run into outcome/result/error/next_step without leaking credentials."""
        result = (stream_summary(run.events()) if stream else (parsed if parsed is not None else run.json()))
        result = redact_value(result)
        error = redact_value(run.error)
        code = error.get("code") if isinstance(error, dict) else None
        out = {"outcome": "success", "exit_code": run.exit_code, "result": result, "error": error,
               "next_step": None, "notices": run.notices[-20:], "timed_out": run.timed_out}
        if run.truncated:
            out["notices"].append("Output exceeded the capture limit and was truncated.")
        if run.exit_code != 0 and not run.timed_out:
            out["outcome"] = "failed"
        elif needs_attention(result) or run.timed_out:
            out["outcome"] = "needs_attention"
        if run.timed_out:
            run_id = result.get("run_id") if isinstance(result, dict) else None
            if not result:
                out["outcome"] = "failed"
            out["run_id"] = run_id
            app_id = (data or {}).get("app_id") if isinstance(data, dict) else None
            out["next_step"] = (f"The run may still be going on Dify. Check it with difyctl_read operation describe.run "
                                f"and input {json.dumps({'app_id': app_id, 'run_id': run_id})}." if run_id else
                                "The call timed out; nothing was retried. Raise timeout_seconds or check the result in Dify.")
        elif run.exit_code == 4 and code in SESSION_ENDED:
            self.end_session(account)
            out["next_step"] = "The Dify login has ended. All MCP tokens for this account were revoked; reconnect to authorize again."
        elif run.exit_code == 4 and code == "access_denied":
            out["next_step"] = "Dify denied this request for this account/workspace; check the role or the error's server code. Tokens are unchanged."
        elif run.exit_code == 6:
            out["next_step"] = "The catalog is unavailable or changed: call difyctl_refresh_catalog, then retry."
        elif run.exit_code == 7:
            out["retry_after_seconds"] = 60
            out["next_step"] = "Rate limited by Dify. Wait 60 seconds before trying again; nothing was retried automatically."
        elif run.exit_code == 2:
            out["next_step"] = "difyctl rejected the input locally; read the descriptor with difyctl_help and fix input."
        elif has_hints(result, error):
            out["next_step"] = "Follow hints: pass each hint's op as operation and its input as input to the matching tool."
        text = json.dumps(out["result"], ensure_ascii=False)
        if len(text) > self.settings.max_output_chars:
            path = save_output(account.workdir, op, text)
            out["result"] = {"output_file": path, "size_chars": len(text), "preview": text[:PREVIEW_CHARS]}
            out["notices"].append(f"Result saved to {path}; read it in parts with difyctl_get_file.")
        return out

    def audit(self, tool, account, token, op, data, result, started) -> None:
        app_id = data.get("app_id") if isinstance(data, dict) and isinstance(data.get("app_id"), str) else None
        audit.info(json.dumps({
            "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "account": account.account_id if account else (getattr(token, "subject", None)),
            "client": getattr(token, "client_id", None), "tool": tool, "operation": op, "app_id": app_id,
            "exit_code": (result or {}).get("exit_code"), "outcome": (result or {}).get("outcome"),
            "error": ((result or {}).get("error") or {}).get("code") if isinstance((result or {}).get("error"), dict) else None,
            "duration_ms": int((time.monotonic() - started) * 1000)}))

    # Other tools -------------------------------------------------------------------
    async def simple(self, token, level: int, tool: str, args: list[str], *, exclusive: bool = False,
                     after=None) -> dict:
        started = time.monotonic()
        account, result = None, None
        try:
            account = self.account(token, level)
            lock = self.locks[account.account_id]
            async with (lock.write() if exclusive else lock.read()):
                account = self.account(token, level)
                run = await self._run(account, args)
                result = self.shape(account, " ".join(args[:2]), run, None)
                if after and run.exit_code == 0:
                    after(account, result)
            return result
        except Failure as failure:
            result = failure.payload
            return result
        finally:
            self.audit(tool, account, token, " ".join(args[:2]), None, result, started)

    async def help(self, token, query: str | None) -> dict:
        terms = (query or "").split()
        if len(terms) > 12 or any(not HELP_WORD.fullmatch(t) for t in terms):
            return Failure("invalid_input", "query must be up to 12 plain words or one operation id.").payload
        result = await self.simple(token, LEVELS["read"], "difyctl_help", ["help", *terms, "--json"])
        if result.get("exit_code") == 0:
            result["result"] = annotate(result["result"])
            if not terms:
                result["mcp_usage"] = MCP_USAGE
        return result

    async def status(self, token) -> dict:
        try:
            account = self.account(token, LEVELS["read"])
        except Failure as failure:
            return failure.payload
        async with self.locks[account.account_id].read():
            try:
                account = self.account(token, LEVELS["read"])
            except Failure as failure:
                return failure.payload
            try:
                version = await self._run(account, ["version", "--json"])
                login = await self._run(account, ["login", "--resume", "--json"])
            except Failure as failure:
                return failure.payload
        login_view = self.shape(account, "login resume", login, None)
        if login_view["outcome"] == "failed":
            return login_view
        versions = version.json() or {}
        client = (versions.get("client") or {}).get("version")
        server = (versions.get("server") or {}).get("version") if isinstance(versions.get("server"), dict) else None
        saved = login_view["result"] if isinstance(login_view["result"], dict) else {}
        remaining = account.session_expires_at - int(time.time())
        warnings = []
        if remaining < WARN_BEFORE_SECONDS:
            warnings.append(f"The Dify login expires in {max(remaining, 0) // 3600} hours; Dify logins cannot be "
                            "extended, so reconnect and approve again before it ends.")
        if client and server and client != server:
            warnings.append(f"difyctl {client} differs from the Dify server {server}; operations may be missing or changed.")
        return {"outcome": "needs_attention" if warnings else "success", "exit_code": 0,
                "result": {"account": {"account_id": account.account_id, "email": account.email},
                           "server": saved.get("server") or self.settings.dify_server,
                           "workspace_id": saved.get("workspace_id"), "difyctl_version": client,
                           "server_version": server, "session_expires_at": account.session_expires_at,
                           "session_expires_in_seconds": max(remaining, 0),
                           "scopes": [s for s in token.scopes if s in SCOPE_LEVELS], "client_id": account.client_id},
                "error": None, "next_step": None, "notices": warnings, "timed_out": False}

    async def refresh_catalog(self, token) -> dict:
        return await self.simple(token, LEVELS["read"], "difyctl_refresh_catalog", ["refresh", "cache", "--json"],
                                 after=lambda account, _: self.clear_descriptors(account.account_id))

    async def use_workspace(self, token, workspace_id: str) -> dict:
        if not isinstance(workspace_id, str) or not WORKSPACE_ID.fullmatch(workspace_id):
            return Failure("invalid_input", "workspace_id must be a Dify workspace id.").payload
        return await self.simple(token, LEVELS["write"], "difyctl_use_workspace",
                                 ["use", "workspace", workspace_id, "--json"], exclusive=True,
                                 after=lambda account, _: self.clear_descriptors(account.account_id))

    async def logout(self, token) -> dict:
        def finish(account, result):
            remove_tree(self.store.clear_session(account.account_id), self.settings.sessions_dir)
            self.end_session(account)
            result["next_step"] = "Logged out of Dify; all MCP tokens of this account were revoked."
        return await self.simple(token, LEVELS["destructive"], "difyctl_logout", ["logout", "--json"],
                                 exclusive=True, after=finish)


MCP_USAGE = (
    "Run catalog operations with difyctl_read / difyctl_write / difyctl_destructive by effect "
    "(see mcp_tool on each entry). `difyctl <op> --a-b x` maps to {operation: \"<op>\", input: {\"a_b\": \"x\"}}. "
    "Hints {op, input} in results can be passed back as operation and input. Login is handled by MCP OAuth."
)


def annotate(value):
    """Add mcp_tool to descriptors and help entries so clients pick the right tool."""
    if isinstance(value, list):
        return [annotate(v) for v in value]
    if not isinstance(value, dict):
        return value
    value = {k: annotate(v) for k, v in value.items()}
    if isinstance(value.get("op"), str):
        value["mcp_tool"] = tool_for(value["op"], value.get("effect"))
        examples = value.get("examples")
        if isinstance(examples, list) and value["mcp_tool"].startswith("difyctl_"):
            value["mcp_examples"] = [{"tool": value["mcp_tool"], "arguments": {"operation": value["op"], "input": e.get("input", {})}}
                                     for e in examples if isinstance(e, dict)]
    elif isinstance(value.get("id"), str) and ("effect" in value or "summary" in value):
        value["mcp_tool"] = tool_for(value["id"], value.get("effect"))
    return value
