#!/usr/bin/env python3
"""Restricted SSH entrypoint for existing and newly discovered MCP services.

Install root-owned at /usr/local/sbin/mcp-plugins-deploy. It accepts a pinned
image and runtime values on stdin, never shell commands, scripts or mount paths.
"""
import fcntl
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import urllib.parse

REPOSITORY = "david-yang-pixel/mcp-plugins"
ROOT = Path("/opt/mcp-plugins")
TARGETS = Path("/etc/mcp-plugins/targets.json")
SHA = re.compile(r"[0-9a-f]{40}\Z")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
MAX_PAYLOAD = 1024 * 1024
SERVICE = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")
ENV_KEY = re.compile(r"[A-Z][A-Z0-9_]*\Z")
PUBLIC_ORIGIN = "https://zendesk.smlershou.top"
NGINX_ROUTES = Path("/etc/nginx/mcp-plugins.d")
NGINX_SITE = Path("/etc/nginx/sites-enabled/zd-agent.conf")
AUTO_PORTS = range(20000, 30000)


class DeployError(Exception):
    """An intentionally non-sensitive error."""


def validate_service_name(service):
    if not isinstance(service, str) or len(service) > 63 or not SERVICE.fullmatch(service):
        raise DeployError("Invalid service name")


def normalize_target(target):
    if not isinstance(target, dict):
        raise DeployError("Invalid deployment contract")
    result = {"init": False, "mounts": [], "command": [], "network": "bridge", "port": "auto", "shm_size": None, **target}
    result.setdefault('container_port', result['port'])
    allowed = {
        "port", "container_port", "auth_key", "health_path", "init", "mounts", "command",
        "network", "env_keys", "secret_file_paths", "shm_size",
    }
    if set(result) != allowed:
        raise DeployError("Unexpected deployment contract fields")
    if result['port'] != 'auto' and (type(result["port"]) is not int or not 1024 <= result["port"] <= 65535):
        raise DeployError("Invalid service port")
    if type(result['container_port']) is not int or not 1024 <= result['container_port'] <= 65535:
        raise DeployError("A fixed container_port between 1024 and 65535 is required")
    if not isinstance(result["auth_key"], str) or not ENV_KEY.fullmatch(result["auth_key"]):
        raise DeployError("Invalid authentication key")
    if not isinstance(result["health_path"], str) or not re.fullmatch(r"/[a-zA-Z0-9/_-]*", result["health_path"]):
        raise DeployError("Invalid health path")
    if type(result["init"]) is not bool or result["network"] not in ("host", "bridge"):
        raise DeployError("Invalid process configuration")
    shm_size = result["shm_size"]
    if shm_size is not None and (
        not isinstance(shm_size, str)
        or not re.fullmatch(r"[1-9][0-9]*[kmgt]?b?", shm_size)
    ):
        raise DeployError("Invalid shared memory size")
    if result['network'] == 'host' and result['port'] != result['container_port']:
        raise DeployError("Host networking requires the original fixed port")
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


def shm_bytes(value):
    """Return HostConfig.ShmSize bytes for an explicit shm_size contract.

    None means the contract omits --shm-size. The container then inherits the
    daemon default, which is not a fixed size this process can assume. Units
    follow Docker: optional kb/mb/gb/tb suffixes are binary, not decimal.
    """
    if value is None:
        return None
    text = value.lower()
    if len(text) > 1 and text[-1] == "b" and text[-2] in "kmgt":
        text = text[:-1]
    unit = 1
    if text[-1:] in "kmgtb":
        unit = {"b": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3, "t": 1024 ** 4}[text[-1]]
        text = text[:-1]
    return int(text) * unit


def recreate_migration_blocked(old_target, target):
    """Block replacements that would move ports, networks, or data mounts.

    init, command, and shm_size are process settings applied by recreating the
    container. Secret files may only gain entries under /run/mcp-secrets/;
    existing secret destinations stay unchanged.
    """
    if any(old_target[key] != target[key] for key in ("port", "container_port", "network", "mounts")):
        return True
    old_files, new_files = old_target["secret_file_paths"], target["secret_file_paths"]
    if any(new_files.get(key) != destination for key, destination in old_files.items()):
        return True
    return any(
        not str(destination).startswith("/run/mcp-secrets/")
        for key, destination in new_files.items()
        if key not in old_files
    )


