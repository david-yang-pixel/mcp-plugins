#!/usr/bin/env python3
"""Restricted SSH entrypoint for existing and newly discovered MCP services.

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
import socket
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
SERVICE = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")
ENV_KEY = re.compile(r"[A-Z][A-Z0-9_]*\Z")


class DeployError(Exception):
    """An intentionally non-sensitive error."""


def validate_service_name(service):
    if not isinstance(service, str) or len(service) > 63 or not SERVICE.fullmatch(service):
        raise DeployError("Invalid service name")


def normalize_target(target):
    if not isinstance(target, dict):
        raise DeployError("Invalid deployment contract")
    result = {"init": False, "mounts": [], "command": [], "network": "bridge", **target}
    if set(result) != {"port", "auth_key", "health_path", "init", "mounts", "command", "network", "env_keys", "secret_file_paths"}:
        raise DeployError("Unexpected deployment contract fields")
    if type(result["port"]) is not int or not 1024 <= result["port"] <= 65535:
        raise DeployError("Invalid service port")
    if not isinstance(result["auth_key"], str) or not ENV_KEY.fullmatch(result["auth_key"]):
        raise DeployError("Invalid authentication key")
    if not isinstance(result["health_path"], str) or not re.fullmatch(r"/[a-zA-Z0-9/_-]*", result["health_path"]):
        raise DeployError("Invalid health path")
    if type(result["init"]) is not bool or result["network"] not in ("host", "bridge"):
        raise DeployError("Invalid process configuration")
    if not isinstance(result["command"], list) or any(not isinstance(v, str) or "\0" in v for v in result["command"]):
        raise DeployError("Invalid container command")
    keys = result["env_keys"]
    if not isinstance(keys, list) or any(not isinstance(k, str) or not ENV_KEY.fullmatch(k) for k in keys) or len(keys) != len(set(keys)) or result["auth_key"] not in keys:
        raise DeployError("Invalid environment key contract")
    files = result["secret_file_paths"]
    if not isinstance(files, dict) or any(not ENV_KEY.fullmatch(k) or not isinstance(v, str) for k, v in files.items()):
        raise DeployError("Invalid secret file contract")
    if not isinstance(result["mounts"], list):
        raise DeployError("Invalid mount contract")
    destinations = []
    for m in result["mounts"]:
        if not isinstance(m, dict) or set(m) != {"source", "destination", "readonly"} or not isinstance(m["source"], str) or not isinstance(m["destination"], str) or type(m["readonly"]) is not bool:
            raise DeployError("Invalid mount contract")
        destinations.append(m["destination"])
    destinations += list(files.values())
    if len(destinations) != len(set(destinations)):
        raise DeployError("Duplicate mount destination")
    if any(not p.startswith('/') or '..' in Path(p).parts or any(c in p for c in ',\n\r\0') for p in destinations):
        raise DeployError("Invalid mount destination")
    return result


def resolve_target(service, declared, installed):
    """Existing privileged mounts stay administrator-owned; new services are isolated."""
    validate_service_name(service)
    target = normalize_target(declared)
    if service in installed:
        trusted = normalize_target({"network": "host", **installed[service]})
        for key in trusted:
            if key != "env_keys" and target[key] != trusted[key]:
                raise DeployError("Legacy infrastructure contract requires administrator update")
    else:
        if target["network"] != "bridge":
            raise DeployError("New services must use isolated bridge networking")
        for m in target["mounts"]:
            if not re.fullmatch(r"@data/[a-z][a-z0-9-]{0,40}", m["source"]) or not m["destination"].startswith('/var/lib/mcp/'):
                raise DeployError("New services may only mount their own managed data directories")
        if any(not p.startswith('/run/mcp-secrets/') for p in target["secret_file_paths"].values()):
            raise DeployError("New secret files must be under /run/mcp-secrets/")
    return target


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


def inspect_optional(name):
    # A daemon/permission error is not evidence that the container is absent.
    ids = command(["docker", "container", "ls", "--all", "--filter", "name=^/" + name + "$", "--format", "{{.ID}}"])
    return inspect(name) if ids.strip() else None


def validate_request(service, revision, payload, target):
    validate_service_name(service)
    if not SHA.fullmatch(revision):
        raise DeployError("Invalid revision")
    if not DIGEST.fullmatch(payload.get("digest", "")):
        raise DeployError("An immutable image digest is required")
    if set(payload) != {"digest", "environment", "files", "settings", "registry_user", "registry_token", "deployer_sha256", "target"}:
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
    if host["NetworkMode"] != target.get("network", "host") or host.get("Privileged"):
        raise DeployError("Live network/privilege configuration drifted")
    if target.get("network") == "bridge":
        expected = {str(target['port']) + '/tcp': [{"HostIp": "127.0.0.1", "HostPort": str(target['port'])}]}
        if host.get('PortBindings') != expected:
            raise DeployError("Live published port drifted")
    allowed = {m["destination"]: m for m in target["mounts"]}
    generated = set(target["secret_file_paths"].values())
    actual = {m["Destination"]: m for m in current["Mounts"]}
    if not set(allowed) <= set(actual) or set(actual) - set(allowed) - generated:
        raise DeployError("Live mount destinations drifted")
    for destination, entry in actual.items():
        source = entry["Source"]
        if entry["Type"] != "bind":
            raise DeployError("Unexpected live mount type")
        if destination in generated or allowed[destination]["source"] in ("@env", "@settings"):
            # Initial DB mount paths and subsequent immutable release files.
            initial = {"/app/.env": "/opt/dify-db/.env", "/app/config.yaml": "/opt/dify-db/config.yaml"}
            if source != initial.get(destination) and not source.startswith(str(ROOT / "releases" / service) + "/"):
                raise DeployError("Generated mount source drifted")
        elif source != mount_source(service, allowed[destination]["source"]):
            raise DeployError("Live mount source drifted")
    if bool(host.get("Init")) != target["init"] or (target.get("network", "host") == "host" and current["Config"].get("User", "")):
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
    fd, name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as output:
            output.write(content)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def mount_source(service, source):
    if source.startswith('@data/'):
        return str(ROOT / 'data' / service / source.split('/', 1)[1])
    return source


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
    network = target.get("network", "host")
    args = ["docker", "run", "-d", "--name", service, "--network", network, "--restart", "always",
            "--label", "io.mcp-plugins.managed=true", "--label", "io.mcp-plugins.revision=" + revision,
            "--env-file", str(release / "runtime.env")]
    if network == "bridge":
        args += ["--publish", f"127.0.0.1:{target['port']}:{target['port']}",
                 "--cap-drop=ALL", "--security-opt=no-new-privileges:true"]
    if target["init"]:
        args.append("--init")
    for mount in target["mounts"]:
        source = {"@env": str(release / "runtime.env"), "@settings": str(release / "config.yaml")}.get(mount["source"], mount_source(service, mount["source"]))
        if mount["source"].startswith('@data/'):
            Path(source).mkdir(parents=True, exist_ok=True, mode=0o700)
        if not Path(source).exists():
            raise DeployError("Required mount source does not exist")
        definition = f"type=bind,source={source},target={mount['destination']}"
        if mount["readonly"]:
            definition += ",readonly"
        args += ["--mount", definition]
    for key, destination in target["secret_file_paths"].items():
        args += ["--mount", f"type=bind,source={release / key},target={destination},readonly"]
    return args + [image] + target["command"]


def wait_healthy(service, target, env, ready, timeout=None):
    deadline = time.monotonic() + (timeout if timeout is not None else (180 if ready else 75))
    while time.monotonic() < deadline:
        try:
            state = inspect(service)["State"]
            if state["Running"] and not state.get("Restarting"):
                return verify_service(target, env, require_ready=ready)
        except (OSError, ValueError, DeployError):
            pass
        time.sleep(3)
    raise DeployError("Health/authentication/readiness verification timed out")


def verify_readback(service, expected_image, environment):
    deployed = inspect(service)
    if expected_image and deployed["Image"] != expected_image:
        raise DeployError("Unexpected final image identity")
    actual = dict(x.split("=", 1) for x in deployed["Config"]["Env"])
    if any(actual.get(k) != v for k, v in environment.items()):
        raise DeployError("Runtime environment readback mismatch")
    return deployed


def remove_candidate(service, revision, expected_image):
    candidate = inspect_optional(service)
    if candidate is not None:
        labels = candidate["Config"].get("Labels", {})
        if labels.get("io.mcp-plugins.revision") != revision or labels.get("io.mcp-plugins.managed") != "true" or (expected_image and candidate['Image'] != expected_image):
            raise DeployError("Cleanup stopped: unexpected container owns the service name")
        command(["docker", "rm", "-f", service])


def create_container(service, revision, image, target, payload, release, expected_image=None, persist=None):
    if inspect_optional(service) is not None:
        raise DeployError("First deployment refused: container name is already in use")
    args = run_arguments(service, image, revision, target, release)
    try:
        command(args)
        count = wait_healthy(service, target, payload['environment'], False)
        deployed = verify_readback(service, expected_image, payload['environment'])
        record = {"service": service, "revision": revision, "image": image, "container": deployed['Id'],
                  "previous_container": None, "release": str(release), "target": target}
        if persist:
            persist(record)
    except BaseException:
        remove_candidate(service, revision, expected_image)
        raise DeployError("First deployment failed; only this attempt's container was removed") from None
    print(json.dumps({"service": service, "revision": revision, "status": "created", "tools": count}), flush=True)
    return record


def replace_container(service, revision, image, target, payload, release, expected_image=None, persist=None):
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
        deployed = verify_readback(service, expected_image, payload['environment'])
        record = {"service": service, "revision": revision, "image": image, "container": deployed["Id"],
                  "previous_container": backup, "release": str(release), "target": target}
        if persist:
            persist(record)
        print(json.dumps({"service": service, "revision": revision, "status": "deployed", "tools": count, "rollback_container": backup}), flush=True)
        return record
    except BaseException:
        # Only remove a container created by this transaction. Original services/data
        # and historical backups are retained; their writable layers stay intact.
        if renamed:
            remove_candidate(service, revision, expected_image)
            command(["docker", "rename", backup, service])
            command(["docker", "update", "--restart=" + restart, service])
        if stopped:
            command(["docker", "start", service])
            wait_healthy(service, target, old_env, ready)
        raise DeployError("Deployment failed; previous container restored and verified") from None


def preflight(service, target, installed, current):
    state_path = ROOT / 'state' / (service + '.json')
    previous = json.loads(state_path.read_text()) if state_path.exists() else None
    if current:
        labels = current['Config'].get('Labels') or {}
        if not previous or previous.get('container') != current['Id'] or labels.get('io.mcp-plugins.managed') != 'true':
            raise DeployError("Container is not owned by this service's deployment record")
        old_target = previous.get('target', installed.get(service))
        if old_target is None:
            raise DeployError("Missing previous service contract")
        # Port/mount/network changes need an explicit migration, not an implicit replacement.
        old_target = normalize_target({'network': 'host' if service in installed else 'bridge', **old_target})
        if any(old_target[k] != target[k] for k in ('port', 'network', 'mounts', 'secret_file_paths', 'init', 'command')):
            raise DeployError("Existing infrastructure changed; an explicit migration is required")
        validate_current(current, old_target, service)
    reservations = {name: item['port'] for name, item in installed.items()}
    for path in (ROOT / 'state').glob('*.json'):
        item = json.loads(path.read_text())
        if item.get('target'):
            reservations[path.stem] = item['target']['port']
    if any(name != service and port == target['port'] for name, port in reservations.items()):
        raise DeployError("Service port is already reserved by another MCP")
    if current is None:
        try:
            with socket.socket() as listener:
                listener.bind(('127.0.0.1', target['port']))
        except OSError:
            raise DeployError("Service port is already in use") from None


def deploy(service, revision, payload, target, installed=None):
    image = validate_request(service, revision, payload, target)
    current = inspect_optional(service)
    preflight(service, target, installed or {}, current)
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
    state = ROOT / "state"
    state.mkdir(mode=0o700, exist_ok=True)
    def persist(record):
        secure_write(state / (service + '.json'), json.dumps(record, indent=2))
    operation = replace_container if current else create_container
    operation(service, revision, image, target, payload, release, expected_image, persist)


def main():
    os.umask(0o077)
    def interrupted(signum, frame):
        raise DeployError("Deployment interrupted")
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGHUP, interrupted)
    request = os.environ.get("SSH_ORIGINAL_COMMAND", "").split()
    targets = json.loads(TARGETS.read_text())
    if len(request) != 3 or request[0] != "deploy" or not SERVICE.fullmatch(request[1]) or len(request[1]) > 63 or not SHA.fullmatch(request[2]):
        raise DeployError("Only deployment of a valid service and commit is permitted")
    raw = sys.stdin.buffer.read(MAX_PAYLOAD + 1)
    if len(raw) > MAX_PAYLOAD:
        raise DeployError("Payload is too large")
    try:
        payload = json.loads(raw)
    except ValueError:
        raise DeployError("Invalid JSON payload") from None
    target = resolve_target(request[1], payload.get('target'), targets)
    with open("/run/lock/mcp-plugins-deploy.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        deploy(request[1], request[2], payload, target, targets)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        message = str(exc) if isinstance(exc, DeployError) else "Deployment failed before completion (sensitive details suppressed)"
        print(message, file=sys.stderr)
        sys.exit(1)
