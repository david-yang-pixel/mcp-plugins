import copy
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import catalog
import client
import server
from select_services import select
from generate_workflow import render

REVISION = "a" * 40
DIGEST = "sha256:" + "b" * 64


def fixture(service="docs-tool-service"):
    target = catalog.target(service)
    plan = catalog.spec(service)
    env = {"SECRET_" + k: "test-placeholder-value" for k in plan["secrets"] | plan["secret_files"]}
    env.update(REGISTRY_USER="test-user", REGISTRY_TOKEN="test-registry-placeholder")
    return target, client.payload(service, DIGEST, env)


class ContractTests(unittest.TestCase):
    def test_all_discovered_service_payloads_validate(self):
        for service in catalog.SERVICES:
            with self.subTest(service=service):
                target, payload = fixture(service)
                self.assertEqual(server.validate_request(service, REVISION, payload, target),
                                 f"ghcr.io/{server.REPOSITORY}/{service}@{DIGEST}")

    def test_tag_or_shell_command_cannot_replace_digest(self):
        target, payload = fixture()
        for value in ("latest", "$(id)", "sha256:" + "b" * 64 + ";id"):
            payload["digest"] = value
            with self.assertRaises(server.DeployError):
                server.validate_request("docs-tool-service", REVISION, payload, target)

    def test_cannot_inject_environment_or_disable_authentication(self):
        target, payload = fixture()
        cases = []
        extra = copy.deepcopy(payload)
        extra["environment"]["LD_PRELOAD"] = "/tmp/inject.so"
        cases.append(extra)
        empty = copy.deepcopy(payload)
        empty["environment"][target["auth_key"]] = ""
        cases.append(empty)
        newline = copy.deepcopy(payload)
        newline["environment"][target["auth_key"]] = "value\nLD_PRELOAD=/tmp/inject.so"
        cases.append(newline)
        path = copy.deepcopy(payload)
        path["files"] = {"../../root/.ssh/authorized_keys": "invalid"}
        cases.append(path)
        for data in cases:
            with self.assertRaises(server.DeployError):
                server.validate_request("docs-tool-service", REVISION, data, target)

    def test_missing_secret_fails_before_ssh(self):
        with self.assertRaisesRegex(ValueError, "Missing GitHub Secret"):
            client.payload("docs-tool-service", DIGEST, {})

    def test_no_secrets_from_other_services_in_payload(self):
        target, payload = fixture("github-mcp")
        self.assertNotIn("ZENDESK_LOGIN_PASSWORD", payload["environment"])
        self.assertNotIn("REGISTRY_TOKEN", payload["environment"])

    def test_generated_workflow_is_current_and_scoped(self):
        actual = (catalog.ROOT / ".github/workflows/ci-cd.yml").read_text()
        self.assertEqual(actual, render())
        self.assertNotIn("toJSON(secrets)", actual)
        self.assertNotIn("pull_request_target", actual)
        self.assertIn("max-parallel: 1", actual)
        self.assertIn("refs/heads/main", actual)

    def test_changed_service_selection(self):
        self.assertEqual(select(["github-mcp/src/github_mcp/tools.py"]), ["github-mcp"])
        self.assertEqual(select(["README.md"]), [])
        self.assertEqual(select(["deploy/server.py"]), [])

    def test_ssh_shell_command_refused(self):
        with patch.object(server, "TARGETS", catalog.ROOT / "deploy/targets.json"):
            with patch.dict(os.environ, {"SSH_ORIGINAL_COMMAND": "sh -c id"}):
                with self.assertRaisesRegex(server.DeployError, "valid service"):
                    server.main()

    def test_command_failure_does_not_print_sensitive_stderr(self):
        result = subprocess.CompletedProcess(["docker"], 1, b"", b"synthetic-sensitive-value")
        with patch.object(server.subprocess, "run", return_value=result):
            with self.assertRaises(server.DeployError) as error:
                server.command(["docker", "run"])
        self.assertNotIn("synthetic-sensitive-value", str(error.exception))

    def test_health_requires_authentication_rejection(self):
        target, payload = fixture()
        replies = [{"ok": True}, {"result": {"tools": [{"name": "read"}]}}, {}]
        with patch.object(server, "request_json", side_effect=replies):
            with self.assertRaisesRegex(server.DeployError, "Unauthenticated"):
                server.verify_service(target, payload["environment"])

    def test_health_accepts_authenticated_discovery_and_401(self):
        target, payload = fixture()
        error = urllib.error.HTTPError("http://localhost", 401, "Unauthorized", {}, None)
        replies = [{"ok": True}, {"result": {"tools": [{"name": "read"}]}}, error]
        with patch.object(server, "request_json", side_effect=replies):
            self.assertEqual(server.verify_service(target, payload["environment"]), 1)


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.service = "docs-tool-service"
        self.target, self.payload = fixture()
        self.old = {"Id": "old-container", "Image": "old-image",
                    "Config": {"Env": [k + "=" + v for k, v in self.payload["environment"].items()], "User": "", "Labels": {}},
                    "HostConfig": {"NetworkMode": "host", "Privileged": False, "RestartPolicy": {"Name": "always"}},
                    "Mounts": []}
        self.new = copy.deepcopy(self.old)
        self.new.update(Id="new-container", Image="new-image")
        self.new["Config"]["Labels"] = {"io.mcp-plugins.revision": REVISION, "io.mcp-plugins.managed": "true"}
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.release = Path(self.directory.name)
        (self.release / "runtime.env").write_text("placeholder")

    def execute(self, wait_effect=None, current=None, new=None):
        with patch.object(server, "inspect", side_effect=[current or self.old, new or self.new]), \
             patch.object(server, "inspect_optional", return_value=new or self.new), \
             patch.object(server, "verify_service", return_value=5), \
             patch.object(server, "wait_healthy", side_effect=wait_effect) as wait, \
             patch.object(server, "command", return_value=b"ok") as command, \
             patch.object(server.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps([new or self.new]).encode(), b"")):
            try:
                result = server.replace_container(self.service, REVISION, "image", self.target, self.payload, self.release, "new-image")
                return result, command.call_args_list, wait.call_count
            except Exception as exc:
                return exc, command.call_args_list, wait.call_count

    def test_success_preserves_previous_container(self):
        result, commands, waits = self.execute([5])
        self.assertEqual(result["container"], "new-container")
        self.assertTrue(result["previous_container"].startswith(self.service + ".rollback-"))
        self.assertFalse(any(call.args[0][1] == "rm" for call in commands))

    def test_unhealthy_new_container_restores_previous_and_rechecks(self):
        result, commands, waits = self.execute([server.DeployError("unhealthy"), 5])
        self.assertIsInstance(result, server.DeployError)
        self.assertIn("restored and verified", str(result))
        self.assertEqual(waits, 2)
        operations = [call.args[0] for call in commands]
        self.assertIn(["docker", "rm", "-f", self.service], operations)
        self.assertIn(["docker", "start", self.service], operations)
        self.assertIn(["docker", "update", "--restart=always", self.service], operations)

    def test_environment_readback_mismatch_rolls_back(self):
        changed = copy.deepcopy(self.new)
        changed["Config"]["Env"] = []
        result, commands, waits = self.execute([5, 5], new=changed)
        self.assertIsInstance(result, server.DeployError)
        self.assertEqual(waits, 2)

    def test_mount_drift_refuses_before_stopping(self):
        changed = copy.deepcopy(self.old)
        changed["Mounts"] = [{"Destination": "/unexpected", "Source": "/", "Type": "bind"}]
        result, commands, waits = self.execute([5], current=changed)
        self.assertIsInstance(result, server.DeployError)
        self.assertEqual(commands, [])


if __name__ == "__main__":
    unittest.main()
