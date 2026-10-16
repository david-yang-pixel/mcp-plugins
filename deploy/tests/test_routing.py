import copy
import hashlib
import json
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server
import catalog

REVISION = 'a' * 40


def target(**extra):
    return server.normalize_target({'container_port': 8000, 'auth_key': 'TEST_API_KEY',
        'health_path': '/health', 'env_keys': ['TEST_API_KEY'], 'secret_file_paths': {}, **extra})


class AllocationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / 'state').mkdir()
        patcher = patch.object(server, 'ROOT', self.root)
        patcher.start();self.addCleanup(patcher.stop)

    def test_default_auto_port_keeps_fixed_container_port(self):
        self.assertEqual(target()['port'], 'auto')
        with patch.object(server, 'reserved_ports', return_value=set()), patch.object(server, 'AUTO_PORTS', [28191]):
            resolved = server.allocate_target('example-mcp', target(), {})
        self.assertEqual(resolved['port'], 28191)
        self.assertEqual(resolved['container_port'], 8000)
        args = server.run_arguments('example-mcp', 'image', REVISION, resolved, self.root)
        self.assertIn('127.0.0.1:28191:8000', args)

    def test_skip_reserved_and_listening_ports(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            occupied = listener.getsockname()[1]
            with patch.object(server, 'reserved_ports', return_value={28191}), patch.object(server, 'AUTO_PORTS', [28191, occupied, 28192]):
                self.assertEqual(server.allocate_target('example-mcp', target(), {})['port'], 28192)

    def test_exhaustion_fails_instead_of_reusing_a_reserved_port(self):
        with patch.object(server, 'reserved_ports', return_value={28191}), patch.object(server, 'AUTO_PORTS', [28191]):
            with self.assertRaisesRegex(server.DeployError, 'No free port'):
                server.allocate_target('example-mcp', target(), {})

    def test_update_reuses_persisted_allocation_without_rescanning(self):
        (self.root / 'state/example-mcp.json').write_text(json.dumps({'target': target(port=28191)}))
        with patch.object(server, 'reserved_ports', side_effect=AssertionError('must not reallocate')):
            self.assertEqual(server.allocate_target('example-mcp', target(), {})['port'], 28191)

    def test_auto_cannot_silently_replace_missing_previous_allocation(self):
        (self.root / 'state/example-mcp.json').write_text('{}')
        with self.assertRaisesRegex(server.DeployError, 'Existing allocation'):
            server.allocate_target('example-mcp', target(), {})

    def test_auto_port_is_not_valid_for_host_networking(self):
        with self.assertRaises(server.DeployError): target(network='host')

    def test_stopped_docker_port_bindings_are_reserved(self):
        containers = [{'HostConfig': {'PortBindings': {'8000/tcp': [{'HostPort': '28191'}]}}}]
        (self.root / 'state/other-mcp.json').write_text(json.dumps({'target': {'port': 28192}}))
        with patch.object(server, 'command', side_effect=[b'id1\n', json.dumps(containers).encode()]):
            self.assertEqual(server.reserved_ports({'legacy': {'port': 8111}}), {8111, 28191, 28192})

    def test_multiple_auto_services_do_not_trigger_duplicate_port_validation(self):
        for service in ('first-mcp', 'second-mcp'):
            folder = self.root / service / 'deploy';folder.mkdir(parents=True)
            plan = {'deployment': {'container_port': 8000, 'auth_key': 'TEST_API_KEY', 'health_path': '/health'},
                'environment': {}, 'secrets': {'TEST_API_KEY': {'name': catalog.secret_name(service, 'TEST_API_KEY'), 'required': True}}, 'secret_files': {}}
            (folder / 'runtime.json').write_text(json.dumps(plan))
            for name in ('Dockerfile', 'pyproject.toml', 'uv.lock', 'requirements.runtime.txt'):
                (folder.parent / name).touch()
        self.assertEqual(catalog.validate_catalog(self.root), {})


class NginxTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.routes = self.root / 'routes';self.routes.mkdir()
        self.site = self.root / 'site.conf'
        for key, value in [('ROOT', self.root), ('NGINX_ROUTES', self.routes), ('NGINX_SITE', self.site)]:
            patcher = patch.object(server, key, value);patcher.start();self.addCleanup(patcher.stop)
        self.original = '''server { listen 80; server_name zendesk.smlershou.top; return 301 https://$host$request_uri; }
server {
 listen 443 ssl;
 server_name zendesk.smlershou.top;
 add_header Example "text; { protected }";
 location /admin/ { proxy_pass http://127.0.0.1:18104/; }
 include /etc/nginx/snippets/extra.conf;
 location / { return 404; }
}
'''
        self.with_include = server.nginx_include_content(self.original)
        self.documents = {str(self.site): server.parse_nginx(self.with_include, str(self.site))}

    def route(self):
        with patch.object(server, 'nginx_documents', return_value=self.documents):
            return server.RouteTransaction('example-mcp', target(port=28191), REVISION)

    def test_add_include_only_to_https_server_and_preserve_original_bytes(self):
        addition = '    # Per-service MCP routes managed by mcp-plugins-deploy.\n    include ' + str(self.routes / '*.conf') + ';\n'
        self.assertEqual(self.with_include.replace(addition, ''), self.original)
        self.assertEqual(server.nginx_include_content(self.with_include), self.with_include)

    def test_location_parser_handles_quotes_empty_strings_and_comments(self):
        nodes = server.parse_nginx('location = /foo/mcp { proxy_set_header Connection ""; # comment\n add_header Text "{;}"; }')
        self.assertEqual(nodes[0]['children'][0]['args'], ['Connection', ''])
        self.assertEqual(nodes[0]['children'][1]['args'], ['Text', '{;}'])

    def test_unrelated_routes_are_allowed_but_namespace_is_not_adopted(self):
        self.route()
        with patch.object(server, 'nginx_documents', return_value=self.documents):
            with self.assertRaisesRegex(server.DeployError, 'conflicts'):
                server.check_route_namespace('admin', self.routes / 'admin.conf')
        extra = '/etc/nginx/snippets/extra.conf'
        self.documents[extra] = server.parse_nginx('location = /example-mcp/mcp { return 404; }', extra)
        with self.assertRaisesRegex(server.DeployError, 'conflicts'): self.route()

    def test_existing_regex_route_is_not_shadowed(self):
        extra = '/etc/nginx/snippets/extra.conf'
        self.documents[extra] = server.parse_nginx('location ~ ^/example- { return 404; }', extra)
        with self.assertRaisesRegex(server.DeployError, 'conflicts'): self.route()

    def test_foreign_domain_same_path_does_not_block_service(self):
        name = '/etc/nginx/foreign.conf'
        self.documents[name] = server.parse_nginx('server {listen 443 ssl; server_name other.example; location /example-mcp/ {return 200;} }', name)
        self.route()

    def test_route_file_without_matching_state_cannot_be_overwritten(self):
        (self.routes / 'example-mcp.conf').write_text('# somebody else\n')
        with self.assertRaisesRegex(server.DeployError, 'ownership or drift'): self.route()

    def test_rendered_route_is_exact_and_preserves_streaming(self):
        content = server.render_route('example-mcp', 28191, REVISION)
        self.assertIn('location = /example-mcp/mcp', content)
        self.assertIn('proxy_pass http://127.0.0.1:28191/mcp;', content)
        self.assertIn('proxy_buffering off;', content)
        self.assertNotIn('location /example-mcp/', content)
        with self.assertRaises(server.DeployError): server.render_route('x;include', 28191, REVISION)

    def test_failed_config_test_restores_file_without_reloading_bad_config(self):
        route = self.route()
        with patch.object(server, 'check_route_namespace'), patch.object(server, 'nginx_command', side_effect=[server.DeployError('invalid'), b'ok']), patch.object(server, 'reload_nginx') as reload:
            with self.assertRaises(server.DeployError): route.publish({'TEST_API_KEY': 'synthetic'})
            route.rollback()
        reload.assert_not_called()
        self.assertFalse(route.path.exists())

    def test_failed_https_verification_restores_prior_route_bytes(self):
        content = server.render_route('example-mcp', 28191, 'b' * 40)
        path = self.routes / 'example-mcp.conf';path.write_text(content)
        (self.root / 'state').mkdir()
        (self.root / 'state/example-mcp.json').write_text(json.dumps({'route_sha256': hashlib.sha256(content.encode()).hexdigest()}))
        route = self.route()
        with patch.object(server, 'check_route_namespace'), patch.object(server, 'nginx_command'), patch.object(server, 'reload_nginx') as reload, patch.object(server, 'wait_public', side_effect=server.DeployError('HTTPS failed')):
            with self.assertRaises(server.DeployError): route.publish({'TEST_API_KEY': 'synthetic'})
            route.rollback()
        self.assertEqual(path.read_text(), content)
        self.assertEqual(reload.call_count, 2)

    def test_legacy_route_is_never_rewritten(self):
        route = server.RouteTransaction('github-mcp', target(port=8111), REVISION, legacy=True)
        with patch.object(server, 'wait_public', return_value='https://example.invalid/mcp'), patch.object(server, 'reload_nginx') as reload:
            route.publish({'TEST_API_KEY': 'synthetic'});route.rollback()
        reload.assert_not_called()
        self.assertEqual(list(self.routes.iterdir()), [])

    def test_rollback_does_not_overwrite_an_administrator_change(self):
        route = self.route()
        with patch.object(server, 'check_route_namespace'), patch.object(server, 'nginx_command'), patch.object(server, 'reload_nginx'), patch.object(server, 'wait_public', return_value='verified'):
            route.publish({'TEST_API_KEY': 'synthetic'})
        route.path.write_text('# edited by administrator\n')
        with self.assertRaisesRegex(server.DeployError, 'administrator changes'):
            route.rollback()
        self.assertEqual(route.path.read_text(), '# edited by administrator\n')

    def test_credentials_never_follow_redirects(self):
        with self.assertRaisesRegex(server.DeployError, 'redirect'):
            server.NoRedirect().redirect_request(None, None, 302, None, None, 'https://foreign.example')

    def test_public_probe_requires_tools_revision_and_bad_key_rejection(self):
        unauthorized = urllib.error.HTTPError('https://example.invalid', 401, 'Unauthorized', {}, None)
        response = ({'result': {'tools': [{'name': 'example'}]}}, REVISION)
        with patch.object(server, 'public_request', side_effect=[response, unauthorized]):
            self.assertEqual(server.verify_public('example-mcp', {'TEST_API_KEY': 'synthetic'}, target(), REVISION), server.PUBLIC_ORIGIN + '/example-mcp/mcp')
        with patch.object(server, 'public_request', return_value=(response[0], 'wrong')):
            with self.assertRaisesRegex(server.DeployError, 'route identity'):
                server.verify_public('example-mcp', {'TEST_API_KEY': 'synthetic'}, target(), REVISION)


if __name__ == '__main__':
    unittest.main()
