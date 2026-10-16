#!/usr/bin/env python3
"""Restricted SSH entrypoint for this repository's eight production services.

Install root-owned at /usr/local/sbin/mcp-plugins-deploy. It accepts a pinned
image and runtime values on stdin, never shell commands, scripts or mount paths.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

REPOSITORY = "david-yang-pixel/mcp-plugins"
ROOT = Path("/opt/mcp-plugins")
TARGETS = Path("/etc/mcp-plugins/targets.json")
SHA = re.compile(r"[0-9a-f]{40}\Z")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
MAX_PAYLOAD = 1024 * 1024


class DeployError(Exception):
    """An intentionally non-sensitive error."""


def command(args, *, stdin=None, timeout=120):
    try:
        p = subprocess.run(args, input=stdin, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise DeployError("Operation timed out: " + args[0]) from None
    if p.returncode:
        # Docker can include configuration in its errors. Keep it off CI logs.
        raise DeployError("Operation failed: " + " ".join(args[:2]))
    return p.stdout


def inspect(name):
    return json.loads(command(["docker", "inspect", name]))[0]


def validate_request(service, revision, payload, target):
    if not SHA.fullmatch(revision):
        raise DeployError("Invalid revision")
    if not DIGEST.fullmatch(payload.get("digest", "")):
        raise DeployError("An immutable image digest is required")
    if set(payload) != {"digest", "environment", "files", "settings", "registry_user", "registry_token", "deployer_sha256"}:
        raise DeployError("Unexpected payload fields")
    if payload["deployer_sha256"] != hashlib.sha256(Path(__file__).read_bytes()).hexdigest():
        raise DeployError("Installed deploy helper differs from this commit; update it through admin SSH first")
    env = payload["environment"]
    if not isinstance(env, dict) or set(env) != set(target["env_keys"]):
        raise DeployError("Environment keys differ from the installed service contract")
    for key, value in env.items():
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or not isinstance(value, str) or any(c in value for c in "\r\n\0"):
            raise DeployError("Invalid environment entry")
    if not env.get(target["auth_key"]):
        raise DeployError("Service authentication must not be empty")
    if set(payload["files"]) != set(target["secret_file_paths"]):
        raise DeployError("File secrets differ from the installed service contract")
    if any(not isinstance(v, str) or not v or "\0" in v for v in payload["files"].values()):
        raise DeployError("Invalid file secret")
    if service == "db-gateway":
        if not isinstance(payload["settings"], dict):
            raise DeployError("DB settings are required")
    elif payload["settings"] is not None:
        raise DeployError("Unexpected settings file")
    if not isinstance(payload["registry_user"], str) or not re.fullmatch(r"[A-Za-z0-9_-]+(?:\[bot\])?", payload["registry_user"]):
        raise DeployError("Invalid registry identity")
    if not isinstance(payload["registry_token"], str) or not payload["registry_token"]:
        raise DeployError("Missing registry credential")
    return f"ghcr.io/{REPOSITORY}/{service}@{payload['digest']}"


def validate_current(current, target, service):
    host = current["HostConfig"]
    if host["NetworkMode"] != "host" or host.get("Privileged"):
        raise DeployError("Live network/privilege configuration drifted")
    allowed = {m["destination"]: m for m in target["mounts"]}
    generated = set(target["secret_file_paths"].values())
    actual = {m["Destination"]: m for m in current["Mounts"]}
    if not set(allowed) <= set(actual) or set(actual) - set(allowed) - generated:
        raise DeployError("Live mount destinations drifted")
    for destination, entry in actual.items():
        source = entry["Source"]
        if entry["Type"] != "bind":
            raise DeployError("Unexpected live mount type")
        if destination in generated or allowed[destination]["source"].startswith("@"):
            # Initial DB mount paths and subsequent immutable release files.
            initial = {"/app/.env": "/opt/dify-db/.env", "/app/config.yaml": "/opt/dify-db/config.yaml"}
            if source != initial.get(destination) and not source.startswith(str(ROOT / "releases" / service) + "/"):
                raise DeployError("Generated mount source drifted")
        elif source != allowed[destination]["source"]:
            raise DeployError("Live mount source drifted")
    if bool(host.get("Init")) != target["init"] or current["Config"].get("User", ""):
        raise DeployError("Live process configuration drifted")


def request_json(port, path, key, body=None, timeout=10):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": "Bearer " + key, "X-API-Key": key,
                 "Content-Type": "application/json", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


def verify_service(target, env, *, require_ready=False):
    key, port = env[target["auth_key"]], target["port"]
    health = request_json(port, target["health_path"], key)
    if health.get("ok") is False:
        raise DeployError("Service reports degraded health")
    tools = request_json(port, "/mcp", key, {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    if not tools.get("result", {}).get("tools"):
        raise DeployError("Authenticated MCP discovery failed")
    # Verify authentication is actually enforced, without making a tool call.
    try:
        request_json(port, "/mcp", "invalid-ci-probe", {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    except urllib.error.HTTPError as exc:
        if exc.code not in (401, 403):
            raise DeployError("Unexpected authentication rejection") from None
    else:
        raise DeployError("Unauthenticated MCP request was accepted")
    if require_ready:
        ready = request_json(port, "/ready", key, timeout=45)
        if not ready.get("ok"):
            raise DeployError("Browser session is not ready")
    return len(tools["result"]["tools"])


def secure_write(path, content):
    path.write_text(content)
    path.chmod(0o600)


def pull_image(image, revision, payload):
    with tempfile.TemporaryDirectory(prefix="mcp-registry-") as folder:
        args = ["docker", "--config", folder]
        command(args + ["login", "ghcr.io", "--username", payload["registry_user"], "--password-stdin"], stdin=payload["registry_token"].encode())
        command(args + ["pull", image], timeout=900)
    metadata = json.loads(command(["docker", "image", "inspect", image]))[0]
    labels = metadata["Config"].get("Labels") or {}
    if labels.get("org.opencontainers.image.revision") != revision or labels.get("org.opencontainers.image.source") != "https://github.com/" + REPOSITORY:
        raise DeployError("Image source/revision label mismatch")
    return metadata["Id"]


def run_arguments(service, image, revision, target, release):
    args = ["docker", "run", "-d", "--name", service, "--network", "host", "--restart", "always",
            "--label", "io.mcp-plugins.managed=true", "--label", "io.mcp-plugins.revision=" + revision,
            "--env-file", str(release / "runtime.env")]
    if target["init"]:
        args.append("--init")
    for mount in target["mounts"]:
        source = {"@env": str(release / "runtime.env"), "@settings": str(release / "config.yaml")}.get(mount["source"], mount["source"])
        if not Path(source).exists():
            raise DeployError("Required mount source does not exist")
        definition = f"type=bind,source={source},target={mount['destination']}"
        if mount["readonly"]:
            definition += ",readonly"
        args += ["--mount", definition]
    for key, destination in target["secret_file_paths"].items():
        args += ["--mount", f"type=bind,source={release / key},target={destination},readonly"]
    return args + [image] + target["command"]


def wait_healthy(service, target, env, ready):
    deadline = time.monotonic() + (180 if ready else 75)
    while time.monotonic() < deadline:
        try:
            state = inspect(service)["State"]
            if state["Running"] and not state.get("Restarting"):
                return verify_service(target, env, require_ready=ready)
        except (OSError, ValueError, DeployError):
            pass
        time.sleep(3)
    raise DeployError("Health/authentication/readiness verification timed out")


def replace_container(service, revision, image, target, payload, release, expected_image=None):
    current = inspect(service)
    validate_current(current, target, service)
    old_env = dict(value.split("=", 1) for value in current["Config"]["Env"])
    ready = service == "zendesk-browser-relay"
    # A failed preflight must not stop the existing service.
    verify_service(target, old_env, require_ready=ready)
    args = run_arguments(service, image, revision, target, release)
    backup = service + ".rollback-" + str(time.time_ns())
    old_policy = current["HostConfig"]["RestartPolicy"]
    restart = old_policy["Name"] or "no"
    if restart == "on-failure" and old_policy.get("MaximumRetryCount"):
        restart += ":" + str(old_policy["MaximumRetryCount"])
    secure_write(release / "previous.json", json.dumps({"container": backup, "id": current["Id"], "image": current["Image"], "restart": restart}))
    stopped = renamed = False
    try:
        command(["docker", "stop", "--time", "60", service], timeout=90)
        stopped = True
        command(["docker", "rename", service, backup])
        renamed = True
        command(["docker", "update", "--restart=no", backup])
        command(args)
        count = wait_healthy(service, target, payload["environment"], ready)
        deployed = inspect(service)
        if expected_image and deployed["Image"] != expected_image:
            raise DeployError("Unexpected final image identity")
        actual_env = dict(x.split("=", 1) for x in deployed["Config"]["Env"])
        if any(actual_env.get(k) != v for k, v in payload["environment"].items()):
            raise DeployError("Runtime environment readback mismatch")
        print(json.dumps({"service": service, "revision": revision, "status": "deployed", "tools": count, "rollback_container": backup}), flush=True)
        return {"service": service, "revision": revision, "image": image, "container": deployed["Id"], "previous_container": backup, "release": str(release)}
    except BaseException:
        # Only remove a container created by this transaction. Original services/data
        # and historical backups are retained; their writable layers stay intact.
        if renamed:
            p = subprocess.run(["docker", "inspect", service], capture_output=True)
            if p.returncode == 0:
                candidate = json.loads(p.stdout)[0]
                if candidate["Config"].get("Labels", {}).get("io.mcp-plugins.revision") != revision:
                    raise DeployError("Rollback stopped: unexpected container owns the service name") from None
                command(["docker", "rm", "-f", service])
            command(["docker", "rename", backup, service])
            command(["docker", "update", "--restart=" + restart, service])
        if stopped:
            command(["docker", "start", service])
            wait_healthy(service, target, old_env, ready)
        raise DeployError("Deployment failed; previous container restored and verified") from None


def deploy(service, revision, payload, target):
    image = validate_request(service, revision, payload, target)
    current = inspect(service)
    validate_current(current, target, service)
    print(json.dumps({"service": service, "stage": "pulling", "revision": revision}), flush=True)
    expected_image = pull_image(image, revision, payload)
    base = ROOT / "releases" / service
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    release = Path(tempfile.mkdtemp(prefix=revision[:12] + "-", dir=base))
    secure_write(release / "runtime.env", "".join(k + "=" + v + "\n" for k, v in sorted(payload["environment"].items())))
    if payload["settings"] is not None:
        secure_write(release / "config.yaml", json.dumps(payload["settings"], ensure_ascii=False))
    for key, value in payload["files"].items():
        secure_write(release / key, value)
    record = replace_container(service, revision, image, target, payload, release, expected_image)
    state = ROOT / "state"
    state.mkdir(mode=0o700, exist_ok=True)
    secure_write(state / (service + ".json"), json.dumps(record, indent=2))


def main():
    os.umask(0o077)
    def interrupted(signum, frame):
        raise DeployError("Deployment interrupted")
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGHUP, interrupted)
    request = os.environ.get("SSH_ORIGINAL_COMMAND", "").split()
    targets = json.loads(TARGETS.read_text())
    if len(request) != 3 or request[0] != "deploy" or request[1] not in targets or not SHA.fullmatch(request[2]):
        raise DeployError("Only deployment of an allowlisted service and commit is permitted")
    raw = sys.stdin.buffer.read(MAX_PAYLOAD + 1)
    if len(raw) > MAX_PAYLOAD:
        raise DeployError("Payload is too large")
    try:
        payload = json.loads(raw)
    except ValueError:
        raise DeployError("Invalid JSON payload") from None
    with open("/run/lock/mcp-plugins-deploy.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        deploy(request[1], request[2], payload, targets[request[1]])


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        message = str(exc) if isinstance(exc, DeployError) else "Deployment failed before completion (sensitive details suppressed)"
        print(message, file=sys.stderr)
        sys.exit(1)
