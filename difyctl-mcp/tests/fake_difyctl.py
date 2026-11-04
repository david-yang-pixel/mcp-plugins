#!/usr/bin/env python3
"""A stand-in for difyctl 1.18 with the same JSON shapes, exit codes and login states.

Tests steer it through $HOME/fake.json and read its call log from $HOME/fake-log.jsonl.
"""
import json
import os
import signal
import socket
import sys
import time
from pathlib import Path

HOME = Path(os.environ["HOME"])
CONFIG = Path(os.environ["DIFY_CONFIG_DIR"])
CONTROL = json.loads((HOME / "fake.json").read_text()) if (HOME / "fake.json").exists() else {}
EXIT = {"not_logged_in": 4, "auth_expired": 4, "access_denied": 4, "input_invalid": 2, "unknown_op": 6,
        "catalog_unavailable": 6, "rate_limited": 7, "server_error": 1}
METHOD_EFFECT = {"GET": "read", "POST": "write", "PUT": "write", "PATCH": "write", "DELETE": "destructive"}
STRING = {"type": "string"}
FILES = {"anyOf": [{"type": "object", "additionalProperties": {"anyOf": [
    {"type": "string", "format": "binary"}, {"type": "array", "items": {"type": "string", "format": "binary"}}]}},
    {"type": "null"}]}
ATTACHMENTS = {"anyOf": [{"type": "array", "items": {"type": "string", "format": "binary"}}, {"type": "null"}]}
CATALOG = {
    "describe.console_app": ("GET", "object", {"app_id": "path"}, {"app_id": STRING}),
    "get.console_app": ("GET", "list", {}, {"page": {"type": "integer"}}),
    "describe.run": ("GET", "object", {"app_id": "path", "run_id": "path"}, {"app_id": STRING, "run_id": STRING}),
    "run.console_app.workflow": ("POST", "sse", {"app_id": "path", "files": "file", "attachments": "file", "inputs": "body"},
                                 {"app_id": STRING, "files": FILES, "attachments": ATTACHMENTS,
                                  "inputs": {"type": "object", "additionalProperties": True}}),
    "upload.console_app.file": ("POST", "object", {"app_id": "path", "file": "file"},
                                {"app_id": STRING, "file": {"type": "string", "format": "binary"}}),
    "create.console_app.workflow": ("POST", "object", {"name": "body"}, {"name": STRING}),
    "check.console_app.dsl": ("POST", "object", {"yaml_content": "body"}, {"yaml_content": STRING}),
    "export.console_app.dsl": ("GET", "object", {"app_id": "path", "include_secret": "query"},
                               {"app_id": STRING, "include_secret": {"type": "boolean", "default": False}}),
    "reset.webapp": ("POST", "object", {"app_id": "path"}, {"app_id": STRING}),
    "restore.console_app.version": ("POST", "object", {"app_id": "path"}, {"app_id": STRING}),
    "delete.plugin": ("DELETE", "object", {"plugin_id": "path"}, {"plugin_id": STRING}),
    "delete.account.current_session": ("DELETE", "object", {}, {}),
    "stop.run": ("POST", "object", {"app_id": "path", "task_id": "path"}, {"app_id": STRING, "task_id": STRING}),
    "noeffect.thing": ("TRACE", "object", {}, {}),
    "download.thing": ("GET", "file", {}, {}),
}


def log(entry):
    with open(HOME / "fake-log.jsonl", "a") as output:
        output.write(json.dumps(entry) + "\n")


def out(value, code=0):
    print(json.dumps(value, indent=2))
    sys.exit(code)


def fail(code, message="failed", stdout=None):
    if stdout is not None:
        print(json.dumps(stdout, indent=2))
    print("notice: something happened", file=sys.stderr)
    print(json.dumps({"error": {"code": code, "message": message}}, indent=2), file=sys.stderr)
    sys.exit(EXIT.get(code, 1))


def state():
    path = CONFIG / "state.json"
    return json.loads(path.read_text()) if path.exists() else {}


def save(value):
    CONFIG.mkdir(parents=True, exist_ok=True)
    (CONFIG / "state.json").write_text(json.dumps(value))


def descriptor(op):
    method, kind, bind, props = CATALOG[op]
    value = {"id": op.replace(".", " "), "usage": "difyctl " + op.replace(".", " ") + " [flags]", "summary": op,
             "input": {"type": "object", "properties": props}, "positional": [],
             "examples": [{"title": "example", "input": {"app_id": "<app_id>"}, "command": "difyctl ..."}],
             "op": op, "method": method, "path": "/x", "kind": kind, "bind": bind, "deprecated": False}
    if method in METHOD_EFFECT:
        value["effect"] = METHOD_EFFECT[method]
    return value


