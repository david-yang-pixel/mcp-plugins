#!/usr/bin/env python3
"""Select changes since the last successful push, including failed/queued pushes."""
import json
import os
import subprocess
from catalog import SERVICES


def select(paths):
    if any(p.startswith(("deploy/", ".github/workflows/")) for p in paths):
        return list(SERVICES)
    return [s for s in SERVICES if any(p.startswith(s + "/") for p in paths)]


def main():
    event = json.loads(open(os.environ["GITHUB_EVENT_PATH"]).read())
    kind = os.environ["GITHUB_EVENT_NAME"]
    if kind == "workflow_dispatch":
        choice = event.get("inputs", {}).get("service", "all")
        if choice != "all" and choice not in SERVICES:
            raise ValueError("Unknown service")
        services = list(SERVICES) if choice == "all" else [choice]
    else:
        if kind == "pull_request":
            base = event["pull_request"]["base"]["sha"]
        else:
            p = subprocess.run(["gh", "api", "repos/david-yang-pixel/mcp-plugins/actions/workflows/ci-cd.yml/runs?branch=main&event=push&status=success&per_page=1"], capture_output=True, text=True)
            runs = json.loads(p.stdout).get("workflow_runs", []) if p.returncode == 0 else []
            base = runs[0]["head_sha"] if runs else None
        if not base:
            services = list(SERVICES)
        else:
            diff = subprocess.run(["git", "diff", "--name-only", base, os.environ["GITHUB_SHA"]], capture_output=True, text=True)
            services = select(diff.stdout.splitlines()) if diff.returncode == 0 else list(SERVICES)
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        output.write("services=" + json.dumps(services) + "\n")
        output.write("changed=" + ("true" if services else "false") + "\n")
    print("Selected services:", ", ".join(services) or "none")


if __name__ == "__main__":
    main()
