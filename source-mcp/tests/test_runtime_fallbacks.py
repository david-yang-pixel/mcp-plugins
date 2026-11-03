from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import anyio
import pytest

from source_mcp import runtime
from source_mcp.errors import SourceError
from source_mcp.runtime import RuntimeRefUnavailable
from source_mcp.settings import Settings


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _gitops_repo(base: Path) -> Path:
    repo = base / "saas-deploy"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    files = {
        "environments/production/dify/rbac/kustomization.yaml": (
            "resources:\n  - ../../../../apps/dify/rbac/base\n"
            "images:\n  - name: langgenius/rbac\n    newName: langgenius/rbac\n    newTag: aaf954b4\n"
            "\nreplicas:\n  - name: dify-rbac\n    count: 2\n"
        ),
        "environments/production/saas/billing/kustomization.yaml": (
            "images:\n  - name: langgenius/dify-billing\n    newName: langgenius/dify-billing\n"
            "    newTag: release-20260616.1\n"
        ),
        "environments/production/saas/billing-worker/kustomization.yaml": (
            "images:\n  - name: langgenius/dify-billing\n    newTag: release-20260601.1\n"
        ),
        "environments/staging/dify/sandbox/kustomization.yaml": (
            'images:\n- name: langgenius/dify-sandbox\n  newTag: "0.2.15"\nreplicas:\n- name: x\n'
        ),
    }
    for name, text in files.items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    # default_branch_ref() reads refs/remotes/origin/HEAD; emulate a clone.
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    return repo


def _settings(monkeypatch, base: Path, **env: str) -> Settings:
    monkeypatch.setenv("SOURCE_MCP_REPOS_BASE_DIR", str(base))
    monkeypatch.setenv("SOURCE_MCP_REPOS", env.pop("repos", "saas-deploy,rbac,dify-billing,dify-sandbox"))
    monkeypatch.setenv("SOURCE_MCP_SIGNOZ_MCP_URL", "http://signoz.invalid")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)


def test_kustomize_image_tag_parses_both_list_styles():
    text = (
        "images:\n  - name: other/image\n    newTag: wrong\n"
        "  - name: langgenius/rbac # comment\n    newName: langgenius/rbac\n    newTag: 'aaf954b4'\n"
    )
    assert runtime._kustomize_image_tag(text, "langgenius/rbac") == "aaf954b4"
    assert runtime._kustomize_image_tag('- name: a/b\n  newTag: "1.2"\n', "a/b") == "1.2"
    assert runtime._kustomize_image_tag("- name: a/b\nreplicas:\n  newTag: x\n", "a/b") is None


def test_gitops_image_tag_reads_declared_production_tag(monkeypatch, tmp_path):
    _gitops_repo(tmp_path)
    settings = _settings(monkeypatch, tmp_path)

    rbac = anyio.run(runtime.gitops_image_tag, "dify-rbac", "production", settings)
    billing = anyio.run(runtime.gitops_image_tag, "dify-billing", "production", settings)
    sandbox = anyio.run(runtime.gitops_image_tag, "dify-sandbox", "production", settings)

    assert rbac["image_tag"] == "aaf954b4"
    assert rbac["gitops"]["path"] == "environments/production/dify/rbac/kustomization.yaml"
    # The overlay whose directory matches the deployment wins over billing-worker.
    assert billing["image_tag"] == "release-20260616.1"
    assert billing["gitops"]["other_overlays"][0]["image_tag"] == "release-20260601.1"
    # Sandbox is only declared for staging.
    assert sandbox is None


def test_gitops_lookup_never_clones(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)  # saas-deploy configured but not cloned

    assert anyio.run(runtime.gitops_image_tag, "dify-rbac", "production", settings) is None
    assert not (tmp_path / "saas-deploy").exists()


def test_runtime_ref_falls_back_to_gitops_when_telemetry_has_no_tag(monkeypatch, tmp_path):
    _gitops_repo(tmp_path)
    settings = _settings(monkeypatch, tmp_path)

    async def no_logs(_deployment: str, _settings: Settings, _environment: Any = None):
        return None

    monkeypatch.setattr(runtime, "_query_runtime_image", no_logs)
    resolved: list[str] = []

    async def rev_parse(repo: str, ref: str, _settings: Settings) -> str:
        resolved.append(ref)
        return "c" * 40

    result = anyio.run(
        lambda: runtime.resolve_runtime_ref(
            repo="rbac", deployment=None, settings=settings, rev_parse_func=rev_parse
        )
    )

    assert result["source"] == "gitops"
    assert result["deployment"] == "dify-rbac"
    assert result["image_tag"] == "aaf954b4"
    assert result["ref"] == "aaf954b4"
    assert "GitOps desired state" in result["source_warning"]
    assert result["gitops"]["repo"] == "saas-deploy"


def test_runtime_ref_unavailable_when_neither_source_has_a_tag(monkeypatch, tmp_path):
    _gitops_repo(tmp_path)
    settings = _settings(monkeypatch, tmp_path)

    async def no_logs(*_args: Any, **_kwargs: Any):
        return None

    monkeypatch.setattr(runtime, "_query_runtime_image", no_logs)

    async def rev_parse(*_args: Any) -> str:
        raise AssertionError("must not resolve")

    with pytest.raises(RuntimeRefUnavailable, match="saas-deploy"):
        anyio.run(
            lambda: runtime.resolve_runtime_ref(
                repo="dify-sandbox", deployment=None, settings=settings, rev_parse_func=rev_parse
            )
        )


