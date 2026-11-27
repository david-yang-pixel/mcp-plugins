"""kube_read: read-only kubectl through tsh kubectl."""
import asyncio
import time
from pathlib import Path

import pytest
import yaml

from db_gateway.kube_read import execute_kube_read, resolve_context, validate_kubectl_args

PROD = "teleport.dify.dev-lg-eks-01-us-east-1-523286616386"
STAGING = "teleport.dify.dev-dify-plugin-1-eks-us-east-1-014498624057"


def kubeconfig_text(names):
    return yaml.safe_dump({
        "apiVersion": "v1", "kind": "Config", "current-context": names[0],
        "clusters": [{"name": "teleport.dify.dev", "cluster": {"server": "https://teleport.dify.dev:443"}}],
        "users": [{"name": n, "user": {}} for n in names],
        "contexts": [{"name": n, "context": {"cluster": "teleport.dify.dev", "user": n}} for n in names],
    })


def write_kubeconfig(path, names):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(kubeconfig_text(names))


KUBECONFIG_TEXT = kubeconfig_text([PROD, STAGING])


@pytest.fixture
def kubeconfig(cfg):
    write_kubeconfig(cfg.teleport_tools.kube.kubeconfig_path, [PROD, STAGING])


@pytest.mark.parametrize("args", [
    ["get", "pods", "-n", "dify-plugin", "-o", "wide"],
    ["get", "pods", "-A", "-l", "app=dify-api-oltp", "--no-headers"],
    ["get", "pods", "-nfoo", "-owide"],              # 值紧跟短参数
    ["get", "pods", "-n", "kube-system", "-L", "app"],
    ["describe", "pod", "x", "-n", "dify-plugin"],
    ["logs", "deploy/dify-api-oltp", "-n", "dify-plugin", "--since=10m", "--tail=500", "-c", "api", "-p"],
    ["auth", "can-i", "get", "pods", "-n", "dify"],
    ["get", "pods", "--", "-f"],                      # -- 之后不检查
    ["version"], ["api-resources"], ["explain", "deployment.spec"],
])
def test_allowed_flags(args):
    assert validate_kubectl_args(args) == args


@pytest.mark.parametrize("args", [
    ["delete", "pod", "x"], ["exec", "x", "--", "sh"], ["apply", "-f", "x.yaml"],
    ["patch", "deploy", "x"], ["port-forward", "x", "80"], ["cp", "a", "b"], ["edit", "deploy", "x"],
    ["auth", "reconcile"], ["config", "view"], ["-n", "x", "get", "pods"],
    ["get", "-f", "/app/.env"], ["get", "-f/app/.env"], ["get", "--filename=/app/.env"],
    ["get", "-k", "."], ["logs", "x", "-f"], ["logs", "x", "--follow"], ["logs", "x", "--follow=true"],
    ["get", "pods", "-w"], ["get", "pods", "-Aw"], ["get", "pods", "--watch=true"], ["get", "pods", "--watch-only"],
    ["get", "pods", "--as=admin"], ["get", "pods", "--as-group", "system:masters"],
    ["get", "pods", "--context", "other"], ["get", "pods", "--kubeconfig=/tmp/x"],
    ["get", "pods", "--server=https://evil"], ["get", "pods", "-s", "https://evil"],
    ["get", "pods", "--token=abc"], ["get", "pods", "--cache-dir=/tmp/x"],
    ["get", "pods", "--profile=cpu", "--profile-output=/app/x"],
    [], "get pods", ["get", 1], ["get"] * 65, ["get", "a\x00b"],
])
def test_blocked_flags(args):
    with pytest.raises(ValueError):
        validate_kubectl_args(args)


def test_resolve_context(tmp_path):
    kc = tmp_path / "kubeconfig"
    assert resolve_context(str(kc), proxy="teleport.dify.dev:443", cluster_name="lg-eks-01") is None
    write_kubeconfig(kc, [PROD, "teleport.dify.dev-dify-plugin-1-eks-us-east-1-014498624057"])
    assert resolve_context(str(kc), proxy="teleport.dify.dev:443", cluster_name="lg-eks-01") == PROD
    assert resolve_context(str(kc), proxy="teleport.dify.dev:443", cluster_name="nope") is None
    write_kubeconfig(kc, [PROD, PROD + "-copy"])
    assert resolve_context(str(kc), proxy="teleport.dify.dev:443", cluster_name="lg-eks-01") is None


async def test_kube_read_inserts_context_after_verb(cfg, fake_tsh, kubeconfig):
    fake_tsh.push(stdout="NAME READY\n")
    out = await execute_kube_read(cfg=cfg, cluster="prod", args=["get", "pods", "-n", "dify-plugin"])
    assert out["success"] and out["output"] == "NAME READY\n" and out["context"] == PROD
    assert fake_tsh.calls[0]["argv"] == ["kubectl", "get", "--context", PROD, "pods", "-n", "dify-plugin"]
    assert fake_tsh.calls[0]["env"]["TELEPORT_HOME"] == cfg.teleport_tools.shadow_home


async def test_kube_read_logs_in_when_kubeconfig_missing(cfg, fake_tsh):
    fake_tsh.until_login(code=0, on_login=(cfg.teleport_tools.kube.kubeconfig_path, KUBECONFIG_TEXT))
    fake_tsh.push(stdout="ok")
    out = await execute_kube_read(cfg=cfg, cluster="prod", args=["get", "pods"])
    assert out["success"]
    assert ["kube", "login", "--all"] in [c["argv"] for c in fake_tsh.calls]