def validate_current(current, target, service):
    host = current["HostConfig"]
    if host["NetworkMode"] != target.get("network", "host") or host.get("Privileged"):
        raise DeployError("Live network/privilege configuration drifted")
    if target.get("network") == "bridge":
        expected = {str(target.get('container_port', target['port'])) + '/tcp': [{"HostIp": "127.0.0.1", "HostPort": str(target['port'])}]}
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
    expected_shm = shm_bytes(target.get("shm_size"))
    if expected_shm is not None and host.get("ShmSize") != expected_shm:
        raise DeployError("Live shared memory size drifted")


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


def previous_state(service):
    path = ROOT / 'state' / (service + '.json')
    return json.loads(path.read_text()) if path.exists() else None


def reserved_ports(installed):
    ports = {v['port'] for v in installed.values()}
    for path in (ROOT / 'state').glob('*.json'):
        item = json.loads(path.read_text())
        if item.get('target'):
            ports.add(item['target']['port'])
    ids = command(['docker', 'container', 'ls', '--all', '--format', '{{.ID}}']).decode().splitlines()
    if ids:
        for item in json.loads(command(['docker', 'inspect', *ids])):
            for bindings in (item['HostConfig'].get('PortBindings') or {}).values():
                ports.update(int(binding['HostPort']) for binding in (bindings or []) if binding.get('HostPort'))
    return ports


def allocate_target(service, target, installed):
    target = dict(target)
    if target['port'] != 'auto':
        return target
    previous = previous_state(service)
    if previous is not None:
        old_port = previous.get('target', {}).get('port')
        if type(old_port) is not int or not 1024 <= old_port <= 65535:
            raise DeployError('Existing allocation is missing; refusing to change the service port')
        target['port'] = old_port
        return target
    reserved = reserved_ports(installed)
    for port in AUTO_PORTS:
        if port in reserved:
            continue
        try:
            with socket.socket() as listener:
                listener.bind(('127.0.0.1', port))
        except OSError:
            continue
        target['port'] = port
        return target
    raise DeployError('No free port in the automatic allocation range')


def parse_nginx(text, source=''):
    """Parse directives, retaining source and offsets for a narrowly scoped include edit."""
    pattern = r'''\s+|\#[^\n]*|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\$\{[^}]*\}|[{};]|[^\s{};#"']+'''
    tokens = [(m.group(), m.start()) for m in re.finditer(pattern, text)
              if not m.group().isspace() and not m.group().startswith('#')]
    index = 0

    def block(nested=False):
        nonlocal index
        nodes = []
        while index < len(tokens):
            value, offset = tokens[index]
            if value == '}':
                if not nested:
                    raise DeployError('Unexpected Nginx block ending')
                index += 1
                return nodes, offset
            words = []
            while index < len(tokens) and tokens[index][0] not in (';', '{', '}'):
                value = tokens[index][0]
                words.append(shlex.split(value)[0] if value.startswith(('"', "'")) else value)
                index += 1
            if not words or index >= len(tokens) or tokens[index][0] == '}':
                raise DeployError('Unsupported Nginx configuration structure')
            terminator, end = tokens[index]
            index += 1
            children = []
            if terminator == '{':
                children, end = block(True)
            nodes.append({'name': words[0], 'args': words[1:], 'children': children,
                          'source': source, 'start': offset, 'end': end})
        if nested:
            raise DeployError('Unclosed Nginx configuration block')
        return nodes, len(text)
    return block()[0]


def nginx_command(*args):
    return command(['nginx', *args])


def reload_nginx():
    command(['systemctl', 'reload', 'nginx'])


def nginx_documents():
    dump = nginx_command('-T').decode()
    parts = re.split(r'^# configuration file (.+):\s*$', dump, flags=re.M)
    return {parts[i]: parse_nginx(parts[i + 1], parts[i]) for i in range(1, len(parts), 2)}


def public_server(documents):
    hostname = urllib.parse.urlsplit(PUBLIC_ORIGIN).hostname
    found = []
    def visit(nodes):
        for node in nodes:
            children = node['children']
            if node['name'] == 'server' and any(n['name'] == 'server_name' and hostname in n['args'] for n in children) and any(n['name'] == 'listen' and 'ssl' in n['args'] for n in children):
                found.append(node)
            visit(children)
    for nodes in documents.values():
        visit(nodes)
    if len(found) != 1:
        raise DeployError('Expected exactly one HTTPS server for the MCP public domain')
    return found[0]


def expanded_nodes(nodes, documents, seen=()):
    for node in nodes:
        yield node
        if node['name'] == 'include':
            pattern = node['args'][0]
            if not pattern.startswith('/'):
                pattern = '/etc/nginx/' + pattern
            for filename, children in documents.items():
                if fnmatch.fnmatch(filename, pattern):
                    if filename in seen:
                        raise DeployError('Recursive Nginx include')
                    yield from expanded_nodes(children, documents, (*seen, filename))
        yield from expanded_nodes(node['children'], documents, seen)


