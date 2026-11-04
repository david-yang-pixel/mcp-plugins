"""Declared public paths, OAuth metadata routes, fixed hostnames and managed data ownership."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server
import catalog

REVISION = 'a' * 40
OAUTH = {'public_paths': ['/authorize', '/token', '/register', '/revoke', '/login/'],
         'oauth_metadata': True, 'max_body_size': '40m', 'hostname': 'difyctl-mcp'}


def target(**extra):
    return server.normalize_target({'container_port': 8000, 'auth_key': 'TEST_API_KEY',
        'health_path': '/health', 'env_keys': ['TEST_API_KEY'], 'secret_file_paths': {}, **extra})


ORIGINAL_ROUTE = '''# Managed by mcp-plugins-deploy; service=example-mcp
location = /example-mcp/mcp {
    proxy_pass http://127.0.0.1:28191/mcp;
    proxy_http_version 1.1;
    proxy_set_header Connection "";
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-MCP-Endpoint /example-mcp/mcp;
    proxy_buffering off;
    proxy_request_buffering off;
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;
    add_header X-MCP-Deployment aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa always;
}
'''


class ContractTests(unittest.TestCase):
    def test_defaults_keep_existing_contracts_unchanged(self):
        plain = target()
        self.assertEqual((plain['hostname'], plain['public_paths'], plain['oauth_metadata'], plain['max_body_size']),
                         (None, [], False, None))
        self.assertEqual(server.normalize_target(plain), plain)

    def test_valid_oauth_contract(self):
        resolved = server.resolve_target('difyctl-mcp', target(**OAUTH), {})
        self.assertEqual(resolved['public_paths'], OAUTH['public_paths'])
        self.assertTrue(resolved['oauth_metadata'])

    def test_invalid_public_paths_are_rejected(self):
        for paths in (['/mcp'], ['/mcp/'], ['/health'], ['/'], ['authorize'], ['/../etc'], ['/a//b'],
                      ['/Login/'], ['/a;b'], ['/a b'], ['/token', '/token'], ['/x' + str(i) for i in range(17)], '/token'):
            with self.subTest(paths=paths), self.assertRaises(server.DeployError):
                target(public_paths=paths)

    def test_health_parent_is_not_publishable(self):
        with self.assertRaises(server.DeployError):
            target(health_path='/internal/health', public_paths=['/internal/'])

    def test_invalid_hostname_body_size_and_oauth_flag(self):
        for extra in ({'hostname': 'Bad_Host'}, {'hostname': 'a' * 64}, {'hostname': 7}, {'max_body_size': '0m'},
                      {'max_body_size': '10g'}, {'max_body_size': '10m;'}, {'oauth_metadata': 'yes'}):
            with self.subTest(extra=extra), self.assertRaises(server.DeployError):
                target(**extra)

    def test_legacy_services_cannot_add_public_infrastructure(self):
        installed = {'github-mcp': {'port': 8111, 'auth_key': 'TEST_API_KEY', 'health_path': '/health',
                                    'env_keys': ['TEST_API_KEY'], 'secret_file_paths': {}}}
        declared = {**installed['github-mcp'], 'network': 'host'}
        server.resolve_target('github-mcp', declared, installed)
        for extra in ({'hostname': 'github-mcp'}, {'public_paths': ['/authorize']}, {'oauth_metadata': True}):
            with self.subTest(extra=extra), self.assertRaisesRegex(server.DeployError, 'administrator'):
                server.resolve_target('github-mcp', {**declared, **extra}, installed)

    def test_service_catalog_accepts_declared_routes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / 'oauth-mcp' / 'deploy'
            folder.mkdir(parents=True)
            plan = {'deployment': {'container_port': 8000, 'auth_key': 'TEST_API_KEY', 'health_path': '/health', **OAUTH},
                    'environment': {}, 'secrets': {'TEST_API_KEY': {'name': catalog.secret_name('oauth-mcp', 'TEST_API_KEY'), 'required': True}},
                    'secret_files': {}}
            (folder / 'runtime.json').write_text(json.dumps(plan))
            self.assertEqual(catalog.target('oauth-mcp', root)['public_paths'], OAUTH['public_paths'])


class RenderTests(unittest.TestCase):
    def test_default_route_is_byte_identical_to_previous_release(self):
        self.assertEqual(server.render_route('example-mcp', 28191, REVISION), ORIGINAL_ROUTE)
        self.assertEqual(server.render_route('example-mcp', 28191, REVISION, target()), ORIGINAL_ROUTE)

    def test_declared_paths_render_exact_and_prefix_locations(self):
        content = server.render_route('difyctl-mcp', 28191, REVISION, target(**OAUTH))
        nodes = server.parse_nginx(content)
        locations = [(n['args'][0], n['args'][1], next(c['args'][0] for c in n['children'] if c['name'] == 'proxy_pass')) for n in nodes]
        self.assertEqual(locations, [
            ('=', '/difyctl-mcp/mcp', 'http://127.0.0.1:28191/mcp'),
            ('=', '/difyctl-mcp/authorize', 'http://127.0.0.1:28191/authorize'),
            ('=', '/difyctl-mcp/token', 'http://127.0.0.1:28191/token'),
            ('=', '/difyctl-mcp/register', 'http://127.0.0.1:28191/register'),
            ('=', '/difyctl-mcp/revoke', 'http://127.0.0.1:28191/revoke'),
            ('^~', '/difyctl-mcp/login/', 'http://127.0.0.1:28191/login/'),
            ('=', '/.well-known/oauth-protected-resource/difyctl-mcp/mcp',
             'http://127.0.0.1:28191/.well-known/oauth-protected-resource/difyctl-mcp/mcp'),
            ('=', '/.well-known/oauth-authorization-server/difyctl-mcp',
             'http://127.0.0.1:28191/.well-known/oauth-authorization-server/difyctl-mcp'),
        ])
        for node in nodes:
            headers = [c['args'] for c in node['children'] if c['name'] == 'add_header']
            self.assertIn(['X-MCP-Deployment', REVISION, 'always'], headers)
            body = [c['args'] for c in node['children'] if c['name'] == 'client_max_body_size']
            self.assertEqual(body, [['40m']] if node['args'][1] == '/difyctl-mcp/mcp' else [])
        self.assertNotIn('/health', content)
        self.assertNotIn('location /difyctl-mcp/ ', content)

    def test_render_refuses_unvalidated_values(self):
        for extra in ({'public_paths': ['/x;include /etc/passwd']}, {'max_body_size': '1m; return 200'}):
            with self.subTest(extra=extra), self.assertRaises(server.DeployError):
                server.render_route('difyctl-mcp', 28191, REVISION, extra)


class NamespaceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.routes = self.root / 'routes'
        self.routes.mkdir()
        self.site = self.root / 'site.conf'
        for key, value in [('ROOT', self.root), ('NGINX_ROUTES', self.routes), ('NGINX_SITE', self.site)]:
            patcher = patch.object(server, key, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        site = server.nginx_include_content('''server {
 listen 443 ssl;
 server_name zendesk.smlershou.top;
 location ^~ /.well-known/acme-challenge/ { root /var/www/html; }
 include /etc/nginx/snippets/extra.conf;
 location / { return 404; }
}
''')
        self.documents = {str(self.site): server.parse_nginx(site, str(self.site))}

    def check(self, extra_config=None, contract=None):
        if extra_config is not None:
            name = '/etc/nginx/snippets/extra.conf'
            self.documents[name] = server.parse_nginx(extra_config, name)
        with patch.object(server, 'nginx_documents', return_value=self.documents):
            server.check_route_namespace('difyctl-mcp', self.routes / 'difyctl-mcp.conf', contract or target(**OAUTH))

    def test_unrelated_acme_route_is_allowed(self):
        self.check()

    def test_existing_well_known_routes_are_not_adopted(self):
        for config in ('location /.well-known/ { return 404; }',
                       'location = /.well-known/oauth-authorization-server/difyctl-mcp { return 404; }',
                       'location /.well-known/oauth-protected-resource/difyctl-mcp/ { return 404; }',
                       'location ~ /\\. { deny all; }'):
            with self.subTest(config=config), self.assertRaisesRegex(server.DeployError, 'conflicts'):
                self.check(config)

    def test_declared_paths_stay_inside_the_reserved_namespace(self):
        with self.assertRaisesRegex(server.DeployError, 'conflicts'):
            self.check('location /difyctl-mcp/login/ { return 404; }')
        with self.assertRaisesRegex(server.DeployError, 'conflicts'):
            self.check('location /difyctl { return 404; }')

    def test_other_service_well_known_paths_do_not_conflict(self):
        self.check('location = /.well-known/oauth-authorization-server/other-mcp { return 404; }')

    def test_transaction_rewrites_and_rolls_back_the_whole_route_file(self):
        contract = target(port=28191, **OAUTH)
        old = server.render_route('difyctl-mcp', 28191, 'b' * 40, contract)
        path = self.routes / 'difyctl-mcp.conf'
        path.write_text(old)
        (self.root / 'state').mkdir()
        (self.root / 'state/difyctl-mcp.json').write_text(json.dumps({'route_sha256': hashlib.sha256(old.encode()).hexdigest()}))
        with patch.object(server, 'nginx_documents', return_value=self.documents):
            route = server.RouteTransaction('difyctl-mcp', contract, REVISION)
        with patch.object(server, 'check_route_namespace') as check, patch.object(server, 'nginx_command'), \
                patch.object(server, 'reload_nginx'), patch.object(server, 'wait_public', side_effect=server.DeployError('HTTPS failed')):
            with self.assertRaises(server.DeployError):
                route.publish({'TEST_API_KEY': 'synthetic'})
            self.assertEqual(check.call_args.args[2], contract)
            self.assertIn('/.well-known/oauth-authorization-server/difyctl-mcp', path.read_text())
            route.rollback()
        self.assertEqual(path.read_text(), old)


class PublicVerificationTests(unittest.TestCase):
    def metadata(self, issuer=server.PUBLIC_ORIGIN + '/difyctl-mcp', header=REVISION):
        protected = {'resource': server.PUBLIC_ORIGIN + '/difyctl-mcp/mcp', 'authorization_servers': [issuer]}
        authorization = {'issuer': issuer, 'authorization_endpoint': issuer + '/authorize', 'token_endpoint': issuer + '/token'}
        return [(protected, header), (authorization, header)]

    def test_oauth_metadata_is_checked_after_tool_discovery(self):
        unauthorized = urllib.error.HTTPError('https://example.invalid', 401, 'Unauthorized', {}, None)
        response = ({'result': {'tools': [{'name': 'difyctl_status'}]}}, REVISION)
        with patch.object(server, 'public_request', side_effect=[response, unauthorized]), \
                patch.object(server, 'public_metadata', side_effect=self.metadata()) as metadata:
            server.verify_public('difyctl-mcp', {'TEST_API_KEY': 'synthetic'}, target(**OAUTH), REVISION)
        self.assertEqual([c.args[0] for c in metadata.call_args_list], [
            '/.well-known/oauth-protected-resource/difyctl-mcp/mcp', '/.well-known/oauth-authorization-server/difyctl-mcp'])

    def test_foreign_issuer_or_stale_route_fails(self):
        for metadata in (self.metadata(issuer='https://evil.example/difyctl-mcp'), self.metadata(header='b' * 40)):
            with self.subTest(), patch.object(server, 'public_metadata', side_effect=metadata):
                with self.assertRaisesRegex(server.DeployError, 'OAuth discovery'):
                    server.verify_oauth_metadata('difyctl-mcp', REVISION)

    def test_services_without_oauth_skip_metadata(self):
        unauthorized = urllib.error.HTTPError('https://example.invalid', 401, 'Unauthorized', {}, None)
        response = ({'result': {'tools': [{'name': 'example'}]}}, REVISION)
        with patch.object(server, 'public_request', side_effect=[response, unauthorized]), \
                patch.object(server, 'public_metadata', side_effect=AssertionError('not expected')):
            server.verify_public('example-mcp', {'TEST_API_KEY': 'synthetic'}, target(), REVISION)


class ContainerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        patcher = patch.object(server, 'ROOT', self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def container(self, hostname):
        return {'Id': 'id', 'Image': 'image', 'Config': {'Hostname': hostname, 'Env': ['TEST_API_KEY=x'], 'User': '10001'},
                'HostConfig': {'NetworkMode': 'bridge', 'Init': False, 'ShmSize': 64 * 1024 * 1024,
                               'PortBindings': {'8000/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '28191'}]}},
                'Mounts': []}

    def test_fixed_hostname_is_applied_and_checked(self):
        contract = target(port=28191, hostname='difyctl-mcp')
        args = server.run_arguments('difyctl-mcp', 'image', REVISION, contract, self.root)
        self.assertEqual(args[args.index('--hostname') + 1], 'difyctl-mcp')
        self.assertNotIn('--hostname', server.run_arguments('example-mcp', 'image', REVISION, target(port=28191), self.root))
        server.validate_current(self.container('difyctl-mcp'), contract, 'difyctl-mcp')
        with self.assertRaisesRegex(server.DeployError, 'hostname'):
            server.validate_current(self.container('3f2a1b'), contract, 'difyctl-mcp')
        server.validate_current(self.container('3f2a1b'), target(port=28191), 'difyctl-mcp')
        with patch.object(server, 'inspect', return_value=self.container('3f2a1b')):
            with self.assertRaisesRegex(server.DeployError, 'process configuration'):
                server.verify_readback('difyctl-mcp', 'image', {'TEST_API_KEY': 'x'}, contract)

    def test_hostname_change_recreates_without_migration(self):
        self.assertFalse(server.recreate_migration_blocked(target(port=28191), target(port=28191, hostname='difyctl-mcp')))
        self.assertTrue(server.recreate_migration_blocked(target(port=28191), target(port=28192, hostname='difyctl-mcp')))

    def test_image_owner(self):
        for user, owner in (('', None), ('0', None), ('root', None), ('10001', (10001, 10001)), ('10001:20002', (10001, 20002))):
            with self.subTest(user=user):
                self.assertEqual(server.image_owner({'Config': {'User': user}}), owner)
        for user in ('mcp', '10001:mcp', '-1'):
            with self.subTest(user=user), self.assertRaises(server.DeployError):
                server.image_owner({'Config': {'User': user}})

    def test_missing_data_directories_are_created_for_the_image_user_only_once(self):
        contract = target(mounts=[{'source': '@data/state', 'destination': '/var/lib/mcp/state', 'readonly': False}])
        with patch.object(server.os, 'chown') as chown:
            server.prepare_data_dirs('difyctl-mcp', contract, (10001, 10001))
            path = self.root / 'data/difyctl-mcp/state'
            self.assertTrue(path.is_dir())
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)
            chown.assert_called_once_with(path, 10001, 10001)
            (path / 'auth.db').write_text('kept')
            server.prepare_data_dirs('difyctl-mcp', contract, (10001, 10001))
            chown.assert_called_once()
        self.assertEqual((path / 'auth.db').read_text(), 'kept')

    def test_root_images_and_symlinked_data_directories(self):
        contract = target(mounts=[{'source': '@data/state', 'destination': '/var/lib/mcp/state', 'readonly': False}])
        with patch.object(server.os, 'chown') as chown:
            server.prepare_data_dirs('root-mcp', contract, None)
        chown.assert_not_called()
        (self.root / 'data/link-mcp').mkdir(parents=True)
        (self.root / 'data/link-mcp/state').symlink_to(self.root)
        with self.assertRaisesRegex(server.DeployError, 'symlink'):
            server.prepare_data_dirs('link-mcp', contract, (10001, 10001))


if __name__ == '__main__':
    unittest.main()
