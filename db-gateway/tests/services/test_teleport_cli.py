"""teleport_cli: shadow TELEPORT_HOME and the tsh subprocess layer."""
import asyncio
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from db_gateway.postgres import TeleportAuthExpired
from db_gateway.teleport import TshStatus
from db_gateway import teleport_cli
from db_gateway.teleport_cli import (
    TeleportNotAuthorized,
    TshTimeout,
    ensure_shadow_home,
    run_tsh,
    run_tsh_with_login,
)
from tsh_helpers import snapshot, write_tsh_src


async def test_shadow_copies_without_app_kube_db_bin(cfg):
    shadow = Path(await ensure_shadow_home(cfg))
    assert shadow == Path(cfg.teleport_tools.shadow_home)
    k = shadow / "keys" / "teleport.dify.dev"
    assert (k / "u.crt").read_text() == "cert-1" and (shadow / "current-profile").exists()
    assert (k / "u-ssh" / "teleport.dify.dev-cert.pub").exists() and (k / "cas").is_dir()
    assert not any((k / d).exists() for d in ("u-app", "u-kube", "u-db"))
    assert not (shadow / "bin").exists()


async def test_shadow_not_recopied_when_signature_unchanged(cfg):
    shadow = Path(await ensure_shadow_home(cfg))
    (shadow / "keys/teleport.dify.dev/u-app").mkdir()   # tsh 在副本里签发的 app 证书
    await ensure_shadow_home(cfg)
    assert (shadow / "keys/teleport.dify.dev/u-app").exists()


async def test_shadow_resyncs_after_renewal(cfg):
    shadow = Path(await ensure_shadow_home(cfg))
    (shadow / "keys/teleport.dify.dev/u-app").mkdir()
    write_tsh_src(Path(cfg.teleport.auth_home), cert="cert-2")
    await ensure_shadow_home(cfg)
    assert (shadow / "keys/teleport.dify.dev/u.crt").read_text() == "cert-2"
    assert not (shadow / "keys/teleport.dify.dev/u-app").exists()


async def test_shadow_kept_during_renewal_window(cfg):
    shadow = await ensure_shadow_home(cfg)
    shutil.rmtree(Path(cfg.teleport.auth_home) / "keys")
    (Path(cfg.teleport.auth_home) / "current-profile").unlink()
    assert await ensure_shadow_home(cfg) == shadow
    assert (Path(shadow) / "keys/teleport.dify.dev/u.crt").read_text() == "cert-1"


async def test_no_source_and_no_shadow_is_expired(cfg):
    shutil.rmtree(Path(cfg.teleport.auth_home) / "keys")
    with pytest.raises(TeleportAuthExpired):
        await ensure_shadow_home(cfg)


async def test_shadow_sync_does_not_touch_source(cfg):
    before = snapshot(cfg.teleport.auth_home)
    await ensure_shadow_home(cfg)
    write_tsh_src(Path(cfg.teleport.auth_home), cert="cert-2")
    before2 = snapshot(cfg.teleport.auth_home)
    await ensure_shadow_home(cfg)
    assert snapshot(cfg.teleport.auth_home) == before2 and before != before2


# --- tsh subprocess layer -------------------------------------------------------


def dl(seconds):
    return time.monotonic() + seconds