def nginx_include_content(text):
    """Idempotently add one include to the existing HTTPS server, preserving all routes."""
    document = {str(NGINX_SITE): parse_nginx(text, str(NGINX_SITE))}
    block = public_server(document)
    pattern = str(NGINX_ROUTES / '*.conf')
    if any(n['name'] == 'include' and n['args'] == [pattern] for n in block['children']):
        return text
    return text[:block['end']] + '    # Per-service MCP routes managed by mcp-plugins-deploy.\n    include ' + pattern + ';\n' + text[block['end']:]


def install_nginx_include():
    """Administrator-only bootstrap; the restricted deployment command cannot call it."""
    nginx_command('-t')
    path = NGINX_SITE.resolve(strict=True)
    before = path.read_text()
    after = nginx_include_content(before)
    NGINX_ROUTES.mkdir(mode=0o700, parents=True, exist_ok=True)
    if before == after:
        return {'changed': False}
    mode = path.stat().st_mode & 0o777
    backup = ROOT / 'backups'
    backup.mkdir(mode=0o700, parents=True, exist_ok=True)
    secure_write(backup / ('nginx-site-' + str(time.time_ns()) + '.conf'), before)
    try:
        secure_write(path, after)
        path.chmod(mode)
        nginx_command('-t')
        reload_nginx()
    except BaseException:
        secure_write(path, before)
        path.chmod(mode)
        nginx_command('-t')
        reload_nginx()
        raise DeployError('Nginx include installation failed; original site restored') from None
    return {'changed': True, 'site_sha256': hashlib.sha256(after.encode()).hexdigest()}


def check_route_namespace(service, own_file):
    documents = nginx_documents()
    block = public_server(documents)
    nodes = list(expanded_nodes(block['children'], documents))
    if not any(n['name'] == 'include' and n['args'] == [str(NGINX_ROUTES / '*.conf')] for n in nodes):
        raise DeployError('Managed route include is not installed in the HTTPS server')
    path = '/' + service + '/mcp'
    namespace = '/' + service
    for node in nodes:
        if node['name'] != 'location' or node['source'] == str(own_file):
            continue
        args = node['args']
        if not args:
            raise DeployError('Invalid Nginx location')
        kind, location = (args[0], args[1]) if len(args) > 1 else ('', args[0])
        if kind in ('~', '~*'):
            try:
                conflict = re.search(location, path, re.I if kind == '~*' else 0) is not None
            except re.error:
                raise DeployError('Cannot safely compare an existing regex route') from None
        else:
            conflict = location == namespace or location.startswith(namespace + '/') or (kind != '=' and location != '/' and path.startswith(location))
        if conflict:
            raise DeployError('MCP path conflicts with an existing Nginx route')


def render_route(service, port, revision):
    validate_service_name(service)
    if type(port) is not int or not 1024 <= port <= 65535 or not SHA.fullmatch(revision):
        raise DeployError('Invalid route target')
    return f'''# Managed by mcp-plugins-deploy; service={service}
location = /{service}/mcp {{
    proxy_pass http://127.0.0.1:{port}/mcp;
    proxy_http_version 1.1;
    proxy_set_header Connection "";
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-MCP-Endpoint /{service}/mcp;
    proxy_buffering off;
    proxy_request_buffering off;
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;
    add_header X-MCP-Deployment {revision} always;
}}
'''


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise DeployError('Public MCP endpoint must not redirect authentication credentials')


def public_request(service, key):
    request = urllib.request.Request(PUBLIC_ORIGIN + '/' + service + '/mcp',
        data=json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list', 'params': {}}).encode(),
        headers={'Authorization': 'Bearer ' + key, 'X-API-Key': key,
                 'Content-Type': 'application/json', 'Accept': 'application/json'})
    opener = urllib.request.build_opener(NoRedirect())
    with opener.open(request, timeout=10) as response:
        return json.loads(response.read(1024 * 1024)), response.headers.get('X-MCP-Deployment')


def verify_public(service, env, target, revision=None):
    tools, header = public_request(service, env[target['auth_key']])
    if not tools.get('result', {}).get('tools') or (revision and header != revision):
        raise DeployError('Public HTTPS MCP discovery or route identity check failed')
    try:
        public_request(service, 'invalid-ci-probe')
    except urllib.error.HTTPError as exc:
        if exc.code not in (401, 403):
            raise DeployError('Public MCP authentication rejection failed') from None
    else:
        raise DeployError('Public MCP accepted an invalid credential')
    return PUBLIC_ORIGIN + '/' + service + '/mcp'