def test_billing_maps_to_dify_billing_deployment(monkeypatch):
    monkeypatch.delenv("SOURCE_MCP_RUNTIME_DEPLOYMENTS", raising=False)

    assert runtime.configured_deployments(Settings(_env_file=None), "dify-billing") == [
        "dify-billing",
        "billing",
    ]


def test_marketplace_tag_prefix_is_tried_first(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path, repos="dify-marketplace")

    async def image(deployment: str, _settings: Settings, _environment: Any = None):
        return {"image_tag": "2.3.2", "environment": "production"} if deployment == "dify-marketplace-ops-backend" else None

    monkeypatch.setattr(runtime, "_query_runtime_image", image)
    tried: list[str] = []

    async def rev_parse(_repo: str, ref: str, _settings: Settings) -> str:
        tried.append(ref)
        if ref != "ops/2.3.2":
            raise SourceError("REF_NOT_FOUND", ref)
        return "d" * 40

    result = anyio.run(
        lambda: runtime.resolve_runtime_ref(
            repo="dify-marketplace", deployment=None, settings=settings, rev_parse_func=rev_parse
        )
    )

    assert tried[0] == "ops/2.3.2"
    assert result["ref"] == "ops/2.3.2"
    assert result["deployment"] == "dify-marketplace-ops-backend"


def test_unique_component_tag_is_found_without_configured_prefix(monkeypatch, tmp_path):
    repo = tmp_path / "svc"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "init")
    _git(repo, "tag", "backend/1.4.0")
    settings = _settings(
        monkeypatch,
        tmp_path,
        repos="svc",
        SOURCE_MCP_RUNTIME_DEPLOYMENTS='{"svc":"svc-backend"}',
        SOURCE_MCP_RUNTIME_TAG_PREFIXES="{}",
    )

    async def image(*_args: Any, **_kwargs: Any):
        return {"image_tag": "1.4.0", "environment": "production"}

    monkeypatch.setattr(runtime, "_query_runtime_image", image)

    async def rev_parse(_repo: str, ref: str, _settings: Settings) -> str:
        if ref != "backend/1.4.0":
            raise SourceError("REF_NOT_FOUND", ref)
        return "e" * 40

    result = anyio.run(
        lambda: runtime.resolve_runtime_ref(
            repo="svc", deployment=None, settings=settings, rev_parse_func=rev_parse
        )
    )

    assert result["ref"] == "backend/1.4.0"


def test_kustomize_replicas_parse():
    text = (
        "images:\n  - name: langgenius/dify-billing\n    newTag: x\n\n"
        "replicas:\n  - name: billing\n    count: 0\n  - name: worker # c\n    count: '2'\n"
        "patches:\n  - name: not-a-replica\n    count: 9\n"
    )
    assert runtime._kustomize_replicas(text) == {"billing": 0, "worker": 2}
    assert runtime._declared_replicas({"billing": 0}, "dify-billing", "e/p/saas/billing/k.yaml") == 0
    assert runtime._declared_replicas({"x": 3}, "dify-rbac", "e/p/dify/rbac/k.yaml") == 3
    assert runtime._declared_replicas({}, "dify-rbac", "e/p/dify/rbac/k.yaml") is None


def _add_scaled_to_zero_billing(repo: Path) -> None:
    target = repo / "environments/production/saas/billing/kustomization.yaml"
    target.write_text(
        "images:\n  - name: langgenius/dify-billing\n    newTag: release-20260616.1\n"
        "replicas:\n  - name: billing\n    count: 0\n"
    )
    _git(repo, "commit", "-qam", "scale billing to zero")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")


def test_scaled_to_zero_overlay_is_not_a_runtime_version(monkeypatch, tmp_path):
    _add_scaled_to_zero_billing(_gitops_repo(tmp_path))
    settings = _settings(monkeypatch, tmp_path)

    async def no_logs(*_args: Any, **_kwargs: Any):
        return None

    monkeypatch.setattr(runtime, "_query_runtime_image", no_logs)

    async def rev_parse(*_args: Any) -> str:
        raise AssertionError("a scaled-to-zero tag must not be resolved")

    with pytest.raises(RuntimeRefUnavailable) as exc_info:
        anyio.run(
            lambda: runtime.resolve_runtime_ref(
                repo="dify-billing", deployment=None, settings=settings, rev_parse_func=rev_parse
            )
        )

    message = exc_info.value.message
    assert "0 replicas" in message
    assert "dify-saas" in message  # the retirement note points to the new repo


def test_sandbox_unavailable_explains_app_runner(monkeypatch, tmp_path):
    _gitops_repo(tmp_path)
    settings = _settings(monkeypatch, tmp_path)

    async def no_logs(*_args: Any, **_kwargs: Any):
        return None

    monkeypatch.setattr(runtime, "_query_runtime_image", no_logs)

    with pytest.raises(RuntimeRefUnavailable, match="App Runner"):
        anyio.run(
            lambda: runtime.resolve_runtime_ref(
                repo="dify-sandbox", deployment=None, settings=settings, rev_parse_func=None
            )
        )


def test_saas_billing_maps_to_dify_saas(monkeypatch):
    monkeypatch.delenv("SOURCE_MCP_RUNTIME_DEPLOYMENTS", raising=False)
    settings = Settings(_env_file=None)

    assert runtime.configured_deployments(settings, "dify-saas") == ["dify-saas-billing"]
    assert "dify-gateway" in settings.repo_component_deployments["dify-saas"]
