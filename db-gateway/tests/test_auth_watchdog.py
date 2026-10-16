"""Test auth-watchdog: throttle, auto-renew gate, mode-A/B branching."""
import importlib.util
import time
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Load the script as a module (it's not a package, so use spec_from_file_location)
SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "auth-watchdog.py"


def _load_watchdog_module():
    spec = importlib.util.spec_from_file_location("auth_watchdog", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def wd_module():
    mod = _load_watchdog_module()
    # Ensure clean throttle state per-test
    mod._LAST_PUSH_TS.clear()
    return mod


@pytest.fixture
def fake_cfg():
    cfg = MagicMock()
    cfg.teleport.proxy = "teleport.dify.dev"
    cfg.teleport.user = "yangyonggang"
    cfg.teleport.auth_home = "/opt/zd-agent/.tsh"
    cfg.teleport.warn_threshold_minutes = 30
    cfg.teleport.check_interval_seconds = 300
    cfg.teleport.notify_interval_seconds = 3600
    cfg.feishu.webhook_url = "https://feishu.test/hook"
    cfg.feishu.webhook_secret = ""
    return cfg


@pytest.fixture
def fake_feishu():
    f = MagicMock()
    f.send_alert = AsyncMock()
    f.send_auth_renew = AsyncMock()
    return f


def _env(*, password="", totp_secret=""):
    e = MagicMock()
    e.teleport_password = password
    e.teleport_totp_secret = totp_secret
    return e


# ─────────── Throttle ───────────

def test_should_push_first_time_passes(wd_module):
    assert wd_module._should_push("a", 3600) is True


def test_should_push_immediate_repeat_blocked(wd_module):
    wd_module._should_push("a", 3600)
    assert wd_module._should_push("a", 3600) is False


def test_should_push_different_reasons_independent(wd_module):
    assert wd_module._should_push("a", 3600) is True
    assert wd_module._should_push("b", 3600) is True


def test_should_push_after_window_expires(wd_module):
    wd_module._should_push("a", 3600)
    # Force timestamp into the past
    wd_module._LAST_PUSH_TS["a"] = time.time() - 4000
    assert wd_module._should_push("a", 3600) is True


# ─────────── OTP generation ───────────

def test_generate_otp_returns_6_digits(wd_module):
    # JBSWY3DPEHPK3PXP is the canonical pyotp test secret
    code = wd_module._generate_otp("JBSWY3DPEHPK3PXP")
    assert code is not None
    assert len(code) == 6
    assert code.isdigit()


def test_generate_otp_invalid_secret_returns_none(wd_module):
    # base32 only contains A-Z and 2-7; '!' will trigger pyotp error
    code = wd_module._generate_otp("INVALID!!!")
    assert code is None


# ─────────── auto-renew gate ───────────

@pytest.mark.asyncio
async def test_auto_renew_skipped_when_secrets_missing(wd_module, fake_cfg):
    env = _env(password="", totp_secret="")
    ok, msg = await wd_module._try_auto_renew(fake_cfg, env)
    assert ok is False
    assert "not configured" in msg


@pytest.mark.asyncio
async def test_auto_renew_calls_run_tsh_login_when_configured(wd_module, fake_cfg):
    env = _env(password="pw", totp_secret="JBSWY3DPEHPK3PXP")
    fake_run = AsyncMock(return_value=(True, "tsh login succeeded"))
    with patch.object(wd_module, "run_tsh_login", fake_run):
        ok, msg = await wd_module._try_auto_renew(fake_cfg, env)
    assert ok is True
    fake_run.assert_awaited_once()
    args = fake_run.await_args.kwargs
    assert args["password"] == "pw"
    assert len(args["otp"]) == 6


# ─────────── check_and_notify branching ───────────

@pytest.mark.asyncio
async def test_check_skips_when_cert_still_valid(wd_module, fake_cfg, fake_feishu):
    env = _env(password="pw", totp_secret="JBSWY3DPEHPK3PXP")
    status = MagicMock()
    status.remaining = timedelta(hours=2)  # well above 30min threshold
    with patch.object(wd_module, "get_tsh_status", AsyncMock(return_value=status)):
        await wd_module.check_and_notify(fake_cfg, env, fake_feishu, "secret")
    fake_feishu.send_alert.assert_not_awaited()
    fake_feishu.send_auth_renew.assert_not_awaited()


@pytest.mark.asyncio
async def test_mode_a_auto_renew_silent_on_success(wd_module, fake_cfg, fake_feishu):
    """配了 secrets + 续期成功 → 完全不推飞书。"""
    env = _env(password="pw", totp_secret="JBSWY3DPEHPK3PXP")
    status = MagicMock()
    status.remaining = timedelta(minutes=5)  # below threshold → needs renew
    with patch.object(wd_module, "get_tsh_status", AsyncMock(return_value=status)), \
         patch.object(wd_module, "run_tsh_login", AsyncMock(return_value=(True, "ok"))):
        await wd_module.check_and_notify(fake_cfg, env, fake_feishu, "secret")
    fake_feishu.send_alert.assert_not_awaited()
    fake_feishu.send_auth_renew.assert_not_awaited()


@pytest.mark.asyncio
async def test_mode_a_renew_failure_sends_alert(wd_module, fake_cfg, fake_feishu):
    """续期失败时推飞书 alert（首次必推）。"""
    env = _env(password="pw", totp_secret="JBSWY3DPEHPK3PXP")
    status = MagicMock()
    status.remaining = timedelta(minutes=5)
    with patch.object(wd_module, "get_tsh_status", AsyncMock(return_value=status)), \
         patch.object(wd_module, "run_tsh_login", AsyncMock(return_value=(False, "bad password"))):
        await wd_module.check_and_notify(fake_cfg, env, fake_feishu, "secret")
    fake_feishu.send_alert.assert_awaited_once()
    msg = fake_feishu.send_alert.await_args.kwargs.get("message", "")
    assert "bad password" in msg
    fake_feishu.send_auth_renew.assert_not_awaited()  # 不走 manual 路径


@pytest.mark.asyncio
async def test_mode_a_renew_failure_throttled(wd_module, fake_cfg, fake_feishu):
    """续期失败首次推；同一周期再失败被 throttle。"""
    env = _env(password="pw", totp_secret="JBSWY3DPEHPK3PXP")
    status = MagicMock()
    status.remaining = timedelta(minutes=5)
    with patch.object(wd_module, "get_tsh_status", AsyncMock(return_value=status)), \
         patch.object(wd_module, "run_tsh_login", AsyncMock(return_value=(False, "x"))):
        await wd_module.check_and_notify(fake_cfg, env, fake_feishu, "secret")
        await wd_module.check_and_notify(fake_cfg, env, fake_feishu, "secret")
    # 仅推一次
    assert fake_feishu.send_alert.await_count == 1


@pytest.mark.asyncio
async def test_mode_b_fallback_when_secrets_missing(wd_module, fake_cfg, fake_feishu):
    """没配 secrets → 退回 manual 模式（推 auth URL 卡片）。"""
    env = _env(password="", totp_secret="")
    status = MagicMock()
    status.remaining = timedelta(minutes=5)
    with patch.object(wd_module, "get_tsh_status", AsyncMock(return_value=status)), \
         patch.object(wd_module, "_issue_auth_url", AsyncMock(return_value="https://x/auth/abc")):
        await wd_module.check_and_notify(fake_cfg, env, fake_feishu, "secret")
    fake_feishu.send_auth_renew.assert_awaited_once()
    fake_feishu.send_alert.assert_not_awaited()


@pytest.mark.asyncio
async def test_check_handles_tsh_status_exception(wd_module, fake_cfg, fake_feishu):
    """get_tsh_status 抛异常时 watchdog 不崩，下个周期继续。"""
    env = _env(password="pw", totp_secret="JBSWY3DPEHPK3PXP")
    with patch.object(wd_module, "get_tsh_status",
                      AsyncMock(side_effect=RuntimeError("boom"))):
        await wd_module.check_and_notify(fake_cfg, env, fake_feishu, "secret")
    # 没崩；不推任何东西
    fake_feishu.send_alert.assert_not_awaited()
    fake_feishu.send_auth_renew.assert_not_awaited()


@pytest.mark.asyncio
async def test_successful_renew_clears_throttle(wd_module, fake_cfg, fake_feishu):
    """续期成功后 _LAST_PUSH_TS 应被清空，让后续故障可立即提醒。"""
    env = _env(password="pw", totp_secret="JBSWY3DPEHPK3PXP")
    # 先污染 throttle
    wd_module._LAST_PUSH_TS["something"] = time.time()
    status = MagicMock()
    status.remaining = timedelta(minutes=5)
    with patch.object(wd_module, "get_tsh_status", AsyncMock(return_value=status)), \
         patch.object(wd_module, "run_tsh_login", AsyncMock(return_value=(True, "ok"))):
        await wd_module.check_and_notify(fake_cfg, env, fake_feishu, "secret")
    assert wd_module._LAST_PUSH_TS == {}
