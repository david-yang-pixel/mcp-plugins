"""Discover service-owned deployment contracts without a central registry."""
from pathlib import Path
import json
import re
from server import normalize_target, resolve_target, validate_service_name

ROOT = Path(__file__).resolve().parents[1]
MAX_SECRETS = 32


def discover(root=ROOT):
    names = []
    for path in sorted(root.glob('*/deploy/runtime.json')):
        name = path.parent.parent.name
        validate_service_name(name)
        if path.is_symlink() or path.parent.is_symlink() or path.parent.parent.is_symlink():
            raise ValueError('Service configuration must not be a symlink')
        names.append(name)
    return tuple(names)


def secret_name(service, key):
    return 'MCP_' + service.upper().replace('-', '_') + '__' + key


def spec(service, root=ROOT):
    if service not in discover(root):
        raise ValueError('Unknown service')
    plan = json.loads((root / service / 'deploy/runtime.json').read_text())
    if set(plan) - {'environment', 'secrets', 'secret_files', 'settings', 'deployment', 'smoke_environment'}:
        raise ValueError('Unknown service configuration field')
    for field in ('environment', 'secrets', 'secret_files'):
        if not isinstance(plan.get(field), dict):
            raise ValueError('Missing service configuration: ' + field)
    if set(plan['environment']) & set(plan['secrets']) or set(plan['secrets']) & set(plan['secret_files']):
        raise ValueError('Duplicate environment or secret key')
    for key, value in plan['environment'].items():
        if not re.fullmatch(r'[A-Z][A-Z0-9_]*', key) or not isinstance(value, str) or any(c in value for c in '\r\n\0'):
            raise ValueError('Invalid environment entry')
    items = plan['secrets'] | plan['secret_files']
    if len(items) > MAX_SECRETS:
        raise ValueError('At most 32 secret values per service are supported')
    for key, item in items.items():
        if not re.fullmatch(r'[A-Z][A-Z0-9_]*', key) or item.get('name') != secret_name(service, key):
            raise ValueError('Secret must use this service and variable prefix')
        if not isinstance(item.get('required'), bool):
            raise ValueError('Secret required must be a boolean')
    smoke = plan.get('smoke_environment', {})
    if not isinstance(smoke, dict) or any(not re.fullmatch(r'[A-Z][A-Z0-9_]*', k) or not isinstance(v, str) or any(c in v for c in '\r\n\0') for k, v in smoke.items()):
        raise ValueError('Invalid smoke environment')
    return plan


def target(service, root=ROOT):
    plan = spec(service, root)
    legacy_path = root / 'deploy/targets.json'
    legacy = json.loads(legacy_path.read_text()) if legacy_path.exists() else {}
    if 'deployment' in plan:
        result = dict(plan['deployment'])
        if set(result) & {'env_keys', 'secret_file_paths'}:
            raise ValueError('Environment and file keys are derived from the service mapping')
    elif service in legacy:
        result = dict(legacy[service])
    else:
        raise ValueError('New service needs deployment configuration: ' + service)
    result['env_keys'] = sorted(set(plan['environment']) | set(plan['secrets']))
    result['secret_file_paths'] = {k: v['destination'] for k, v in plan['secret_files'].items()}
    # Original services keep their administrator-installed host-network contracts.
    result.setdefault('network', 'host' if service in legacy else 'bridge')
    result = normalize_target(result)
    if result['auth_key'] not in plan['secrets'] or not plan['secrets'][result['auth_key']]['required']:
        raise ValueError('MCP authentication must be a required GitHub Secret')
    return resolve_target(service, result, legacy)


def secret_items(service, root=ROOT):
    plan = spec(service, root)
    return sorted((plan['secrets'] | plan['secret_files']).items())


def matrix_entry(service):
    names = [item['name'] for _, item in secret_items(service)]
    return {'service': service, 'secret_names': names + [''] * (MAX_SECRETS - len(names))}


def validate_catalog(root=ROOT):
    ports = {}
    for service in discover(root):
        contract = target(service, root)
        port = contract['port']
        if port == 'auto':
            pass
        elif port in ports:
            raise ValueError('Duplicate service port: ' + service + ' and ' + ports[port])
        else:
            ports[port] = service
        for name in ('Dockerfile', 'pyproject.toml', 'uv.lock', 'requirements.runtime.txt'):
            if not (root / service / name).is_file():
                raise ValueError('Missing build input: ' + service + '/' + name)
    return ports


SERVICES = discover()
# Compatibility for the one-off original inventory/import script.
PORTS = {s: target(s)['port'] for s in SERVICES}
AUTH_KEYS = {s: target(s)['auth_key'] for s in SERVICES}
