#!/usr/bin/env python3
"""Exercise real Docker first deployment, update, rollback and peer isolation in CI."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


def docker(*args):
    return subprocess.check_output(['docker', *args], stderr=subprocess.DEVNULL, text=True).strip()


def port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def main():
    prefix = 'mcp-ci-' + secrets.token_hex(4)
    image = prefix + ':fixture'
    docker('build', '-q', '-t', image, str(Path(__file__).parent / 'fixtures/mcp'))
    image_id = json.loads(docker('image', 'inspect', image))[0]['Id']
    key = secrets.token_urlsafe(24)
    revision = 'a' * 40
    targets = {}
    for suffix in ('peer', 'new', 'fail'):
        selected_port = port()
        while selected_port in {t['port'] for t in targets.values()}:
            selected_port = port()
        targets[prefix + '-' + suffix] = server.resolve_target(prefix + '-' + suffix, {
            'port': selected_port, 'auth_key': 'TEST_API_KEY', 'health_path': '/health',
            'env_keys': ['TEST_API_KEY', 'PORT', 'FAIL_HEALTH', 'VERSION'], 'secret_file_paths': {}}, {})
    real_validate = server.validate_request
    real_wait = server.wait_healthy

    def local_image(*args):
        real_validate(*args)
        return image  # Registry transport is tested by production CI; this fixture is local.

    def short_wait(service, target, env, ready):
        return real_wait(service, target, env, ready, timeout=6)

    def payload(target, fail='false', version='1'):
        return {'digest': 'sha256:' + 'b' * 64, 'target': target,
                'environment': {'TEST_API_KEY': key, 'PORT': str(target['port']), 'FAIL_HEALTH': fail, 'VERSION': version},
                'files': {}, 'settings': None, 'registry_user': 'fixture', 'registry_token': 'synthetic',
                'deployer_sha256': hashlib.sha256(Path(server.__file__).read_bytes()).hexdigest()}

    try:
        with tempfile.TemporaryDirectory(prefix=prefix) as directory, \
             patch.object(server, 'ROOT', Path(directory)), \
             patch.object(server, 'validate_request', side_effect=local_image), \
             patch.object(server, 'pull_image', return_value=image_id), \
             patch.object(server, 'wait_healthy', side_effect=short_wait):
            root = Path(directory)
            peer, new, fail = (prefix + '-' + suffix for suffix in ('peer', 'new', 'fail'))
            server.deploy(peer, revision, payload(targets[peer]), targets[peer])
            peer_id = server.inspect(peer)['Id']
            server.deploy(new, revision, payload(targets[new]), targets[new])
            state = json.loads((root / 'state' / (new + '.json')).read_text())
            assert state['previous_container'] is None
            assert (root / 'state' / (new + '.json')).stat().st_mode & 0o777 == 0o600
            original_id = state['container']
            try:
                server.deploy(fail, revision, payload(targets[fail], fail='true'), targets[fail])
            except server.DeployError:
                pass
            else:
                raise AssertionError('Unhealthy first deployment was accepted')
            assert server.inspect_optional(fail) is None
            assert not (root / 'state' / (fail + '.json')).exists()
            server.deploy(new, 'c' * 40, payload(targets[new], version='2'), targets[new])
            updated_id = server.inspect(new)['Id']
            assert updated_id != original_id
            saved_state = (root / 'state' / (new + '.json')).read_text()
            try:
                server.deploy(new, 'd' * 40, payload(targets[new], fail='true', version='bad'), targets[new])
            except server.DeployError:
                pass
            else:
                raise AssertionError('Unhealthy update was accepted')
            assert server.inspect(new)['Id'] == updated_id
            assert (root / 'state' / (new + '.json')).read_text() == saved_state
            assert server.inspect(peer)['Id'] == peer_id
            print('PASS: first deploy, failed-first cleanup, update, rollback, private files, and unchanged peer container')
    finally:
        ids = docker('container', 'ls', '--all', '--filter', 'name=^/' + prefix + '-', '--format', '{{.ID}}').splitlines()
        if ids:
            docker('rm', '-f', *ids)
        docker('image', 'rm', image)


if __name__ == '__main__':
    main()
