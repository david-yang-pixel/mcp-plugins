#!/usr/bin/env python3
"""Teleport Auth Watchdog.

Periodically checks `tsh status`. When the certificate is expiring soon
or already expired:

  Mode A — auto-renew  (preferred, when TELEPORT_PASSWORD + TELEPORT_TOTP_SECRET
                       are configured in .env)
    Generates fresh OTP from stored TOTP secret and runs `tsh login` via
    pexpect, fully unattended. Only pushes Feishu when renewal *fails*.

  Mode B — manual fallback  (when secrets aren't configured)
    Mints a one-time auth URL via the main app and pushes it to Feishu so
    the user can authenticate via the web form on phone.

Throttle (notify_interval_seconds): same Feishu reason pushed at most once
per N seconds; auto-renew failure shares the same throttle so server isn't
spammed if Teleport is down.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import timedelta
from pathlib import Path

import httpx

# Ensure src is importable when running the script directly
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from db_gateway.config import EnvSettings, load_config
from db_gateway.teleport import get_tsh_status
from db_gateway.teleport_login import run_tsh_login
from db_gateway.log import get_logger

logger = get_logger(module="scripts.auth-watchdog")

# Throttle: 上次按 reason 推送的 UNIX 时间戳。同 reason 距离上次 < notify_interval
# 时跳过，防止过期后每个 check 周期（5min）都推/续期一遍。
_LAST_PUSH_TS: dict[str, float] = {}


async def _issue_auth_url(internal_secret: str, cfg) -> str | None:
    """Call main app to mint a one-time token; return the public URL."""
    try:
        port = getattr(cfg.db_gateway, "listen_port", 18080)
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"http://127.0.0.1:{port}/internal/teleport-auth/issue",
                headers={"X-Internal-Secret": internal_secret},
            )
        if resp.status_code != 200:
            logger.error(
                "auth_url_issue_failed",
                status=resp.status_code, body=resp.text[:200],
            )
            return None
        return resp.json().get("url")
    except Exception as e:
        logger.error("auth_url_issue_exception", error=str(e))
        return None


def _should_push(reason: str, notify_interval_seconds: int) -> bool:
    """Throttle 判定：同 reason 距离上次推送 < notify_interval 则跳过。"""
    now = time.time()
    last = _LAST_PUSH_TS.get(reason)
    if last is not None and (now - last) < notify_interval_seconds:
        remaining_quiet = notify_interval_seconds - (now - last)
        logger.info(
            "auth_push_throttled",
            reason=reason,
            quiet_remaining_s=int(remaining_quiet),
        )
        return False
    _LAST_PUSH_TS[reason] = now
    return True


def _generate_otp(totp_secret: str) -> str | None:
    """Generate current 6-digit TOTP code from stored secret."""
    try:
        import pyotp
    except ImportError:
        logger.error("pyotp_not_installed")
        return None
    try:
        return pyotp.TOTP(totp_secret, digits=6, interval=30).now()
    except Exception as e:
        logger.error("otp_generation_failed", error=str(e))
        return None


async def _try_auto_renew(cfg, env: EnvSettings) -> tuple[bool, str]:
    """尝试用 stored password + 自动 OTP 跑 tsh login，返回 (成功?, 详情)。"""
    if not env.teleport_password or not env.teleport_totp_secret:
        return False, "auto-renew secrets not configured"

    otp = _generate_otp(env.teleport_totp_secret)
    if otp is None:
        return False, "could not generate OTP"

    logger.info("auto_renew_start", user=cfg.teleport.user, proxy=cfg.teleport.proxy)
    ok, msg = await run_tsh_login(
        proxy=cfg.teleport.proxy,
        user=cfg.teleport.user,
        password=env.teleport_password,
        otp=otp,
        auth_home=cfg.teleport.auth_home,
        timeout_s=60,
    )
    if ok:
        logger.info("auto_renew_success")
    else:
        logger.error("auto_renew_failed", message=msg[:500])
    return ok, msg


async def check_and_notify(cfg, env: EnvSettings, feishu, internal_secret: str) -> None:
    """One iteration: check status, auto-renew or push Feishu URL."""
    warn_threshold = timedelta(minutes=cfg.teleport.warn_threshold_minutes)
    notify_interval = cfg.teleport.notify_interval_seconds
    can_auto_renew = bool(env.teleport_password and env.teleport_totp_secret)

    try:
        status = await get_tsh_status(
            proxy=cfg.teleport.proxy,
            user=cfg.teleport.user,
            auth_home=cfg.teleport.auth_home,
        )
    except Exception as e:
        logger.error("tsh_status_failed", error=str(e))
        return

    needs_renew = status is None or status.remaining < warn_threshold
    if not needs_renew:
        # 证书 OK → 清掉 throttle，下次有事可立刻处理
        logger.info("tsh_ok", remaining=str(status.remaining))
        _LAST_PUSH_TS.clear()
        return

    reason = "not logged in" if status is None else "expiring soon"
    remaining = "?" if status is None else str(status.remaining)
    logger.warning(
        "tsh_needs_renew", reason=reason, remaining=remaining,
        auto_renew_available=can_auto_renew,
    )

    # === Mode A: auto-renew ===
    if can_auto_renew:
        # auto-renew 成功就什么都不做（没飞书）；失败才推飞书 alert（带 throttle）
        ok, msg = await _try_auto_renew(cfg, env)
        if ok:
            _LAST_PUSH_TS.clear()  # 续期成功 → 清空之前的 throttle 状态
            return

        # 续期失败：throttle alert
        if _should_push(f"auto_renew_failed_{reason}", notify_interval):
            try:
                await feishu.send_alert(
                    title="⚠️ Teleport 自动续期失败",
                    message=(
                        f"reason: {reason}\nremaining: {remaining}\n"
                        f"error: {msg[:400]}\n\n"
                        f"请检查 /opt/db-gateway/.env 里 TELEPORT_PASSWORD / "
                        f"TELEPORT_TOTP_SECRET 是否被改 / Teleport 集群是否可达 / "
                        f"账号是否被锁。需要的话退回手动登录页：/teleport-auth"
                    ),
                    urgent=True,
                )
            except Exception as e:
                logger.error("feishu_push_failed", error=str(e))
        return

    # === Mode B: fallback to manual ===
    if _should_push(reason, notify_interval):
        await _push_auth_url(
            feishu, cfg, internal_secret, reason=reason, remaining=remaining,
        )


async def _push_auth_url(feishu, cfg, internal_secret: str, *, reason: str, remaining: str) -> None:
    url = await _issue_auth_url(internal_secret, cfg)
    if url is None:
        logger.error("auth_url_could_not_be_issued")
        await feishu.send_alert(
            title="⚠️ Teleport Watchdog 异常",
            message=f"证书剩余 {remaining}，但无法从主 app 获取认证 URL。"
                    f"请检查 db-gateway 容器是否在线。",
            urgent=True,
        )
        return

    try:
        await feishu.send_auth_renew(url=url, user=cfg.teleport.user)
        logger.info("auth_url_pushed", reason=reason, remaining=remaining)
    except Exception as e:
        logger.error("feishu_push_failed", error=str(e))


async def main() -> None:
    cfg = load_config()
    env = EnvSettings()
    internal_secret = os.environ.get("INTERNAL_SHARED_SECRET", "")
    if not internal_secret:
        logger.warning(
            "INTERNAL_SHARED_SECRET env not set; manual auth URL fallback disabled"
        )

    from db_gateway.feishu import FeishuClient
    feishu = FeishuClient(
        webhook_url=cfg.feishu.webhook_url,
        webhook_secret=cfg.feishu.webhook_secret,
    )

    can_auto = bool(env.teleport_password and env.teleport_totp_secret)
    logger.info(
        "auth_watchdog_started",
        proxy=cfg.teleport.proxy,
        user=cfg.teleport.user,
        threshold_min=cfg.teleport.warn_threshold_minutes,
        interval_s=cfg.teleport.check_interval_seconds,
        notify_interval_s=cfg.teleport.notify_interval_seconds,
        auto_renew_enabled=can_auto,
    )

    while True:
        try:
            await check_and_notify(cfg, env, feishu, internal_secret)
        except Exception as e:
            logger.error("watchdog_iteration_error", error=str(e))
        await asyncio.sleep(cfg.teleport.check_interval_seconds)


if __name__ == "__main__":
    asyncio.run(main())
