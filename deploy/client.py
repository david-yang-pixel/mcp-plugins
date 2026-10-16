#!/usr/bin/env python3
"""Send only the selected service's runtime secrets over verified SSH."""
import json
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from catalog import SERVICES, spec, target, secret_items
from server import wait_public, DeployError


def payload(service, digest, env=os.environ):
    plan = spec(service)
    # The workflow supplies only this matrix entry's secrets, never the whole context.
    env = dict(env)
    for i, (key, _) in enumerate(secret_items(service)):
        if 'MCP_SECRET_' + str(i) in env:
            env['SECRET_' + key] = env['MCP_SECRET_' + str(i)]
    values = dict(plan["environment"])
    files = {}
    for key, item in plan["secrets"].items():
        value = env.get("SECRET_" + key, "")
        if item["required"] and not value:
            raise ValueError("Missing GitHub Secret: " + item["name"])
        values[key] = value
    for key, item in plan["secret_files"].items():
        value = env.get("SECRET_" + key, "")
        if not value:
            raise ValueError("Missing GitHub Secret: " + item["name"])
        files[key] = value
    return {"digest": digest, "environment": values, "files": files, "target": target(service),
            "settings": plan.get("settings"), "registry_user": env["REGISTRY_USER"],
            "registry_token": env["REGISTRY_TOKEN"],
            "deployer_sha256": hashlib.sha256(Path(__file__).with_name("server.py").read_bytes()).hexdigest()}


def main():
    service = os.environ["SERVICE"]
    if service not in SERVICES:
        raise ValueError("Unknown service")
    revision = os.environ["REVISION"]
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Invalid revision")
    # Older queued workflows must not overwrite a newer main revision.
    head = subprocess.check_output(["gh", "api", "repos/david-yang-pixel/mcp-plugins/git/ref/heads/main", "--jq", ".object.sha"], text=True).strip()
    if head != revision:
        raise ValueError("Main advanced; this obsolete deployment is refused")
    image = f"ghcr.io/david-yang-pixel/mcp-plugins/{service}:{revision}"
    result = subprocess.run(["docker", "buildx", "imagetools", "inspect", image], capture_output=True, text=True)
    match = re.search(r"^Digest:\s+(sha256:[0-9a-f]{64})\s*$", result.stdout, re.M)
    if result.returncode or not match:
        raise ValueError("Could not resolve the built image digest")
    data = payload(service, match[1])
    with tempfile.TemporaryDirectory(prefix="mcp-ssh-") as folder:
        key = Path(folder) / "identity"
        known = Path(folder) / "known_hosts"
        key.write_text(os.environ["SSH_PRIVATE_KEY"].rstrip() + "\n")
        key.chmod(0o600)
        known.write_text(os.environ["SSH_KNOWN_HOSTS"].rstrip() + "\n")
        host = os.environ["DEPLOY_HOST"]
        if not re.fullmatch(r"[a-zA-Z0-9.-]+", host):
            raise ValueError("Invalid deployment host")
        args = ["ssh", "-T", "-i", str(key), "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=yes", "-o", "UserKnownHostsFile=" + str(known),
                "-o", "ConnectTimeout=20", "-o", "ServerAliveInterval=20", "root@" + host,
                "deploy", service, revision]
        result = subprocess.run(args, input=json.dumps(data).encode(), timeout=1500)
        if result.returncode:
            raise ValueError("Remote deployment did not pass verification")
    try:
        url = wait_public(service, data['environment'], data['target'])
    except DeployError:
        raise ValueError('Server completed deployment; external HTTPS verification failed and needs investigation') from None
    print('Verified public MCP URL:', url)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as summary:
            summary.write(f'### {service}\n\nVerified MCP endpoint: [{url}]({url})\n')


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Never render subprocess command arguments or the payload on failure.
        print(str(exc) if isinstance(exc, ValueError) else "Deployment client failed", file=sys.stderr)
        sys.exit(1)