def wait_public(service, env, target, revision=None, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return verify_public(service, env, target, revision)
        except (OSError, ValueError, DeployError):
            time.sleep(2)
    raise DeployError('Public HTTPS MCP verification timed out')


class RouteTransaction:
    def __init__(self, service, target, revision, legacy=False):
        self.service, self.target, self.revision = service, target, revision
        self.legacy = legacy
        self.path = NGINX_ROUTES / (service + '.conf')
        self.previous = None
        self.changed = False
        self.loaded = False
        if legacy:
            return
        if not NGINX_ROUTES.is_dir() or self.path.is_symlink():
            raise DeployError('Managed Nginx directory missing or route is a symlink')
        if self.path.exists():
            self.previous = self.path.read_text()
            state = previous_state(service) or {}
            if hashlib.sha256(self.previous.encode()).hexdigest() != state.get('route_sha256'):
                raise DeployError('Existing managed Nginx route has unexpected ownership or drift')
        check_route_namespace(service, self.path)

    def publish(self, env):
        if not self.legacy:
            # Repeat immediately before the mutation, in case an administrator edited Nginx.
            check_route_namespace(self.service, self.path)
            current = self.path.read_text() if self.path.exists() else None
            if current != self.previous:
                raise DeployError('Nginx route changed during deployment')
            content = render_route(self.service, self.target['port'], self.revision)
            if content != self.previous:
                secure_write(self.path, content)
                self.changed = True
                nginx_command('-t')
                self.loaded = True  # A reload error can still have delivered its signal.
                reload_nginx()
        return wait_public(self.service, env, self.target, None if self.legacy else self.revision)

    def rollback(self):
        if not self.changed:
            return
        candidate = render_route(self.service, self.target['port'], self.revision)
        if self.path.is_symlink() or not self.path.exists() or self.path.read_text() != candidate:
            raise DeployError('Nginx route changed after publication; refusing to overwrite administrator changes')
        if self.previous is None:
            self.path.unlink()
        else:
            secure_write(self.path, self.previous)
        nginx_command('-t')
        if self.loaded:
            reload_nginx()
        self.changed = False

    def metadata(self):
        if self.legacy:
            return {}
        return {'route_sha256': hashlib.sha256(self.path.read_bytes()).hexdigest(), 'route_file': str(self.path)}


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
        args += ["--publish", f"127.0.0.1:{target['port']}:{target.get('container_port', target['port'])}",
                 "--cap-drop=ALL", "--security-opt=no-new-privileges:true"]
    if target["init"]:
        args.append("--init")
    if target.get("shm_size"):
        args += ["--shm-size", str(target["shm_size"])]
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


def verify_readback(service, expected_image, environment, target=None):
    deployed = inspect(service)
    if expected_image and deployed["Image"] != expected_image:
        raise DeployError("Unexpected final image identity")
    actual = dict(x.split("=", 1) for x in deployed["Config"]["Env"])
    if any(actual.get(k) != v for k, v in environment.items()):
        raise DeployError("Runtime environment readback mismatch")
    if target is not None:
        host = deployed["HostConfig"]
        expected_shm = shm_bytes(target.get("shm_size"))
        shm_mismatch = expected_shm is not None and host.get("ShmSize") != expected_shm
        if bool(host.get("Init")) != target["init"] or shm_mismatch:
            raise DeployError("Replacement process configuration mismatch")
    return deployed


def remove_candidate(service, revision, expected_image):
    candidate = inspect_optional(service)
    if candidate is not None:
        labels = candidate["Config"].get("Labels", {})
        if labels.get("io.mcp-plugins.revision") != revision or labels.get("io.mcp-plugins.managed") != "true" or (expected_image and candidate['Image'] != expected_image):
            raise DeployError("Cleanup stopped: unexpected container owns the service name")
        command(["docker", "rm", "-f", service])


def rollback_route(callback):
    try:
        if callback:
            callback()
    except BaseException:
        return False
    return True


def create_container(service, revision, image, target, payload, release, expected_image=None, persist=None, rollback=None, restored=None):
    if inspect_optional(service) is not None:
        raise DeployError("First deployment refused: container name is already in use")
    args = run_arguments(service, image, revision, target, release)
    try:
        command(args)
        count = wait_healthy(service, target, payload['environment'], False)
        deployed = verify_readback(service, expected_image, payload['environment'], target)
        record = {"service": service, "revision": revision, "image": image, "container": deployed['Id'],
                  "previous_container": None, "release": str(release), "target": target}
        if persist:
            persist(record)
    except BaseException:
        route_ok = rollback_route(rollback)
        remove_candidate(service, revision, expected_image)
        if not route_ok:
            raise DeployError("New container removed; Nginx rollback needs administrator attention") from None
        raise DeployError("First deployment failed; only this attempt's container was removed") from None
    print(json.dumps({"service": service, "revision": revision, "status": "created", "tools": count}), flush=True)
    return record


def replace_container(service, revision, image, target, payload, release, expected_image=None, persist=None, rollback=None, restored=None, current_contract=None):
    current = inspect(service)
    # The live container must still match the contract it was created with.
    # Recreate fields such as init are applied to the replacement, not required
    # of the container being replaced.
    validate_current(current, current_contract or target, service)
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
        deployed = verify_readback(service, expected_image, payload['environment'], target)
        record = {"service": service, "revision": revision, "image": image, "container": deployed["Id"],
                  "previous_container": backup, "release": str(release), "target": target}
        if persist:
            persist(record)
        print(json.dumps({"service": service, "revision": revision, "status": "deployed", "tools": count, "rollback_container": backup}), flush=True)
        return record
    except BaseException:
        route_ok = rollback_route(rollback)
        # Only remove a container created by this transaction. Original services/data
        # and historical backups are retained; their writable layers stay intact.
        if renamed:
            remove_candidate(service, revision, expected_image)
            command(["docker", "rename", backup, service])
            command(["docker", "update", "--restart=" + restart, service])
        if stopped:
            command(["docker", "start", service])
            wait_healthy(service, target, old_env, ready)
            if restored and route_ok:
                restored(old_env)
        if not route_ok:
            raise DeployError("Previous container restored; Nginx rollback needs administrator attention") from None
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
        # Port, network, and data-mount changes need an explicit migration.
        # init, command, shm, and additional secret files are recreated in place.
        old_target = normalize_target({'network': 'host' if service in installed else 'bridge', **old_target})
        if recreate_migration_blocked(old_target, target):
            raise DeployError("Existing infrastructure changed; an explicit migration is required")
        changes = [key for key in ('init', 'command', 'shm_size', 'secret_file_paths') if old_target[key] != target[key]]
        if changes:
            print(json.dumps({"service": service, "stage": "recreate", "changes": changes}), flush=True)
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
    return old_target if current else None


def deploy(service, revision, payload, target, installed=None):
    image = validate_request(service, revision, payload, target)
    target = allocate_target(service, target, installed or {})
    current = inspect_optional(service)
    current_contract = preflight(service, target, installed or {}, current)
    route = RouteTransaction(service, target, revision, legacy=service in (installed or {}))
    print(json.dumps({"service": service, "stage": "pulling", "revision": revision}), flush=True)
    expected_image = pull_image(image, revision, payload)
    base = ROOT / "releases" / service
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    release = Path(tempfile.mkdtemp(prefix=revision[:12] + "-", dir=base))
    previous = previous_state(service)
    if previous:
        secure_write(release / 'previous-state.json', json.dumps(previous))
    if route.previous is not None:
        secure_write(release / 'previous-route.conf', route.previous)
    secure_write(release / "runtime.env", "".join(k + "=" + v + "\n" for k, v in sorted(payload["environment"].items())))
    if payload["settings"] is not None:
        secure_write(release / "config.yaml", json.dumps(payload["settings"], ensure_ascii=False))
    for key, value in payload["files"].items():
        secure_write(release / key, value)
    state = ROOT / "state"
    state.mkdir(mode=0o700, exist_ok=True)
    def persist(record):
        record['public_url'] = route.publish(payload['environment'])
        record.update(route.metadata())
        secure_write(state / (service + '.json'), json.dumps(record, indent=2))
    def restored(env):
        if route.legacy or route.previous is not None:
            wait_public(service, env, target, None if route.legacy else previous['revision'])
    if current:
        record = replace_container(service, revision, image, target, payload, release, expected_image, persist, route.rollback, restored, current_contract)
    else:
        record = create_container(service, revision, image, target, payload, release, expected_image, persist, route.rollback, restored)
    print(json.dumps({'service': service, 'host_port': target['port'], 'container_port': target['container_port'],
                      'public_url': record['public_url'], 'verified': True}), flush=True)


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
