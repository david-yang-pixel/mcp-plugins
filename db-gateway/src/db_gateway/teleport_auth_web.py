"""Teleport auth web relay — allows remote password+OTP login via HTTPS.

Flow:
1. A one-time link is minted and pushed to Feishu:
   * by db-gateway itself when its automatic login stopped (maintenance.py) —
     the token renews db-gateway's own session (``SCOPE_SESSION``);
   * by the legacy tsh-auth-watchdog through ``/internal/teleport-auth/issue`` —
     the token renews the host session at ``teleport.auth_home`` (``SCOPE_HOST``),
     which the host tunnels use until step 2 moves them into db-gateway.
2. User opens URL on phone → GET /teleport-auth?t=<token> → HTML form
3. User types password + OTP → POST /teleport-auth
4. Server validates token + runs `tsh login` via PTY for the token's session
5. Success consumes the token; a wrong code keeps it for a retry

⚠️ Security: password flows through the HTTPS endpoint. Accept this tradeoff
knowingly — the official headless flow (cluster-level config) keeps passwords
on the user's device.
"""
from __future__ import annotations

import hmac
import os
from html import escape
from typing import Optional
from urllib.parse import urlparse

from aiohttp import web

from db_gateway.log import get_logger
from db_gateway.app_context import app_config, teleport_auth_token_store
from db_gateway.teleport_tokens import MAX_FAILED_LOGINS, SCOPE_HOST, SCOPE_SESSION, TokenStore

# Links in db-gateway's own alerts stay valid until the next (hourly) reminder.
SESSION_LINK_TTL_MINUTES = 60
# One manual login at a time (a double submit would send the same code twice,
# and two host-session logins would rewrite the same auth_home together).
_manual_login_running = False
_TARGET_LABELS = {
    SCOPE_SESSION: "db-gateway 自管会话",
    SCOPE_HOST: "宿主机 tsh 会话（tsh-proxy 隧道使用）",
}

logger = get_logger(module="services.teleport_auth_web")


# ====================== HTML form page ======================

_FORM_HTML = """<!DOCTYPE html>
<html lang="zh-cn">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Teleport 认证</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, 'SF Pro', sans-serif;
         background: #f5f5f7; margin: 0; padding: 24px; }
  .card { max-width: 480px; margin: 40px auto; background: white;
          border-radius: 16px; padding: 32px; box-shadow: 0 4px 24px rgba(0,0,0,.08); }
  h1 { margin: 0 0 8px; font-size: 20px; color: #1d1d1f; }
  .sub { color: #6e6e73; font-size: 14px; margin-bottom: 24px; }
  label { display: block; font-size: 13px; color: #3a3a3c; margin-bottom: 6px; font-weight: 500; }
  input { width: 100%; padding: 12px 14px; font-size: 16px; border: 1px solid #d2d2d7;
          border-radius: 10px; box-sizing: border-box; margin-bottom: 16px; }
  input:focus { outline: none; border-color: #007aff; }
  button { width: 100%; padding: 14px; font-size: 16px; font-weight: 600;
           background: #007aff; color: white; border: none; border-radius: 10px; }
  button:disabled { background: #c7c7cc; }
  .warn { background: #fff3cd; border-left: 3px solid #ffc107; padding: 12px;
          border-radius: 8px; margin-bottom: 20px; font-size: 13px; color: #664d03; }
  .info { font-size: 12px; color: #8e8e93; margin-top: 16px; }
</style>
</head>
<body>
<div class="card">
  <h1>🔐 Teleport 证书续期</h1>
  <p class="sub">服务器：__SERVER__ &nbsp;·&nbsp; 用户：__USER__<br>续期对象：__TARGET__</p>
  <div class="warn">
    密码与 OTP 码将通过 HTTPS 发送到服务器，由本站点直接调用 <code>tsh login</code>。
    只输入一次，用完丢弃；链接一次性使用，过期后失效。
  </div>
  <form method="post" action="/teleport-auth">
    <input type="hidden" name="t" value="__TOKEN__">
    <label>Teleport 密码</label>
    <input type="password" name="password" required autocomplete="current-password"
           autofocus placeholder="你的 Teleport 登录密码">
    <label>OTP 码 (Google Authenticator)</label>
    <input type="text" name="otp" required pattern="[0-9]{6}" maxlength="6"
           inputmode="numeric" placeholder="6 位数字" autocomplete="one-time-code">
    <button type="submit">认证</button>
  </form>
  <p class="info">Token 过期或已使用会失败。提交后约 10-20 秒完成。</p>
</div>
</body>
</html>"""


