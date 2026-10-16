#!/usr/bin/env python3
"""Deploy service changes since the last successful push; shared CI is validation only."""
import json
import os
import subprocess
from catalog import ROOT, SERVICES, matrix_entry, validate_catalog


def select(paths, services=None, changed_targets=()):
    services = SERVICES if services is None else services
    return [s for s in services if s in changed_targets or any(p.startswith(s + '/') for p in paths)]


def changed_services(base, head, root=ROOT):
    paths = subprocess.check_output(['git', 'diff', '--name-only', base, head], cwd=root, text=True).splitlines()
    changed_targets = set()
    if 'deploy/targets.json' in paths:
        def targets(rev):
            p = subprocess.run(['git', 'show', rev + ':deploy/targets.json'], cwd=root, capture_output=True, text=True)
            return json.loads(p.stdout) if p.returncode == 0 else {}
        old, new = targets(base), targets(head)
        changed_targets = {s for s in old.keys() | new.keys() if old.get(s) != new.get(s)}
    return select(paths, changed_targets=changed_targets)


def main():
    validate_catalog()
    event = json.loads(open(os.environ['GITHUB_EVENT_PATH']).read())
    kind = os.environ['GITHUB_EVENT_NAME']
    if kind == 'workflow_dispatch':
        choice = event.get('inputs', {}).get('service', 'all').strip()
        if choice != 'all' and choice not in SERVICES:
            raise ValueError('Unknown service')
        services = list(SERVICES) if choice == 'all' else [choice]
    else:
        if kind == 'pull_request':
            base = event['pull_request']['base']['sha']
        else:
            # API or Git failures must not silently expand a deployment to every service.
            response = subprocess.check_output(['gh', 'api', 'repos/david-yang-pixel/mcp-plugins/actions/workflows/ci-cd.yml/runs?branch=main&event=push&status=success&per_page=1'], text=True)
            runs = json.loads(response).get('workflow_runs', [])
            base = runs[0]['head_sha'] if runs else event.get('before')
        services = changed_services(base, os.environ['GITHUB_SHA']) if base and set(base) != {'0'} else list(SERVICES)
    with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
        output.write('services=' + json.dumps([matrix_entry(s) for s in services]) + '\n')
        output.write('changed=' + ('true' if services else 'false') + '\n')
    print('Selected services:', ', '.join(services) or 'none (deployment checks only)')


if __name__ == '__main__':
    main()
