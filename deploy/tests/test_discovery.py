import copy
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import catalog
import client
import server
import select_services

REVISION = 'a' * 40
DIGEST = 'sha256:' + 'b' * 64


def plan(port=18120):
    return {'deployment': {'port': port, 'auth_key': 'EXAMPLE_MCP_API_KEY', 'health_path': '/health'},
            'environment': {'EXAMPLE_MCP_HOST': '0.0.0.0', 'EXAMPLE_MCP_PORT': str(port)},
            'secrets': {'EXAMPLE_MCP_API_KEY': {'name': 'MCP_EXAMPLE_MCP__EXAMPLE_MCP_API_KEY', 'required': True}},
            'secret_files': {}}


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        folder = self.root / 'example-mcp' / 'deploy'
        folder.mkdir(parents=True)
        self.path = folder / 'runtime.json'
        self.path.write_text(json.dumps(plan()))
        for name in ('Dockerfile', 'pyproject.toml', 'uv.lock', 'requirements.runtime.txt'):
            (folder.parent / name).touch()

    def test_new_directory_is_discovered_without_registration(self):
        self.assertEqual(catalog.discover(self.root), ('example-mcp',))
        self.assertEqual(catalog.validate_catalog(self.root), {18120: 'example-mcp'})
        target = catalog.target('example-mcp', self.root)
        self.assertEqual(target['network'], 'bridge')
        self.assertEqual(server.resolve_target('example-mcp', target, {}), target)

    def test_new_service_cannot_read_another_services_secrets(self):
        data = plan()
        data['secrets']['EXAMPLE_MCP_API_KEY']['name'] = 'MCP_DEPLOY_SSH_KEY'
        self.path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, 'prefix'):
            catalog.spec('example-mcp', self.root)

    def test_new_service_auth_must_be_required_secret(self):
        data = plan()
        data['secrets']['EXAMPLE_MCP_API_KEY']['required'] = False
        self.path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, 'required GitHub Secret'):
            catalog.target('example-mcp', self.root)

    def test_missing_deployment_contract_fails_clearly(self):
        data = plan();del data['deployment']
        self.path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, 'deployment configuration'):
            catalog.target('example-mcp', self.root)

    def test_duplicate_port_fails_before_build_or_deploy(self):
        folder = self.root / 'second-mcp' / 'deploy'
        folder.mkdir(parents=True)
        data = plan()
        data['secrets']['EXAMPLE_MCP_API_KEY']['name'] = 'MCP_SECOND_MCP__EXAMPLE_MCP_API_KEY'
        (folder / 'runtime.json').write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, 'Duplicate service port'):
            catalog.validate_catalog(self.root)

    def test_secret_slot_limit_does_not_silently_drop_values(self):
        data = plan()
        for i in range(32):
            key = 'EXTRA_' + str(i)
            data['secrets'][key] = {'name': 'MCP_EXAMPLE_MCP__' + key, 'required': True}
        self.path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, 'At most 32'):
            catalog.spec('example-mcp', self.root)

    def test_secret_slots_map_to_only_selected_service(self):
        service = 'github-mcp'
        items = catalog.secret_items(service)
        env = {f'MCP_SECRET_{i}': f'synthetic-{key}' for i, (key, _) in enumerate(items)}
        env.update(REGISTRY_USER='test-user', REGISTRY_TOKEN='synthetic-registry')
        payload = client.payload(service, DIGEST, env)
        for key, _ in items:
            self.assertEqual(payload['environment'][key], 'synthetic-' + key)
        entry = catalog.matrix_entry(service)
        self.assertEqual(len(entry['secret_names']), 32)
        self.assertTrue(all(not name or name.startswith('MCP_GITHUB_MCP__') for name in entry['secret_names']))
        self.assertNotIn('REGISTRY_TOKEN', payload['environment'])

    def test_added_service_and_shared_workflow_only_select_new_service(self):
        names = list(catalog.SERVICES) + ['example-mcp']
        self.assertEqual(select_services.select(['example-mcp/src/app.py', 'deploy/catalog.py', '.github/workflows/ci-cd.yml'], names), ['example-mcp'])
        self.assertEqual(select_services.select(['deploy/catalog.py', '.github/workflows/ci-cd.yml'], names), [])

    def test_real_git_diff_for_registration_does_not_redeploy_peers(self):
        def git(*args):
            return subprocess.check_output(['git', *args], cwd=self.root, stderr=subprocess.DEVNULL, text=True).strip()
        git('init');git('config', 'user.email', 'test@example.invalid');git('config', 'user.name', 'Test')
        (self.root / 'deploy').mkdir()
        registry = self.root / 'deploy/targets.json'
        registry.write_text(json.dumps({'existing-mcp': {'port': 18121}}))
        git('add', '.');git('commit', '-m', 'base');base = git('rev-parse', 'HEAD')
        registry.write_text(json.dumps({'existing-mcp': {'port': 18121}, 'new-mcp': {'port': 18122}}))
        (self.root / 'new-mcp').mkdir();(self.root / 'new-mcp/app.py').write_text('new')
        git('add', '.');git('commit', '-m', 'new');head = git('rev-parse', 'HEAD')
        with patch.object(select_services, 'SERVICES', ('existing-mcp', 'new-mcp')):
            self.assertEqual(select_services.changed_services(base, head, self.root), ['new-mcp'])
        registry.write_text(json.dumps({'existing-mcp': {'port': 18123}, 'new-mcp': {'port': 18122}}))
        git('add', '.');git('commit', '-m', 'existing port')
        with patch.object(select_services, 'SERVICES', ('existing-mcp', 'new-mcp')):
            self.assertEqual(select_services.changed_services(head, git('rev-parse', 'HEAD'), self.root), ['existing-mcp'])


class FirstDeployTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.target = server.normalize_target({**plan()['deployment'], 'env_keys': ['EXAMPLE_MCP_API_KEY'], 'secret_file_paths': {}})
        self.payload = {'environment': {'EXAMPLE_MCP_API_KEY': 'synthetic-secret'}}
        self.new = {'Id': 'new-id', 'Image': 'new-image', 'Config': {
            'Env': ['EXAMPLE_MCP_API_KEY=synthetic-secret'],
            'Labels': {'io.mcp-plugins.managed': 'true', 'io.mcp-plugins.revision': REVISION}},
            'HostConfig': {'Init': False, 'ShmSize': 64 * 1024 * 1024}}

    def create(self, health=1, existing=None, persist=None, candidate=None):
        with patch.object(server, 'inspect_optional', side_effect=[existing, candidate or self.new]), \
             patch.object(server, 'inspect', return_value=self.new), \
             patch.object(server, 'command', return_value=b'new-id') as command, \
             patch.object(server, 'wait_healthy', side_effect=[health]):
            try:
                result = server.create_container('example-mcp', REVISION, 'image', self.target, self.payload, self.root, 'new-image', persist)
            except Exception as e:
                result = e
            return result, [c.args[0] for c in command.call_args_list]

    def test_first_deploy_creates_without_stop_or_rename(self):
        saved = []
        result, operations = self.create(persist=saved.append)
        self.assertEqual(result['container'], 'new-id')
        self.assertIsNone(result['previous_container'])
        self.assertEqual(saved, [result])
        self.assertEqual(len(operations), 1)
        self.assertIn('127.0.0.1:18120:18120', operations[0])
        self.assertIn('--cap-drop=ALL', operations[0])

    def test_first_deploy_health_failure_removes_only_new_container(self):
        result, operations = self.create(health=server.DeployError('unhealthy'))
        self.assertIsInstance(result, server.DeployError)
        self.assertEqual(operations[-1], ['docker', 'rm', '-f', 'example-mcp'])
        self.assertFalse(any(op[1] in ('stop', 'rename', 'start') for op in operations))

    def test_state_write_failure_cleans_up_first_deploy(self):
        def fail(_): raise OSError('disk full')
        result, operations = self.create(persist=fail)
        self.assertIsInstance(result, server.DeployError)
        self.assertEqual(operations[-1], ['docker', 'rm', '-f', 'example-mcp'])

    def test_name_collision_does_not_mutate_existing_container(self):
        result, operations = self.create(existing={'Id': 'unrelated'})
        self.assertIsInstance(result, server.DeployError)
        self.assertEqual(operations, [])

    def test_failed_creation_cannot_remove_unrelated_container(self):
        other = copy.deepcopy(self.new);other['Config']['Labels'] = {}
        result, operations = self.create(health=server.DeployError('unhealthy'), candidate=other)
        self.assertIn('unexpected container', str(result))
        self.assertFalse(any(op[1] == 'rm' for op in operations))

    def test_port_collision_refuses_before_changes(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            target = {**self.target, 'port': sock.getsockname()[1]}
            with patch.object(server, 'ROOT', self.root):
                with self.assertRaisesRegex(server.DeployError, 'already in use'):
                    server.preflight('example-mcp', target, {}, None)

    def test_reserved_port_of_stopped_service_is_protected(self):
        with patch.object(server, 'ROOT', self.root):
            with self.assertRaisesRegex(server.DeployError, 'reserved'):
                server.preflight('example-mcp', self.target, {'existing-mcp': {'port': 18120}}, None)

    def test_unmanaged_existing_container_is_not_adopted(self):
        with patch.object(server, 'ROOT', self.root):
            with self.assertRaisesRegex(server.DeployError, 'not owned'):
                server.preflight('example-mcp', self.target, {}, self.new)

    def test_new_services_cannot_request_host_network_or_host_mounts(self):
        cases = [{**self.target, 'network': 'host'}, {**self.target, 'mounts': [{'source': '/', 'destination': '/host', 'readonly': False}]},
                 {**self.target, 'mounts': [{'source': '@data/../../root', 'destination': '/var/lib/mcp/data', 'readonly': False}]},
                 {**self.target, 'secret_file_paths': {'KEY': '/etc/passwd'}}]
        for target in cases:
            with self.subTest(target=target), self.assertRaises(server.DeployError):
                server.resolve_target('example-mcp', target, {})

    def test_new_service_managed_data_is_scoped_by_name(self):
        target = {**self.target, 'mounts': [{'source': '@data/cache', 'destination': '/var/lib/mcp/cache', 'readonly': False}]}
        target = server.resolve_target('example-mcp', target, {})
        with patch.object(server, 'ROOT', self.root):
            args = server.run_arguments('example-mcp', 'image', REVISION, target, self.root)
        self.assertIn(f'type=bind,source={self.root}/data/example-mcp/cache,target=/var/lib/mcp/cache', args)

    def test_docker_daemon_failure_is_not_treated_as_absent_container(self):
        with patch.object(server, 'command', side_effect=server.DeployError('daemon unavailable')):
            with self.assertRaises(server.DeployError): server.inspect_optional('example-mcp')

    def test_bridge_service_can_keep_image_nonroot_user_on_update(self):
        current = {'Config': {'User': '1000'}, 'Mounts': [], 'HostConfig': {
            'NetworkMode': 'bridge', 'Privileged': False, 'Init': False,
            'ShmSize': 64 * 1024 * 1024,
            'PortBindings': {'18120/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '18120'}]}}}
        server.validate_current(current, self.target, 'example-mcp')
        current['HostConfig']['ShmSize'] = 32 * 1024 * 1024
        with self.assertRaisesRegex(server.DeployError, 'shared memory'):
            server.validate_current(current, self.target, 'example-mcp')


if __name__ == '__main__':
    unittest.main()