_RESULT_HTML = """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  body {{ font-family: -apple-system, sans-serif; background: #f5f5f7; margin: 0; padding: 40px 24px; }}
  .card {{ max-width: 420px; margin: 40px auto; background: white; border-radius: 16px;
          padding: 40px 32px; box-shadow: 0 4px 24px rgba(0,0,0,.08); text-align: center; }}
  .emoji {{ font-size: 56px; margin-bottom: 16px; }}
  h1 {{ font-size: 22px; color: #1d1d1f; margin: 0 0 8px; }}
  p {{ color: #6e6e73; font-size: 14px; line-height: 1.6; }}
  pre {{ background: #f5f5f7; padding: 12px; border-radius: 8px; font-size: 12px;
        text-align: left; overflow-x: auto; color: #d70015; }}
</style></head>
<body><div class="card">
<div class="emoji">{emoji}</div>
<h1>{title}</h1>
<p>{message}</p>
{detail}
</div></body></html>"""


def _server_label(public_base_url: str) -> str:
    if not public_base_url:
        return "(未配置)"
    parsed = urlparse(public_base_url)
    return parsed.netloc or public_base_url


def render_form(token: str, user: str, public_base_url: str = "", scope: str = SCOPE_SESSION) -> str:
    return (
        _FORM_HTML
        .replace("__TOKEN__", escape(token, quote=True))
        .replace("__USER__", escape(user or "(未配置)"))
        .replace("__SERVER__", escape(_server_label(public_base_url)))
        .replace("__TARGET__", escape(_TARGET_LABELS.get(scope, scope)))
    )


def render_result(*, ok: bool, message: str, detail: str = "") -> str:
    if ok:
        return _RESULT_HTML.format(
            title="认证成功",
            emoji="✅",
            message=escape(message),
            detail="<p class='sub'>系统会在 30 秒内自动恢复数据库连接。</p>",
        )
    d_html = f"<pre>{escape(detail)}</pre>" if detail else ""
    return _RESULT_HTML.format(
        title="认证失败",
        emoji="❌",
        message=message,
        detail=d_html,
    )


# ====================== tsh login via pexpect ======================
# 实际逻辑抽到 db_gateway.teleport_login 共享给 watchdog auto-renewal


async def _run_tsh_login(
    *, proxy: str, user: str, password: str, otp: str,
    auth_home: str, timeout_s: int = 60,
) -> tuple[bool, str]:
    """Wrapper to keep call sites stable; delegates to shared runner."""
    from db_gateway.teleport_login import run_tsh_login
    return await run_tsh_login(
        proxy=proxy, user=user, password=password, otp=otp,
        auth_home=auth_home, timeout_s=timeout_s,
    )


async def _renew_own_session(cfg, *, password: str, otp: str) -> tuple[bool, str]:
    """Manual login into db-gateway's own session (resumes a paused auto-login)."""
    from db_gateway.teleport_session import SESSION, LoginRejected

    try:
        await SESSION.renew(cfg, reason="manual_web", password=password, otp=otp)
    except LoginRejected as e:
        if e.kind == "locked":
            return False, "locked: " + str(e)
        return False, "invalid credentials: " + str(e)
    except Exception as e:  # noqa: BLE001 - shown to the person, never the password
        return False, f"{type(e).__name__}: {e}"
    return True, "ok"


# ====================== HTTP handlers ======================

async def issue_link(app: web.Application, scope: str, *, ttl_minutes: int = 5) -> Optional[str]:
    """Mint a one-time ``/teleport-auth`` link; None without a public base URL."""
    cfg = app_config(app)
    base = getattr(cfg.teleport, "public_base_url", "")
    if not isinstance(base, str) or not base.rstrip("/"):
        logger.error("teleport_public_base_url_missing")
        return None
    store: TokenStore = teleport_auth_token_store(app)
    tok = await store.create(scope, ttl_minutes=ttl_minutes)
    logger.info("teleport_auth_token_issued", scope=scope)
    return f"{base.rstrip('/')}/teleport-auth?t={tok}"


async def handle_teleport_auth_issue(request: web.Request) -> web.Response:
    """POST /internal/teleport-auth/issue — the legacy watchdog mints a host-session link.

    Auth: X-Internal-Secret header must match env `INTERNAL_SHARED_SECRET`.
    Returns: JSON {"url": "<public-base-url>/teleport-auth?t=<tok>"}.
    """
    internal_secret = os.environ.get("INTERNAL_SHARED_SECRET", "")
    if not internal_secret:
        return web.Response(
            status=500, text="server not configured (INTERNAL_SHARED_SECRET empty)",
        )
    supplied = request.headers.get("X-Internal-Secret", "")
    if not hmac.compare_digest(supplied.encode("utf-8"), internal_secret.encode("utf-8")):
        return web.Response(status=401, text="bad internal secret")

    url = await issue_link(request.app, SCOPE_HOST)
    if url is None:
        return web.Response(
            status=500,
            text="server not configured (teleport.public_base_url empty)",
        )
    return web.json_response({"url": url, "expires_in_seconds": 300})


