#!/usr/bin/env python3
"""Exercise real Docker, automatic ports, TLS Nginx routes and transactional rollback."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


def docker(*args):
    return subprocess.check_output(['docker', *args], stderr=subprocess.DEVNULL, text=True).strip()


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def upgrade_from_base_container(prefix, image, key):
    """Replace a pre-change container: init off, default shm, one secret, old command."""
    service = prefix + '-base'
    host_port = free_port()
    env_keys = ['FAIL_HEALTH', 'FAIL_PUBLIC', 'PORT', 'TEST_API_KEY', 'VERSION']
    environment = {
        'TEST_API_KEY': key, 'PORT': '8000', 'FAIL_HEALTH': 'false',
        'FAIL_PUBLIC': 'false', 'VERSION': 'base',
    }
    old_target = server.normalize_target({
        'port': host_port, 'container_port': 8000, 'auth_key': 'TEST_API_KEY',
        'health_path': '/health', 'init': False, 'network': 'bridge', 'mounts': [],
        'command': ['uvicorn', 'zendesk_browser_relay.app:app', '--host', '127.0.0.1', '--port', '8097'],
        'env_keys': env_keys,
        'secret_file_paths': {
            'FEISHU_NOTIFICATION_WEBHOOK_URL': '/var/lib/zendesk-browser-relay/state/feishu_webhook_url',
        },
    })
    new_target = server.normalize_target({
        **old_target,
        'init': True,
        'shm_size': '256m',
        'command': ['python', '-B', 'app.py'],
        'secret_file_paths': {
            **old_target['secret_file_paths'],
            'ZENDESK_MCP_AUTHORIZATION': '/run/mcp-secrets/zendesk_mcp_authorization',
        },
    })
    release = server.ROOT / 'releases' / service / 'base'
    release.mkdir(parents=True)
    secret = release / 'FEISHU_NOTIFICATION_WEBHOOK_URL'
    secret.write_text('https://example.invalid/hook')
    env_file = release / 'runtime.env'
    env_file.write_text(''.join(k + '=' + v + '\n' for k, v in sorted(environment.items())))
    server.command([
        'docker', 'run', '-d', '--name', service, '--network', 'bridge', '--restart', 'always',
        '--label', 'io.mcp-plugins.managed=true', '--label', 'io.mcp-plugins.revision=' + ('b' * 40),
        '--env-file', str(env_file), '--publish', f'127.0.0.1:{host_port}:8000',
        '--cap-drop=ALL', '--security-opt=no-new-privileges:true',
        '--mount', f'type=bind,source={secret},target=/var/lib/zendesk-browser-relay/state/feishu_webhook_url,readonly',
        image, 'python', '-B', 'app.py',
    ])
    deadline = time.monotonic() + 10
    while True:
        try:
            server.verify_service(old_target, environment, require_ready=False)
            break
        except (OSError, ValueError, server.DeployError):
            if time.monotonic() > deadline:
                raise
            time.sleep(0.2)
    current = server.inspect(service)
    assert current['HostConfig'].get('Init') in (None, False)
    assert current['HostConfig']['ShmSize'] == 64 * 1024 * 1024
    state_dir = server.ROOT / 'state'
    state_dir.mkdir(mode=0o700, exist_ok=True)
    (state_dir / (service + '.json')).write_text(json.dumps({
        'service': service, 'revision': 'b' * 40, 'image': image, 'container': current['Id'],
        'previous_container': None, 'release': str(release), 'target': old_target,
    }))
    try:
        server.validate_current(current, new_target, service)
    except server.DeployError:
        pass
    else:
        raise AssertionError('base container unexpectedly matched the new init/shm contract')
    server.validate_current(current, old_target, service)
    body = {
        'digest': 'sha256:' + 'c' * 64, 'target': new_target, 'environment': environment,
        'files': {
            'FEISHU_NOTIFICATION_WEBHOOK_URL': 'https://example.invalid/hook',
            'ZENDESK_MCP_AUTHORIZATION': 'synthetic-zendesk-token',
        },
        'settings': None, 'registry_user': 'fixture', 'registry_token': 'synthetic',
        'deployer_sha256': hashlib.sha256(Path(server.__file__).read_bytes()).hexdigest(),
    }
    server.deploy(service, 'e' * 40, body, new_target)
    upgraded = server.inspect(service)
    assert upgraded['Id'] != current['Id']
    assert upgraded['HostConfig']['Init'] is True
    assert upgraded['HostConfig']['ShmSize'] == 256 * 1024 * 1024
    assert upgraded['Config']['Cmd'] == ['python', '-B', 'app.py']
    destinations = {item['Destination'] for item in upgraded['Mounts']}
    assert '/var/lib/zendesk-browser-relay/state/feishu_webhook_url' in destinations
    assert '/run/mcp-secrets/zendesk_mcp_authorization' in destinations
    server.validate_current(upgraded, new_target, service)
    drifted = json.loads(json.dumps(upgraded))
    drifted['HostConfig']['ShmSize'] = 64 * 1024 * 1024
    try:
        server.validate_current(drifted, new_target, service)
    except server.DeployError as exc:
        assert 'shared memory' in str(exc)
    else:
        raise AssertionError('shared memory drift was accepted')


def main():
    prefix = 'mcp-ci-' + secrets.token_hex(4)
    image = prefix + ':fixture'
    nginx = shutil.which('nginx')
    if not nginx:
        raise RuntimeError('Install nginx to run the isolated proxy integration test')
    docker('build', '-q', '-t', image, str(Path(__file__).parent / 'fixtures/mcp'))
    image_id = json.loads(docker('image', 'inspect', image))[0]['Id']
    key = secrets.token_urlsafe(24)
    revision = 'a' * 40
    target = server.resolve_target(prefix + '-new', {
        'container_port': 8000, 'auth_key': 'TEST_API_KEY', 'health_path': '/health',
        'env_keys': ['TEST_API_KEY', 'PORT', 'FAIL_HEALTH', 'FAIL_PUBLIC', 'VERSION'], 'secret_file_paths': {}}, {})
    real_validate, real_wait, real_public = server.validate_request, server.wait_healthy, server.wait_public

    def local_image(*args):
        real_validate(*args)
        return image  # Registry transport uses the existing production CI path.

    def payload(fail='false', version='1', public='false'):
        return {'digest': 'sha256:' + 'b' * 64, 'target': target,
                'environment': {'TEST_API_KEY': key, 'PORT': '8000', 'FAIL_HEALTH': fail, 'FAIL_PUBLIC': public, 'VERSION': version},
                'files': {}, 'settings': None, 'registry_user': 'fixture', 'registry_token': 'synthetic',
                'deployer_sha256': hashlib.sha256(Path(server.__file__).read_bytes()).hexdigest()}

    process = None
    try:
        with tempfile.TemporaryDirectory(prefix=prefix) as directory:
            root = Path(directory)
            root.chmod(0o711)
            routes = root / 'routes';routes.mkdir()
            config = root / 'nginx.conf'
            cert = root / 'cert.pem';private = root / 'key.pem'
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                '-keyout', str(private), '-out', str(cert), '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost'],
                check=True, capture_output=True)
            https_port = free_port()
            config.write_text(f'''pid {root}/nginx.pid;
error_log {root}/nginx-error.log;
events {{ worker_connections 32; }}
http {{
 access_log off;
 client_body_temp_path {root}/client_temp;
 proxy_temp_path {root}/proxy_temp;
 server {{
  listen 127.0.0.1:{https_port} ssl;
  server_name localhost;
  ssl_certificate {cert};
  ssl_certificate_key {private};
  location /existing/ {{ return 200 "unchanged"; }}
  location / {{ return 404; }}
 }}
}}
''')
            def nginx_command(*args):
                return server.command([nginx, '-p', str(root), '-c', str(config), *args])
            with patch.object(server, 'ROOT', root / 'deploy'), \
                 patch.object(server, 'NGINX_ROUTES', routes), patch.object(server, 'NGINX_SITE', config), \
                 patch.object(server, 'PUBLIC_ORIGIN', f'https://localhost:{https_port}'), \
                 patch.object(server, 'validate_request', side_effect=local_image), \
                 patch.object(server, 'pull_image', return_value=image_id), \
                 patch.object(server, 'nginx_command', side_effect=nginx_command), \
                 patch.object(server, 'reload_nginx', side_effect=lambda: nginx_command('-s', 'reload')), \
                 patch.object(server, 'wait_healthy', side_effect=lambda s, t, e, r: real_wait(s, t, e, r, timeout=6)), \
                 patch.object(server, 'wait_public', side_effect=lambda s, e, t, r=None: real_public(s, e, t, r, timeout=6)), \
                 patch.dict(os.environ, {'SSL_CERT_FILE': str(cert)}):
                config.write_text(server.nginx_include_content(config.read_text()))
                nginx_command('-t')
                process = subprocess.Popen([nginx, '-p', str(root), '-c', str(config), '-g', 'daemon off;'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                nginx_pid = str(process.pid)
                for _ in range(50):
                    if (root / 'nginx.pid').exists() and (root / 'nginx.pid').read_text().strip() == nginx_pid: break
                    if process.poll() is not None: raise RuntimeError('Isolated Nginx failed to start')
                    time.sleep(0.1)
                else:
                    raise RuntimeError('Isolated Nginx PID was not ready')
                site_before = config.read_bytes()
                peer, new, fail, public_fail = (prefix + '-' + suffix for suffix in ('peer', 'new', 'fail', 'public-fail'))
                def state(service):
                    return json.loads((server.ROOT / 'state' / (service + '.json')).read_text())
                def deploy(service, rev=revision, **kwargs):
                    server.deploy(service, rev, payload(**kwargs), target)
                def reject(service, rev=revision, **kwargs):
                    try: deploy(service, rev, **kwargs)
                    except server.DeployError: return
                    raise AssertionError('Invalid deployment was accepted')

                deploy(peer)
                peer_id = server.inspect(peer)['Id']
                peer_route = (routes / (peer + '.conf')).read_bytes()
                deploy(new)
                initial = state(new)
                assert initial['previous_container'] is None
                assert initial['target']['port'] != state(peer)['target']['port']
                assert initial['target']['container_port'] == 8000
                assert initial['public_url'].endswith('/' + new + '/mcp')
                assert (server.ROOT / 'state' / (new + '.json')).stat().st_mode & 0o777 == 0o600
                reject(fail, fail='true')
                reject(public_fail, public='true')
                for name in (fail, public_fail):
                    assert server.inspect_optional(name) is None
                    assert not (server.ROOT / 'state' / (name + '.json')).exists()
                    assert not (routes / (name + '.conf')).exists()
                deploy(new, 'c' * 40, version='2')
                updated = state(new)
                assert updated['container'] != initial['container']
                assert updated['target']['port'] == initial['target']['port']
                saved_state = (server.ROOT / 'state' / (new + '.json')).read_bytes()
                saved_route = (routes / (new + '.conf')).read_bytes()
                reject(new, 'd' * 40, public='true', version='bad')
                assert server.inspect(new)['Id'] == updated['container']
                assert (server.ROOT / 'state' / (new + '.json')).read_bytes() == saved_state
                assert (routes / (new + '.conf')).read_bytes() == saved_route
                assert server.inspect(peer)['Id'] == peer_id
                assert (routes / (peer + '.conf')).read_bytes() == peer_route
                assert (root / 'nginx.pid').read_text().strip() == nginx_pid
                assert config.read_bytes() == site_before
                real_public(peer, payload()['environment'], state(peer)['target'], revision, timeout=6)
                peer_before_upgrade = server.inspect(peer)['Id']
                upgrade_from_base_container(prefix, image, key)
                assert server.inspect(peer)['Id'] == peer_before_upgrade
                assert (root / 'nginx.pid').read_text().strip() == nginx_pid
                print('PASS: automatic distinct/stable ports, real TLS routes, bad-key rejection, first-deploy cleanup, public-failure rollback, unchanged peer and graceful Nginx reload, base-container recreate')
                nginx_command('-s', 'quit')
                process.wait(timeout=15)
                process = None
    finally:
        if process and process.poll() is None:
            process.terminate()
            try: process.wait(timeout=10)
            except subprocess.TimeoutExpired: process.kill();process.wait()
        ids = docker('container', 'ls', '--all', '--filter', 'name=^/' + prefix + '-', '--format', '{{.ID}}').splitlines()
        if ids: docker('rm', '-f', *ids)
        docker('image', 'rm', image)


if __name__ == '__main__':
    main()