def main():
    args = sys.argv[1:]
    log({"args": args, "cwd": os.getcwd(), "env": sorted(os.environ), "config": str(CONFIG)})
    words = []
    for arg in args:
        if arg.startswith("--"):
            break
        words.append(arg)
    current = state()
    if words == ["version"]:
        out({"client": {"version": "1.18.0"}, "server": {"version": CONTROL.get("server_version", "1.18.0")}})
    if words == ["login"] and "--resume" in args:
        if current.get("pending"):
            if CONTROL.get("deny"):
                save({})
                fail("access_denied", "authorization denied")
            if CONTROL.get("approve"):
                login = {"server": current["server"], "email": CONTROL.get("email", "alice@example.com"),
                         "account": {"id": CONTROL.get("account_id", "acc-1")}, "workspace_id": "ws-1"}
                save({"login": login})
                out(login)
            out({"status": "pending"})
        if current.get("login") and not CONTROL.get("expired"):
            out(current["login"])
        fail("not_logged_in", "not logged in")
    if words == ["login"]:
        server = args[args.index("--server") + 1]
        assert "--no-wait" in args and "--no-keyring" in args and "--no-browser" in args
        save({"pending": True, "server": server})
        print("open " + server + "/device", file=sys.stderr)
        out({"status": "pending", "verification_uri": CONTROL.get("verification_uri", server + "/device"),
             "user_code": "ABCD-1234", "expires_in": 900})
    if words == ["logout"]:
        with open(HOME / "logouts", "a") as output:
            output.write(str(CONFIG) + "\n")
        if not current.get("login"):
            fail("not_logged_in")
        save({})
        out({"logged_out": True, "server": "https://cloud.dify.ai"})
    if not current.get("login") or CONTROL.get("expired"):
        if words and words[0] != "help":
            fail("auth_expired" if CONTROL.get("expired") else "not_logged_in")
    if words and words[0] == "help":
        terms = words[1:]
        if not terms:
            out({"get": {"count": 3}, "run": {"count": 1}, "login": {"summary": "Log in", "count": 1}})
        key = ".".join(" ".join(terms).replace(".", " ").split())
        if key in CATALOG:
            out(descriptor(key))
        if key == "use.workspace":
            out({"id": "use workspace", "summary": "Pin", "effect": "write", "input": {}, "positional": ["workspace_id"]})
        entries = [{"id": op.replace(".", " "), "summary": op, "effect": METHOD_EFFECT.get(CATALOG[op][0])}
                   for op in CATALOG if any(t in op for t in terms)]
        out({"entries": entries[:20], "total": len(entries)})
    if words == ["use", "workspace"] or words[:2] == ["use", "workspace"]:
        current["login"]["workspace_id"] = words[2]
        save(current)
        out({"workspace_id": words[2], "name": "Workspace"})
    if words == ["refresh", "cache"]:
        out({"ops": len(CATALOG), "fingerprint": "abc", "path": str(CONFIG / "catalog.json")})
    if words == ["describe", "account"]:
        out({"id": CONTROL.get("account_id", "acc-1"), "email": CONTROL.get("email", "alice@example.com"), "name": "Alice"})
    if words == ["get", "account", "session"]:
        out({"data": [{"id": "s0", "device_label": "elsewhere", "expires_at": int(time.time()) + 3600},
                      {"id": "s1", "device_label": socket.gethostname(),
                       "expires_at": CONTROL.get("session_expires_at", int(time.time()) + 10 * 86400)}], "page": 1})
    op = ".".join(words)
    if op not in CATALOG:
        fail("unknown_op", "unknown op")
    data = json.loads(sys.stdin.read() or "{}") if "@-" in args else {}
    if "fail" in data:
        fail(data["fail"], "simulated", stdout={"status": "failed"} if data["fail"] == "server_error" else None)
    if "sleep" in data:
        if data.get("ignore_interrupt"):
            signal.signal(signal.SIGINT, signal.SIG_IGN)
        else:
            def interrupted(*_):
                print("stream ended early: cancelled", file=sys.stderr)
                out({"status": "incomplete", "text": {"answer": ""}, "hints": [], "run_id": "run-1"})
            signal.signal(signal.SIGINT, interrupted)
        time.sleep(data["sleep"])
    if "big" in data:
        out({"data": "x" * data["big"]})
    if "--stream" in args:
        only = [args[i + 1] for i, a in enumerate(args) if a == "--only"]
        events = [{"event": "workflow_started", "task_id": "task-1", "workflow_run_id": "run-1"},
                  {"event": "workflow_finished", "workflow_run_id": "run-1", "data": {"status": "succeeded"}}]
        for event in events:
            if not only or event["event"] in only:
                print(json.dumps(event))
        sys.exit(0)
    out({"op": op, "input": data, "cwd": os.getcwd(), "env": sorted(os.environ), "token": "dfoa_abc.DEF-123",
         "status": data.get("status"), "args": args, "hints": data.get("hints", [])})


if __name__ == "__main__":
    main()
