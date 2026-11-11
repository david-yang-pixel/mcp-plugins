from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server

REVISION = 'a' * 40
SECRET = 'synthetic-key-not-a-real-credential'


def target(**extra):
    return server.normalize_target({'container_port': 8000, 'auth_key': 'TEST_API_KEY',
        'health_path': '/health', 'env_keys': ['TEST_API_KEY'], 'secret_file_paths': {}, **extra})


class HealthAndLogArgumentTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def test_bridge_service_probes_its_container_port(self):
        args = server.run_arguments('example-mcp', 'image', REVISION, target(port=28191), self.root)
        probe = args[args.index('--health-cmd') + 1]
        self.assertIn("http://127.0.0.1:8000/health", probe)
        self.assertNotIn('28191', probe)
        self.assertIn("os.environ.get('TEST_API_KEY','')", probe)
        self.assertEqual(args[args.index('--health-interval') + 1], '30s')
        self.assertEqual(args[args.index('--health-retries') + 1], '3')

    def test_host_network_service_probes_its_fixed_port_and_health_path(self):
        legacy = target(network='host', port=18081, container_port=18081, health_path='/healthz')
        probe = server.health_command(legacy)
        self.assertIn("http://127.0.0.1:18081/healthz", probe)

    def test_options_precede_the_image_and_command(self):
        args = server.run_arguments('example-mcp', 'image', REVISION, target(port=28191, command=['serve']), self.root)
        self.assertEqual(args[-2:], ['image', 'serve'])
        for flag in ('--health-cmd', '--log-driver'):
            self.assertLess(args.index(flag), args.index('image'))

    def test_logs_rotate_per_container(self):
        args = server.run_arguments('example-mcp', 'image', REVISION, target(port=28191), self.root)
        self.assertEqual(args[args.index('--log-driver') + 1], 'json-file')
        options = [args[i + 1] for i, value in enumerate(args) if value == '--log-opt']
        self.assertEqual(options, ['max-size=20m', 'max-file=5'])

    def test_probe_is_safe_inside_a_double_quoted_shell_word(self):
        command = server.health_command(target(port=28191))
        self.assertTrue(command.startswith('python -c "') and command.endswith('"'))
        body = command[len('python -c "'):-1]
        self.assertFalse(set(body) & set('"$`\\!\n'))
        self.assertNotIn(SECRET, command)


class HealthProbeBehaviourTests(unittest.TestCase):
    """Run the exact probe source against a local server."""

    def serve(self, status, body):
        seen = {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen['path'] = self.path
                seen['authorization'] = self.headers.get('Authorization')
                seen['api_key'] = self.headers.get('X-API-Key')
                payload = json.dumps(body).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        httpd = HTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return httpd.server_address[1], seen

    def probe(self, port):
        source = server.HEALTH_PROBE.format(key='TEST_API_KEY', port=port, path='/health')
        env = {**os.environ, 'TEST_API_KEY': SECRET}
        return subprocess.run([sys.executable, '-c', source], env=env, capture_output=True, timeout=30).returncode

    def test_healthy_service_passes_and_receives_the_key_from_the_environment(self):
        port, seen = self.serve(200, {'ok': True})
        self.assertEqual(self.probe(port), 0)
        self.assertEqual(seen, {'path': '/health', 'authorization': 'Bearer ' + SECRET, 'api_key': SECRET})

    def test_health_without_an_ok_field_passes_like_verify_service(self):
        port, _ = self.serve(200, {'status': 'up'})
        self.assertEqual(self.probe(port), 0)

    def test_degraded_service_fails(self):
        port, _ = self.serve(200, {'ok': False})
        self.assertEqual(self.probe(port), 1)

    def test_http_error_fails(self):
        port, _ = self.serve(503, {'ok': False})
        self.assertNotEqual(self.probe(port), 0)

    def test_unreachable_service_fails(self):
        port, _ = self.serve(200, {'ok': True})
        self.doCleanups()
        self.assertNotEqual(self.probe(port), 0)


if __name__ == '__main__':
    unittest.main()
