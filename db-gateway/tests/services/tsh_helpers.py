"""Helpers shared by the teleport tool tests (fixtures live in conftest.py)."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
from pathlib import Path

_SRC_FILES = {
    "current-profile": "teleport.dify.dev",
    "teleport.dify.dev.yaml": "web_proxy_addr: teleport.dify.dev:443\nuser: u\n",
    "bin/.config.json": "{}",
    "keys/teleport.dify.dev/u.key": "key",
    "keys/teleport.dify.dev/u": "ssh-key",
    "keys/teleport.dify.dev/certs.pem": "ca",
    "keys/teleport.dify.dev/cas/teleport.dify.dev.pem": "ca",
    "keys/teleport.dify.dev/u-ssh/teleport.dify.dev-cert.pub": "ssh-cert",
    "keys/teleport.dify.dev/u-app/teleport.dify.dev/x.crt": "app",
    "keys/teleport.dify.dev/u-kube/teleport.dify.dev/c.cred": "kube",
    "keys/teleport.dify.dev/u-db/teleport.dify.dev/d.crt": "db",
}


def write_tsh_src(root: Path, cert: str = "cert-1") -> Path:
    """A minimal TELEPORT_HOME with a logged-in profile."""
    files = dict(_SRC_FILES)
    files["keys/teleport.dify.dev/u.crt"] = cert
    for rel, text in files.items():
        path = Path(root) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return Path(root)


def snapshot(path: str | Path) -> dict[str, tuple[int, int, str]]:
    """{relative path: (size, mtime_ns, sha256)} of every file under ``path``."""
    out: dict[str, tuple[int, int, str]] = {}
    for root, _dirs, files in os.walk(path):
        for name in files:
            full = os.path.join(root, name)
            st = os.stat(full)
            digest = hashlib.sha256(Path(full).read_bytes()).hexdigest()
            out[os.path.relpath(full, path)] = (st.st_size, st.st_mtime_ns, digest)
    return out


_FAKE_TSH = r'''#!{python}
import base64, fcntl, json, os, subprocess, sys, time
D = {dir!r}
argv = sys.argv[1:]
env = {{k: v for k, v in os.environ.items()
        if k in ("TELEPORT_HOME", "KUBECONFIG", "TELEPORT_TOOLS_VERSION") or "proxy" in k.lower()}}
is_login = argv[:2] in (["apps", "login"], ["kube", "login"])
entered = None
if argv[:1] == ["login"]:
    # Interactive `tsh login`, prompts as printed by tsh 18.11 (Step 0 on the server).
    user = next((a.split("=", 1)[1] for a in argv if a.startswith("--user=")), "u")
    sys.stdout.write("Enter password for Teleport user %s:" % user)
    sys.stdout.flush()
    pw = sys.stdin.readline().strip()
    sys.stdout.write("\r\nEnter an OTP code from a device:")
    sys.stdout.flush()
    otp = sys.stdin.readline().strip()
    sys.stdout.write("\r\n")
    entered = {{"password": pw, "otp": otp}}

def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)

with open(os.path.join(D, "lock"), "a+") as lk:
    fcntl.flock(lk, fcntl.LOCK_EX)
    with open(os.path.join(D, "calls.jsonl"), "a") as f:
        f.write(json.dumps({{"argv": argv, "env": env, "entered": entered}}) + "\n")
    resp = None
    mode_path = os.path.join(D, "mode.json")
    marker = os.path.join(D, "logged_in")
    if os.path.exists(mode_path) and not os.path.exists(marker):
        mode = json.load(open(mode_path))
        if is_login:
            open(marker, "w").close()
            if mode.get("on_login"):
                _write(*mode["on_login"])
            resp = {{}}
        else:
            resp = {{"code": mode["code"], "stderr": mode["stderr"]}}
    if resp is None:
        qpath, ipath = os.path.join(D, "queue.jsonl"), os.path.join(D, "index")
        lines = open(qpath).read().splitlines() if os.path.exists(qpath) else []
        idx = int(open(ipath).read() or 0) if os.path.exists(ipath) else 0
        if idx < len(lines):
            resp = json.loads(lines[idx])
            with open(ipath, "w") as f:
                f.write(str(idx + 1))
        else:
            resp = {{}}
    fcntl.flock(lk, fcntl.LOCK_UN)

if resp.get("write_file"):
    _write(*resp["write_file"])
if resp.get("profile") and os.environ.get("TELEPORT_HOME"):
    # A successful login leaves a profile behind, like tsh does.
    home = os.environ["TELEPORT_HOME"]
    _write(os.path.join(home, "current-profile"), "teleport.dify.dev")
    _write(os.path.join(home, "keys", "teleport.dify.dev", "u.crt"), resp["profile"])
out = base64.b64decode(resp["stdout_b64"]) if resp.get("stdout_b64") else resp.get("stdout", "").encode()
sys.stdout.buffer.write(out)
sys.stdout.flush()
sys.stderr.write(resp.get("stderr", ""))
sys.stderr.flush()
if resp.get("child_sleep"):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(%s)" % resp["child_sleep"]])
    _write(os.path.join(D, "child.pid"), str(child.pid))
time.sleep(resp.get("sleep", 0))
sys.exit(resp.get("code", 0))
'''


class FakeTsh:
    """Scriptable stand-in for the tsh binary (see the plan's fixture notes)."""

    def __init__(self, directory: Path) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = str(self.dir / "tsh")
        Path(self.path).write_text(_FAKE_TSH.format(python=sys.executable, dir=str(self.dir)))
        os.chmod(self.path, 0o755)

    def push(self, stdout: str = "", stdout_bytes: bytes | None = None, stderr: str = "",
             code: int = 0, sleep: float = 0, child_sleep: float | None = None,
             write_file: list[str] | None = None, profile: str | None = None) -> None:
        resp: dict = {"stdout": stdout, "stderr": stderr, "code": code, "sleep": sleep}
        if profile is not None:
            resp["profile"] = profile
        if stdout_bytes is not None:
            resp["stdout_b64"] = base64.b64encode(stdout_bytes).decode()
        if child_sleep is not None:
            resp["child_sleep"] = child_sleep
        if write_file is not None:
            resp["write_file"] = list(write_file)
        with open(self.dir / "queue.jsonl", "a") as f:
            f.write(json.dumps(resp) + "\n")

    def until_login(self, code: int = 1, stderr: str = "no creds",
                    on_login: tuple[str, str] | None = None) -> None:
        (self.dir / "mode.json").write_text(json.dumps(
            {"code": code, "stderr": stderr, "on_login": list(on_login) if on_login else None}
        ))

    @property
    def calls(self) -> list[dict]:
        path = self.dir / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line]

    def child_pid(self) -> int:
        return int((self.dir / "child.pid").read_text())