async def test_kube_first_login_once_under_concurrency(cfg, fake_tsh):
    fake_tsh.until_login(code=0, on_login=(cfg.teleport_tools.kube.kubeconfig_path, KUBECONFIG_TEXT))
    await asyncio.gather(*[execute_kube_read(cfg=cfg, cluster="prod", args=["get", "pods"])
                           for _ in range(3)])
    assert sum(c["argv"][:2] == ["kube", "login"] for c in fake_tsh.calls) == 1


async def test_kube_login_shares_deadline(cfg, fake_tsh):
    cfg.teleport_tools.time_budget_seconds = 2
    fake_tsh.push(sleep=30)                                   # kube login 卡住
    t0 = time.monotonic()
    out = await execute_kube_read(cfg=cfg, cluster="prod", args=["get", "pods"])
    assert out["code"] == "TSH_TIMEOUT" and time.monotonic() - t0 < 4


async def test_kube_read_relogs_in_on_credential_error(cfg, fake_tsh, kubeconfig):
    fake_tsh.push(code=0, stderr="Unable to connect to the server: getting credentials: exec: ...")
    fake_tsh.push(); fake_tsh.push(stdout="ok")
    out = await execute_kube_read(cfg=cfg, cluster="prod", args=["get", "pods"])
    assert out["success"] and out["output"] == "ok" and len(fake_tsh.calls) == 3


async def test_kube_read_forbidden_is_kubectl_error_without_login(cfg, fake_tsh, kubeconfig):
    fake_tsh.push(code=0, stderr='Error from server (Forbidden): secrets is forbidden: User "u" cannot list')
    out = await execute_kube_read(cfg=cfg, cluster="prod", args=["get", "secrets", "-n", "dify-plugin"])
    assert (out["success"], out["code"], out["exit_code"]) == (False, "KUBECTL_ERROR", 0)
    assert "Forbidden" in out["error"] and len(fake_tsh.calls) == 1


@pytest.mark.parametrize("stderr", [
    "error: unknown flag: --no-such-flag\nSee 'kubectl get --help' for usage.\n",
    'Error from server (NotFound): deployments.apps "x" not found\n',
])
async def test_kube_read_other_errors_with_exit_zero(cfg, fake_tsh, kubeconfig, stderr):
    fake_tsh.push(code=0, stderr=stderr)
    out = await execute_kube_read(cfg=cfg, cluster="prod", args=["get", "deploy", "x"])
    assert out["code"] == "KUBECTL_ERROR"


async def test_kube_read_no_resources_is_success(cfg, fake_tsh, kubeconfig):
    fake_tsh.push(code=0, stderr="No resources found in dify-plugin namespace.\n")
    out = await execute_kube_read(cfg=cfg, cluster="prod", args=["get", "pods", "-n", "dify-plugin"])
    assert out["success"] and out["stderr"].startswith("No resources found")


async def test_kube_read_rejects_before_running(cfg, fake_tsh, kubeconfig):
    for cluster, args in (("prod", ["delete", "pod", "x"]), ("nope", ["get", "pods"])):
        out = await execute_kube_read(cfg=cfg, cluster=cluster, args=args)
        assert out["code"] == "INVALID_ARGUMENT"
    assert fake_tsh.calls == []


async def test_kube_read_truncates_and_flags(cfg, fake_tsh, kubeconfig):
    cfg.teleport_tools.kube.max_output_bytes = 100
    fake_tsh.push(stdout="x" * 5000)
    out = await execute_kube_read(cfg=cfg, cluster="prod", args=["logs", "p"])
    assert out["success"] and out["truncated"] is True and len(out["output"]) == 100 and out["warnings"]


async def test_kube_read_timeout_returns_partial(cfg, fake_tsh, kubeconfig):
    cfg.teleport_tools.time_budget_seconds = 1
    fake_tsh.push(stdout="line1\n", sleep=30)
    out = await execute_kube_read(cfg=cfg, cluster="prod", args=["logs", "p"])
    assert out["success"] and out["timed_out"] is True and out["output"] == "line1\n"


async def test_kube_read_audit_has_args_but_no_output(cfg, fake_tsh, kubeconfig):
    fake_tsh.push(stdout="CUSTOMER-DATA")
    await execute_kube_read(cfg=cfg, cluster="prod", args=["logs", "p", "-n", "dify-plugin"])
    text = (Path(cfg.db_gateway.audit_log_dir) / "db_gateway_audit.log").read_text()
    assert '"tool": "kube_read"' in text and '"dify-plugin"' in text and "CUSTOMER-DATA" not in text


# --- final review fixes -----------------------------------------------------------


async def test_kube_timeout_without_output_is_tsh_timeout(cfg, fake_tsh, kubeconfig):
    cfg.teleport_tools.time_budget_seconds = 1
    fake_tsh.push(sleep=30)
    out = await execute_kube_read(cfg=cfg, cluster="prod", args=["logs", "p"])
    assert (out["success"], out["code"]) == (False, "TSH_TIMEOUT")


async def test_kube_queued_call_past_deadline_never_runs(cfg, fake_tsh, kubeconfig):
    cfg.teleport_tools.max_concurrent = 1
    cfg.teleport_tools.time_budget_seconds = 1
    fake_tsh.push(stdout="line1\n", sleep=30)
    first, second = await asyncio.gather(
        execute_kube_read(cfg=cfg, cluster="prod", args=["logs", "a"]),
        execute_kube_read(cfg=cfg, cluster="prod", args=["logs", "b"]),
    )
    assert first["success"] and first["timed_out"]
    assert (second["success"], second["code"]) == (False, "TSH_TIMEOUT") and len(fake_tsh.calls) == 1
