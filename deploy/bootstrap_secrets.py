#!/usr/bin/env python3
"""Import live environment values through SSH directly into GitHub Secrets.

Values stay in subprocess pipes/memory. Only non-secret configuration is saved.
The default is a preview. --apply authorizes the GitHub writes.
"""
import argparse
import json
import re
import subprocess
import sys
from catalog import ROOT, SERVICES, PORTS, AUTH_KEYS, secret_name

BASE_ENV = {"PATH", "GPG_KEY", "LANG", "PYTHONPATH", "PYTHON_SHA256",
            "PYTHON_VERSION", "PYTHONUNBUFFERED"}
SENSITIVE = re.compile(r"SECRET|TOKEN|PASSWORD|API_KEY|AUTHORIZATION|WEBHOOK_URL|EMAIL|TELEPORT_USER")
REMOTE = r'''
import json,subprocess,pathlib
names=SERVICES_PLACEHOLDER
out={}
for name in names:
 c=json.loads(subprocess.check_output(['docker','inspect',name]))[0]
 out[name]={'env':dict(x.split('=',1) for x in c['Config']['Env']),
            'mounts':c['Mounts'],'host':c['HostConfig'],'image':c['Image']}
out['db-gateway']['settings']=json.loads(subprocess.check_output([
 'docker','exec','db-gateway','/app/.venv/bin/python','-c',
 'import yaml,json;print(json.dumps(yaml.safe_load(open("/app/config.yaml"))))']))
p=pathlib.Path('/opt/dify-db/browser-relay-state/feishu_webhook_url')
if p.is_file() and p.read_text().strip():
 out['zendesk-browser-relay']['file_secret']=p.read_text().strip()
print(json.dumps(out))
'''.replace("SERVICES_PLACEHOLDER", repr(list(SERVICES)))


def run(args, **kw):
    p = subprocess.run(args, capture_output=True, **kw)
    if p.returncode:
        raise RuntimeError("Command failed: " + args[0] + " (details suppressed to protect credentials)")
    return p.stdout


def safe_config(value, path=""):
    if isinstance(value, dict):
        for key, child in value.items():
            if SENSITIVE.search(key.upper()) and child:
                raise ValueError("Sensitive value in configuration field: " + path + key)
            safe_config(child, path + key + ".")
    elif isinstance(value, str) and re.search(r"https?://[^/\s]+:[^/\s]+@|gh[pousr]_[A-Za-z0-9]+|sk-[A-Za-z0-9]{16}", value):
        raise ValueError("Credential-bearing configuration field: " + path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="zendesk-agent-server")
    parser.add_argument("--repo", default="david-yang-pixel/mcp-plugins")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--replace", action="store_true", help="Allow overwriting existing secret names")
    args = parser.parse_args()
    live = json.loads(run(["ssh", "-o", "BatchMode=yes", args.host, "python3 -"], input=REMOTE.encode()))
    existing = {x["name"] for x in json.loads(run(["gh", "secret", "list", "--repo", args.repo, "--json", "name"]))}
    plans, uploads, targets = {}, {}, {}
    for name in SERVICES:
        current = live[name]
        if current["host"]["NetworkMode"] != "host" or current["host"].get("Privileged"):
            raise ValueError("Unexpected runtime mode for " + name)
        env = {k: v for k, v in current["env"].items() if k not in BASE_ENV}
        config, secrets = {}, {}
        for key, value in sorted(env.items()):
            if SENSITIVE.search(key):
                label = secret_name(name, key)
                secrets[key] = {"name": label, "required": bool(value)}
                if value:
                    uploads[label] = value
            else:
                config[key] = value
        if name == "zendesk-browser-relay":
            # Chromium is baked into the new image; session/profile paths stay unchanged.
            config["PLAYWRIGHT_BROWSERS_PATH"] = "/ms-playwright"
        safe_config(config)
        files = {}
        if current.get("file_secret"):
            key = "FEISHU_NOTIFICATION_WEBHOOK_URL"
            label = secret_name(name, key)
            files[key] = {"name": label, "required": True,
                          "destination": "/var/lib/zendesk-browser-relay/state/feishu_webhook_url"}
            uploads[label] = current["file_secret"]
        settings = current.get("settings")
        if settings:
            safe_config(settings)
        plans[name] = {"environment": config, "secrets": secrets, "secret_files": files}
        if settings:
            plans[name]["settings"] = settings
        mounts = []
        for m in current["mounts"]:
            if m["Type"] != "bind":
                raise ValueError("Unexpected mount type for " + name)
            source = m["Source"]
            if name == "db-gateway" and m["Destination"] == "/app/.env":
                source = "@env"
            if name == "db-gateway" and m["Destination"] == "/app/config.yaml":
                source = "@settings"
            mounts.append({"source": source, "destination": m["Destination"], "readonly": not m["RW"]})
        targets[name] = {
            "port": PORTS[name], "auth_key": AUTH_KEYS[name],
            "health_path": "/healthz" if name == "db-gateway" else "/health",
            "init": bool(current["host"].get("Init")), "mounts": mounts,
            "command": ["uvicorn", "zendesk_browser_relay.app:app", "--host", "127.0.0.1", "--port", "8097"] if name == "zendesk-browser-relay" else [],
            "env_keys": sorted(set(config) | set(secrets)),
            "secret_file_paths": {k: v["destination"] for k, v in files.items()},
        }
        print(name + ": " + str(sum(bool(env[k]) for k in secrets)) + " environment secrets, " + str(len(files)) + " file secrets")
    overlap = set(uploads) & existing
    if overlap and not args.replace:
        raise ValueError("Existing secrets would be overwritten; use --replace only intentionally")
    print("Total nonempty service secrets:", len(uploads))
    if not args.apply:
        print("Preview only; no files or GitHub settings changed")
        return
    for label, value in uploads.items():
        run(["gh", "secret", "set", label, "--repo", args.repo], input=value.encode())
        print("Stored", label, flush=True)
    for name, plan in plans.items():
        folder = ROOT / name / "deploy"
        folder.mkdir(exist_ok=True)
        (folder / "runtime.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n")
    (ROOT / "deploy" / "targets.json").write_text(json.dumps(targets, indent=2) + "\n")
    names = {x["name"] for x in json.loads(run(["gh", "secret", "list", "--repo", args.repo, "--json", "name"]))}
    if not set(uploads) <= names:
        raise RuntimeError("Secret metadata readback incomplete")
    print("All secret names verified; values require a deployment round trip to verify")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(type(exc).__name__ + ": " + str(exc), file=sys.stderr)
        sys.exit(1)