async def handle_teleport_auth_get(request: web.Request) -> web.Response:
    """GET /teleport-auth?t=<token> → render password+OTP form."""
    token = request.query.get("t", "")
    store: TokenStore = teleport_auth_token_store(request.app)
    entry = await store.lookup(token)
    if entry is None:
        return web.Response(
            status=403,
            text=render_result(
                ok=False, message="Token 无效或已过期",
                detail="请使用飞书里最新推送的认证链接。",
            ),
            content_type="text/html",
        )
    cfg = app_config(request.app)
    return web.Response(
        text=render_form(
            token=token,
            user=cfg.teleport.user or "",
            public_base_url=cfg.teleport.public_base_url,
            scope=entry.scope,
        ),
        content_type="text/html",
    )


async def handle_teleport_auth_post(request: web.Request) -> web.Response:
    """POST /teleport-auth → validate token (peek), run tsh login, consume on success."""
    data = await request.post()
    token = str(data.get("t", ""))
    # Strip 防止前后空白 / CRLF 污染
    password = str(data.get("password", "")).strip()
    otp = str(data.get("otp", "")).strip()
    logger.info(
        "teleport_auth_post_received",
        password_len=len(password),
        otp_len=len(otp),
        otp_all_digits=otp.isdigit(),
    )

    store: TokenStore = teleport_auth_token_store(request.app)
    # Look up first (not consume) — allows retry with same token if creds wrong
    entry = await store.lookup(token)
    if entry is None:
        return web.Response(
            status=403,
            text=render_result(
                ok=False, message="Token 无效或已过期",
                detail="返回飞书找最新的那条认证链接。",
            ),
            content_type="text/html",
        )

    if not password or not otp:
        return web.Response(
            status=400,
            text=render_result(ok=False, message="密码或 OTP 为空"),
            content_type="text/html",
        )

    cfg = app_config(request.app)
    global _manual_login_running
    if _manual_login_running:     # checked and set with no await in between
        return web.Response(
            status=409,
            text=render_result(ok=False, message="已有一次登录正在进行，请等它完成后再刷新查看，不要重复提交"),
            content_type="text/html",
        )
    _manual_login_running = True
    try:
        if entry.scope == SCOPE_HOST:
            ok, msg = await _run_tsh_login(
                proxy=cfg.teleport.proxy,
                user=cfg.teleport.user,
                password=password,
                otp=otp,
                auth_home=cfg.teleport.auth_home,
            )
        else:
            ok, msg = await _renew_own_session(cfg, password=password, otp=otp)
    finally:
        _manual_login_running = False

    # Password reference goes out of scope; do NOT log
    del password, otp

    if ok:
        # 成功才消耗 token（失败让用户重试，不用重新生成）
        await store.consume(token)
        logger.info("teleport_auth_relay_success", user=cfg.teleport.user, scope=entry.scope)
        message = ("db-gateway 的 Teleport 会话已续期，自动续期已恢复。"
                   if entry.scope == SCOPE_SESSION else "证书已续期。数据库连接会在 30 秒内自动恢复。")
        return web.Response(
            text=render_result(ok=True, message=message),
            content_type="text/html",
        )

    # 失败：保留 token，用户可以回到飞书链接重试（token 5 分钟内仍有效）
    failures = await store.record_failure(token)
    logger.warning("teleport_auth_relay_failed", scope=entry.scope, failures=failures, reason=msg[:200])
    left = MAX_FAILED_LOGINS - failures
    retry_note = (f"此链接还可再试 {left} 次。" if left > 0
                  else "此链接已连续失败 3 次并作废，请等待下一条提醒里的新链接。")

    # 把 "invalid credentials" / 锁定翻译成友好提示
    hint = ""
    if msg.startswith("locked: "):
        hint = ("<strong>Teleport 账号已被锁定</strong>。请等锁定解除（或联系管理员解锁）后"
                "再使用新的认证链接登录，不要反复重试。")
    elif "invalid credentials" in msg.lower():
        hint = (
            "<strong>密码或 OTP 错误</strong>。最常见原因：OTP 码过期了"
            "（30 秒切换）。请返回飞书卡片，等 Google Authenticator 显示"
            "<strong>新的码 + 剩余至少 25 秒</strong>时立刻填写提交。"
            + retry_note
        )
    return web.Response(
        status=200,  # 不返 500，避免浏览器报错页覆盖我们的指引
        text=render_result(
            ok=False,
            message=hint or ("tsh login 失败。" + retry_note),
            detail=msg[-800:] if not hint else "",
        ),
        content_type="text/html",
    )