def _alive(pid):
    """False when the process is gone or a zombie."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat.rsplit(")", 1)[1].split()[0] != "Z"


async def test_run_tsh_env(cfg, fake_tsh, monkeypatch):
    for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY", "no_proxy"):
        monkeypatch.setenv(k, "http://127.0.0.1:7890")
    fake_tsh.push(stdout="ok\n")
    r = await run_tsh(["status"], cfg=cfg, deadline=dl(5))
    assert (r.exit_code, r.stdout, r.timed_out, r.truncated) == (0, "ok\n", False, False)
    call = fake_tsh.calls[0]
    assert call["argv"] == ["status"]
    assert call["env"]["TELEPORT_HOME"] == cfg.teleport_tools.shadow_home
    assert call["env"]["KUBECONFIG"] == cfg.teleport_tools.kube.kubeconfig_path
    assert call["env"]["TELEPORT_TOOLS_VERSION"] == "off"
    assert not [k for k in call["env"]
                if k.lower() in ("http_proxy", "https_proxy", "all_proxy", "no_proxy")]
    assert Path(cfg.teleport_tools.kube.kubeconfig_path).parent.is_dir()


async def test_run_tsh_passes_shell_metacharacters_verbatim(cfg, fake_tsh):
    fake_tsh.push()
    await run_tsh(["aws", "--filter-pattern", '"ERROR"; rm -rf / $(x)'], cfg=cfg, deadline=dl(5))
    assert fake_tsh.calls[0]["argv"][2] == '"ERROR"; rm -rf / $(x)'


async def test_run_tsh_caps_stdout_and_decodes_bad_bytes(cfg, fake_tsh):
    fake_tsh.push(stdout_bytes=b"\xff" + b"a" * 5000)
    r = await run_tsh(["x"], cfg=cfg, deadline=dl(5), max_output_bytes=1000)
    assert r.truncated is True and len(r.stdout.encode()) <= 1002 and r.exit_code == 0


async def test_run_tsh_strips_ansi(cfg, fake_tsh):
    fake_tsh.push(stderr="\x1b[31mERROR: \x1b[0mexit status 255\n", code=1)
    r = await run_tsh(["x"], cfg=cfg, deadline=dl(5))
    assert (r.exit_code, r.stderr) == (1, "ERROR: exit status 255\n")


async def test_deadline_returns_partial(cfg, fake_tsh):
    fake_tsh.push(stdout="first\n", sleep=30)
    r = await run_tsh(["x"], cfg=cfg, deadline=dl(1))
    assert r.timed_out is True and r.exit_code is None and r.stdout == "first\n"
    assert r.elapsed_ms < 4000


async def test_deadline_kills_process_group(cfg, fake_tsh):
    fake_tsh.push(stdout="first\n", child_sleep=30, sleep=30)   # 像 tsh aws 那样带一个子进程
    t0 = time.monotonic()
    r = await run_tsh(["aws"], cfg=cfg, deadline=dl(1))
    assert r.timed_out and r.stdout == "first\n" and time.monotonic() - t0 < 4
    assert not _alive(fake_tsh.child_pid())


async def test_past_deadline_does_not_spawn(cfg, fake_tsh):
    r = await run_tsh(["x"], cfg=cfg, deadline=time.monotonic() - 1)
    assert r.timed_out and r.exit_code is None and fake_tsh.calls == []


async def test_source_never_written(cfg, fake_tsh):
    await ensure_shadow_home(cfg)
    before = snapshot(cfg.teleport.auth_home)
    target = f"{cfg.teleport_tools.shadow_home}/keys/teleport.dify.dev/u.crt"
    fake_tsh.push(write_file=[target, "rewritten"])          # 模拟 tsh 重写 key ring
    await run_tsh(["apps", "login", "a"], cfg=cfg, deadline=dl(5))
    assert snapshot(cfg.teleport.auth_home) == before
    assert Path(target).read_text() == "rewritten"


async def test_login_then_retry(cfg, fake_tsh):
    fake_tsh.push(stderr="no creds", code=1)    # 第一次执行
    fake_tsh.push()                              # 登录
    fake_tsh.push(stdout="done")                 # 重试
    r = await run_tsh_with_login(["aws", "x"], cfg=cfg, deadline=dl(10),
                                 login_args=["apps", "login", "a"],
                                 needs_login=lambda res: res.exit_code != 0)
    assert r.stdout == "done"
    assert [c["argv"] for c in fake_tsh.calls] == [["aws", "x"], ["apps", "login", "a"], ["aws", "x"]]


async def test_concurrent_calls_login_once(cfg, fake_tsh):
    fake_tsh.until_login(code=1)
    await asyncio.gather(*[run_tsh_with_login(["aws", str(i)], cfg=cfg, deadline=dl(10),
        login_args=["apps", "login", "a"], needs_login=lambda r: r.exit_code != 0)
        for i in range(5)])
    assert sum(c["argv"][:2] == ["apps", "login"] for c in fake_tsh.calls) == 1


async def test_login_timeout_uses_remaining_time(cfg, fake_tsh):
    fake_tsh.push(code=1); fake_tsh.push(sleep=30)          # 登录卡住
    t0 = time.monotonic()
    with pytest.raises(TshTimeout):
        await run_tsh_with_login(["aws"], cfg=cfg, deadline=dl(2),
                                 login_args=["apps", "login", "a"], needs_login=lambda r: True)
    assert time.monotonic() - t0 < 4


async def test_login_failure_without_session_is_expired(cfg, fake_tsh):
    fake_tsh.push(code=1)
    fake_tsh.push(stderr="ERROR: No proxy address specified, missed --proxy flag?", code=1)
    with pytest.raises(TeleportAuthExpired):
        await run_tsh_with_login(["aws"], cfg=cfg, deadline=dl(10),
                                 login_args=["apps", "login", "a"], needs_login=lambda r: True)


async def test_login_failure_with_session_is_not_authorized(cfg, fake_tsh, monkeypatch):
    status = TshStatus(user="u", cluster="c", roles=["production-read-all"],
                       valid_until=datetime.now(timezone.utc), remaining=timedelta(hours=1))
    get_status = AsyncMock(return_value=status)
    monkeypatch.setattr("db_gateway.teleport_cli.get_tsh_status", get_status)
    fake_tsh.push(code=1); fake_tsh.push(stderr='ERROR: app "a" not found', code=1)
    with pytest.raises(TeleportNotAuthorized, match="production-read-all"):
        await run_tsh_with_login(["aws"], cfg=cfg, deadline=dl(10),
                                 login_args=["apps", "login", "a"], needs_login=lambda r: True)
    assert get_status.call_args.kwargs["auth_home"] == cfg.teleport.auth_home


# --- final review fixes -----------------------------------------------------------


async def test_shadow_heals_when_old_cert_is_written_back(cfg):
    shadow = Path(await ensure_shadow_home(cfg))
    write_tsh_src(Path(cfg.teleport.auth_home), cert="cert-2")
    await ensure_shadow_home(cfg)
    crt = shadow / "keys/teleport.dify.dev/u.crt"
    crt.write_text("cert-1")            # 续期前启动的 tsh 把旧 key ring 写回了新副本
    await ensure_shadow_home(cfg)
    assert crt.read_text() == "cert-2"


async def test_shadow_copy_retries_when_source_changes_midway(cfg, monkeypatch):
    src_key = Path(cfg.teleport.auth_home) / "keys/teleport.dify.dev/u.key"
    real_copytree = shutil.copytree
    calls = []

    def copytree(src, *a, **kw):
        result = real_copytree(src, *a, **kw)
        if str(src) == cfg.teleport.auth_home:      # 只看顶层调用（copytree 会递归调用自己）
            if not calls:
                src_key.write_text("key-2")        # watchdog/tsh 在复制过程中改写了私钥
            calls.append(1)
        return result

    monkeypatch.setattr(teleport_cli.shutil, "copytree", copytree)
    shadow = Path(await ensure_shadow_home(cfg))
    assert (shadow / "keys/teleport.dify.dev/u.key").read_text() == "key-2" and len(calls) == 2


def test_swap_failure_keeps_previous_shadow(tmp_path):
    shadow = tmp_path / "shadow"
    (shadow / "keys").mkdir(parents=True)
    (shadow / "keys" / "u.crt").write_text("old")
    ok = teleport_cli._swap_in(tmp_path / "missing-tmp", shadow, tmp_path / "shadow.old")
    assert ok is False and (shadow / "keys" / "u.crt").read_text() == "old"


async def test_login_timeout_with_valid_session_is_timeout(cfg, fake_tsh, monkeypatch):
    status = TshStatus(user="u", cluster="c", roles=["support-lambda-logs"],
                       valid_until=datetime.now(timezone.utc), remaining=timedelta(hours=1))
    monkeypatch.setattr("db_gateway.teleport_cli.get_tsh_status", AsyncMock(return_value=status))
    fake_tsh.push(code=1); fake_tsh.push(sleep=30)
    with pytest.raises(TshTimeout):
        await run_tsh_with_login(["aws"], cfg=cfg, deadline=dl(1),
                                 login_args=["apps", "login", "a"], needs_login=lambda r: True)