def alive(pid: int) -> bool:
    """False when the process is gone or a zombie (Linux /proc)."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat.rsplit(")", 1)[1].split()[0] != "Z"


def status_text(valid_until: str = "2030-01-01 00:00:00 +0000", remaining: str = "55m",
                roles: str = "production-read-all, support-lambda-logs") -> str:
    """`tsh status` output as tsh 18.11 prints it."""
    return (
        "> Profile URL:        https://teleport.dify.dev:443\n"
        "  Logged in as:       u\n"
        "  Cluster:            teleport.dify.dev\n"
        f"  Roles:              {roles}\n"
        f"  Valid until:        {valid_until} UTC [valid for {remaining}]\n"
    )


def push_login_ok(fake: "FakeTsh", *, kube: bool = True, apps: bool = True,
                  valid_until: str = "2030-01-01 00:00:00 +0000") -> None:
    """Queue one successful renewal: login, [kube login], [apps login], status."""
    fake.push(stdout="> Profile URL: https://teleport.dify.dev:443\n", profile="cert")
    if kube:
        fake.push(stdout="Logged into all Kubernetes clusters available.\n")
    if apps:
        fake.push(stdout="Logged into AWS app aws-support-logs.\n")
    fake.push(stdout=status_text(valid_until=valid_until))


def install_session(cfg, generation: int = 1, minutes_left: float = 55):
    """Make ``generation`` the live session without logging in (tests)."""
    from datetime import datetime, timedelta, timezone

    from db_gateway import teleport_session as ts

    sessions = Path(cfg.teleport.sessions_dir)
    root = sessions / f"gen-{generation}"
    write_tsh_src(root / "home")
    tmp = sessions / ".current.tmp"
    if tmp.is_symlink():
        tmp.unlink()
    os.symlink(f"gen-{generation}", tmp)
    os.replace(tmp, sessions / "current")
    snap = ts.SessionSnapshot(
        generation=generation, root=root,
        valid_until=datetime.now(timezone.utc) + timedelta(minutes=minutes_left),
        roles=("production-read-all", "support-lambda-logs"),
    )
    ts.SESSION._current = snap
    ts.SESSION._generation = max(ts.SESSION._generation, generation)
    return snap
