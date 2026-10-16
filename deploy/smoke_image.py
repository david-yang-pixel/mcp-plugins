#!/usr/bin/env python3
"""Start an image with disposable credentials and verify MCP authentication."""
import json
import secrets
import subprocess
import sys
import time
from catalog import SERVICES, spec, target as service_target
from server import verify_service


def main():
    service, image = sys.argv[1:]
    if service not in SERVICES:
        raise ValueError("Unknown service")
    name = "mcp-ci-" + service
    key = secrets.token_urlsafe(32)
    target = service_target(service)
    env = dict(spec(service).get('smoke_environment', {}))
    env[target['auth_key']] = key
    if service == "zendesk-browser-relay":
        env["ZENDESK_SESSION_SUPERVISOR_ENABLED"] = "false"
    args = ["docker", "run", "-d", "--name", name, "--network", target['network']]
    if target['network'] == 'bridge':
        args += ['--publish', f"127.0.0.1:{target['port']}:{target['port']}"]
    for k, v in env.items():
        args += ["-e", k + "=" + v]
    subprocess.run(args + [image], check=True, capture_output=True)
    try:
        for _ in range(45):
            try:
                count = verify_service(target, env)
                print(service + ": health, authenticated tools/list (" + str(count) + "), and invalid-key rejection passed")
                return
            except Exception:
                time.sleep(1)
        # This container contains only synthetic credentials, no production config.
        subprocess.run(["docker", "logs", "--tail", "60", name])
        raise RuntimeError("Image smoke test failed")
    finally:
        subprocess.run(["docker", "rm", "-f", name], check=True, capture_output=True)


if __name__ == "__main__":
    main()
