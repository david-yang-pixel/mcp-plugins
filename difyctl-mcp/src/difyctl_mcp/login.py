"""Browser step of MCP OAuth: the user approves a Dify device login for this server.

Each pending authorization is bound to an HttpOnly cookie set on the /authorize redirect,
so only the browser that started it can continue.
"""
from __future__ import annotations

import hmac
import html
import json
import logging
import os
import re
import secrets
import socket
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from mcp.server.auth.provider import construct_redirect_uri

from .files import remove_tree
from .oauth import Provider
from .runner import Runner
from .service import Service
from .store import digest

log = logging.getLogger("difyctl_mcp.login")
COOKIE = "difyctl_login_"
USER_CODE = re.compile(r"[A-Za-z0-9-]{4,32}")
PENDING_ID = re.compile(r"[A-Za-z0-9_-]{20,64}")
EXPIRY_FIELDS = ("expires_at", "expire_at", "expired_at", "expires", "expiry")
MAX_SESSION_SECONDS = 30 * 24 * 3600
SCOPE_TEXT = {"difyctl:read": "读取（read）", "difyctl:write": "读取和修改（write）",
              "difyctl:destructive": "读取、修改和删除/撤销（destructive）"}


def csrf_token(cookie: str, pending_id: str) -> str:
    return hmac.new(cookie.encode(), ("csrf:" + pending_id).encode(), "sha256").hexdigest()


def parse_time(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value / 1000) if value > 1e12 else int(value)
    if isinstance(value, str) and value:
        if value.isdigit():
            return parse_time(int(value))
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
        except ValueError:
            return None
    return None


def session_expiry(listing, hostname: str, now: int, fallback: int) -> int:
    """Match this server's device session (current flag, else device_label = hostname)."""
    items = listing if isinstance(listing, list) else []
    if isinstance(listing, dict):
        for key in ("data", "items", "sessions", "results"):
            if isinstance(listing.get(key), list):
                items = listing[key]
                break
    matches = [i for i in items if isinstance(i, dict) and (i.get("current") is True or i.get("is_current") is True)]
    if not matches:
        matches = [i for i in items if isinstance(i, dict) and (i.get("device_label") or i.get("label")) == hostname]
    times = []
    for item in matches:
        for key in EXPIRY_FIELDS:
            value = parse_time(item.get(key))
            if value and now < value <= now + MAX_SESSION_SECONDS:
                times.append(value)
                break
    # Several sessions with the same label: the newest login lives longest. Without a match, assume 14 days.
    return max(times) if times else now + fallback


def page(title: str, body: str, *, status: int = 200, refresh: int | None = None) -> HTMLResponse:
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    content = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">{meta}<title>{html.escape(title)}</title>
<style>body{{font:16px/1.6 system-ui,sans-serif;max-width:40rem;margin:2rem auto;padding:0 1rem;color:#1f2328;background:#fff}}
.box{{border:1px solid #d0d7de;border-radius:8px;padding:1rem 1.25rem;margin:1rem 0}}.warn{{background:#fff8c5;border-color:#d4a72c}}
.code{{font:700 2rem ui-monospace,monospace;letter-spacing:.15em}}button,a.button{{font-size:1rem;padding:.5rem 1.25rem;border-radius:6px;
border:1px solid #1f883d;background:#1f883d;color:#fff;text-decoration:none;display:inline-block;cursor:pointer}}
dt{{font-weight:600}}dd{{margin:0 0 .5rem}}@media (prefers-color-scheme:dark){{body{{background:#0d1117;color:#e6edf3}}
.box{{border-color:#30363d}}.warn{{background:#272115;border-color:#9e6a03}}}}</style></head>
<body><h1>{html.escape(title)}</h1>{body}</body></html>"""
    return HTMLResponse(content, status_code=status, headers={
        "Cache-Control": "no-store", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"})


class Login:
    def __init__(self, service: Service, provider: Provider):
        self.service, self.provider = service, provider
        self.settings, self.store, self.runner = service.settings, service.store, service.runner
        self.busy: set[str] = set()
        self.hostname = socket.gethostname()

    # Cookie binding ------------------------------------------------------------------
    # Login pages live at fixed paths (/login?request=<id>, /login/start), so Nginx publishes only exact routes.
    def page_url(self, pending_id: str) -> str:
        return f"{self.settings.base_path}/login?request={pending_id}"

    def attach_cookie(self, response: Response, pending_id: str, cookie: str) -> None:
        response.set_cookie(COOKIE + pending_id, cookie, max_age=self.settings.pending_ttl_seconds,
                            path=f"{self.settings.base_path}/login", secure=self.settings.secure_cookies,
                            httponly=True, samesite="lax")

    def load(self, request: Request, pending_id):
        pending_id = pending_id if isinstance(pending_id, str) else ""
        pending = self.store.pending(pending_id) if PENDING_ID.fullmatch(pending_id) else None
        if pending is None or pending["expires_at"] < int(time.time()):
            return None, None, page("授权请求已失效", "<p>此授权请求不存在或已过期（15 分钟）。请回到 MCP 客户端重新连接。</p>", status=404)
        cookie = request.cookies.get(COOKIE + pending_id, "")
        if not cookie or not hmac.compare_digest(digest(cookie), pending["cookie_hash"]):
            return None, None, page("无法继续授权", "<p>只有发起授权的浏览器才能继续。请在原来的浏览器中打开，"
                                    "或回到 MCP 客户端重新连接。</p>", status=403)
        return pending, cookie, None

    # Pages ---------------------------------------------------------------------------
    async def describe(self, pending: dict) -> str:
        params = json.loads(pending["params"])
        client = await self.provider.get_client(pending["client_id"])
        name = (client.client_name if client and client.client_name else pending["client_id"])
        redirect_host = urlsplit(params["redirect_uri"]).netloc
        scopes = "".join(f"<li>{html.escape(SCOPE_TEXT.get(s, s))}</li>" for s in params["scopes"])
        return (f'<div class="box"><dl><dt>客户端</dt><dd>{html.escape(name)}</dd>'
                f'<dt>授权后跳转到</dt><dd><strong>{html.escape(redirect_host)}</strong></dd>'
                f'<dt>申请的权限</dt><dd><ul>{scopes}</ul></dd>'
                f'<dt>Dify 服务器</dt><dd>{html.escape(self.settings.dify_server)}</dd></dl></div>'
                '<div class="box warn">请确认回调域名是你正在使用的 MCP 客户端。授权后，该客户端可以用你的 Dify 账号执行上述操作。'
                '在 Dify 页面批准前，请核对页面上显示的账户和工作空间。</div>')

    async def show(self, request: Request) -> Response:
        pending, cookie, error = self.load(request, request.query_params.get("request"))
        if error:
            return error
        details = await self.describe(pending)
        status = pending["status"]
        if status == "new":
            action = html.escape(self.settings.base_path + "/login/start")
            return page("连接 difyctl MCP", details + f'<form method="post" action="{action}">'
                        f'<input type="hidden" name="request" value="{html.escape(pending["id"])}">'
                        f'<input type="hidden" name="csrf" value="{csrf_token(cookie, pending["id"])}">'
                        '<button type="submit">继续，登录 Dify</button></form>')
        if status == "waiting":
            if pending["id"] not in self.busy and self.store.claim_poll(pending["id"], self.settings.poll_interval_seconds):
                result = await self.check(pending)
                if result is not None:
                    return result
                pending = self.store.pending(pending["id"])
            if pending["status"] == "waiting":
                return self.waiting(pending, details)
        if pending["status"] == "approved":
            return page("授权已完成", "<p>授权已完成，可以关闭此页面。</p>")
        if pending["status"] in ("starting", "completing"):
            return page("正在处理", "<p>正在处理，请稍候……</p>", refresh=self.settings.poll_interval_seconds)
        return page("授权失败", f'<p>{html.escape(pending["error"] or "授权未完成。")}</p><p>请回到 MCP 客户端重新连接。</p>', status=400)

    def waiting(self, pending: dict, details: str) -> Response:
        uri = html.escape(pending["verification_uri"], quote=True)
        code = html.escape(pending["user_code"])
        remaining = max(0, pending["expires_at"] - int(time.time())) // 60
        return page("在 Dify 中批准登录", details + f'<div class="box"><p>1. 打开 Dify 验证页并登录：</p>'
                    f'<p><a class="button" href="{uri}" target="_blank" rel="noopener noreferrer">打开 {uri}</a></p>'
                    f'<p>2. 输入或核对验证码：</p><p class="code">{code}</p>'
                    '<p>3. 在 Dify 页面确认账户、工作空间和设备后批准。本页每 5 秒自动检查一次结果。</p>'
                    f'<p>剩余约 {remaining} 分钟。Dify 对同一网页会话每小时最多批准 10 次。</p></div>',
                    refresh=self.settings.poll_interval_seconds)

    # Device flow ------------------------------------------------------------------------
    async def start(self, request: Request) -> Response:
        form = await request.form()
        pending, cookie, error = self.load(request, form.get("request"))
        if error:
            return error
        supplied = form.get("csrf")
        if not isinstance(supplied, str) or not hmac.compare_digest(supplied, csrf_token(cookie, pending["id"])):
            return page("无法继续授权", "<p>请求校验失败，请刷新页面后重试。</p>", status=403)
        back = RedirectResponse(self.page_url(pending["id"]), status_code=303)
        if not self.store.update_pending(pending["id"], "new", status="starting"):
            return back
        config_dir = self.settings.sessions_dir / secrets.token_hex(16)
        config_dir.mkdir(parents=True, mode=0o700)
        self.store.update_pending(pending["id"], config_dir=str(config_dir))
        run = await self.runner.run(["login", "--server", self.settings.dify_server, "--no-browser", "--no-wait",
                                     "--no-keyring", "--json"], config_dir=config_dir, timeout=60, limited=False)
        data = run.json() if run.exit_code == 0 else None
        uri = data.get("verification_uri") if isinstance(data, dict) else None
        code = data.get("user_code") if isinstance(data, dict) else None
        if not (isinstance(uri, str) and self.trusted_uri(uri) and isinstance(code, str) and USER_CODE.fullmatch(code)):
            log.warning("device login could not start (exit %s, code %s)", run.exit_code, (run.error or {}).get("code"))
            self.fail(pending["id"], config_dir, "无法从 Dify 获取设备验证码，请稍后重试。")
            return back
        self.store.update_pending(pending["id"], "starting", status="waiting", user_code=code, verification_uri=uri,
                                  last_poll_at=time.time())
        return back

    def trusted_uri(self, uri: str) -> bool:
        """The verification page must be on the configured Dify server, so the code is not phished elsewhere."""
        server, target = urlsplit(self.settings.dify_server), urlsplit(uri)
        return target.scheme == server.scheme and target.netloc == server.netloc and not target.username

    def fail(self, pending_id: str, config_dir, message: str) -> None:
        self.store.update_pending(pending_id, status="failed", error=message, config_dir=None)
        remove_tree(config_dir, self.settings.sessions_dir)

    async def check(self, pending: dict) -> Response | None:
        self.busy.add(pending["id"])
        try:
            config_dir = Path(pending["config_dir"])
            run = await self.runner.run(["login", "--resume", "--json"], config_dir=config_dir, timeout=60, limited=False)
            data = run.json()
            if run.exit_code == 0 and isinstance(data, dict) and data.get("status") == "pending":
                return None
            if run.exit_code == 0 and isinstance(data, dict) and data.get("email"):
                if not self.store.update_pending(pending["id"], "waiting", status="completing"):
                    return None
                return await self.complete(pending, config_dir, data)
            code = (run.error or {}).get("code")
            if run.exit_code == 4 and code in ("auth_expired", "access_denied", "not_logged_in"):
                message = "Dify 登录被拒绝。" if code == "access_denied" else "验证码已过期或登录未完成。"
                self.fail(pending["id"], config_dir, message)
            # Other failures (network, 5xx) keep waiting until the pending login expires.
            return None
        finally:
            self.busy.discard(pending["id"])

    async def complete(self, pending: dict, config_dir: Path, login: dict) -> Response:
        params = json.loads(pending["params"])
        account_run = await self.runner.run(["describe", "account", "--input", "@-", "--json"], config_dir=config_dir,
                                            stdin=b"{}", timeout=60, limited=False)
        account = account_run.json() if account_run.exit_code == 0 else None
        account = account if isinstance(account, dict) else {}
        saved = login.get("account") if isinstance(login.get("account"), dict) else {}
        account_id = account.get("id") or account.get("account_id") or saved.get("id")
        email = str(account.get("email") or login.get("email") or "").strip().lower()
        if not isinstance(account_id, str) or not account_id or not email:
            await self.discard(config_dir)
            self.fail(pending["id"], None, "无法确定 Dify 账户信息，请重试。")
            return await self.show_failed(pending)
        if email not in self.settings.allowed_emails:
            log.warning("login refused for an account outside MCP_ALLOWED_EMAILS")
            await self.discard(config_dir)
            self.fail(pending["id"], None, f"账号 {email} 不在允许列表中，已结束这次 Dify 登录。")
            return await self.show_failed(pending)
        now = int(time.time())
        sessions = await self.runner.run(["get", "account", "session", "--input", "@-", "--json"], config_dir=config_dir,
                                         stdin=b"{}", timeout=60, limited=False)
        expires = session_expiry(sessions.json() if sessions.exit_code == 0 else None, self.hostname, now,
                                 self.settings.session_fallback_seconds)
        async with self.service.locks[account_id].write():
            replaced = self.store.adopt_session(account_id, email, str(config_dir), expires)
            remove_tree(replaced, self.settings.sessions_dir)
            self.service.clear_descriptors(account_id)
        code = self.provider.issue_code(pending, params, account_id)
        self.store.update_pending(pending["id"], status="approved", config_dir=None, user_code=None)
        log.info("authorized account %s for client %s", account_id, pending["client_id"])
        return RedirectResponse(construct_redirect_uri(params["redirect_uri"], code=code, state=params["state"],
                                                       iss=self.settings.issuer),
                                status_code=303, headers={"Cache-Control": "no-store"})

    async def discard(self, config_dir: Path) -> None:
        """End the Dify session on the server, then delete its local files."""
        await self.runner.run(["logout", "--json"], config_dir=config_dir, timeout=60, limited=False)
        remove_tree(config_dir, self.settings.sessions_dir)

    async def show_failed(self, pending: dict) -> Response:
        current = self.store.pending(pending["id"]) or pending
        return page("授权失败", f'<p>{html.escape(current["error"] or "授权未完成。")}</p>', status=403)

    # Maintenance ---------------------------------------------------------------------------
    def cleanup(self) -> None:
        sessions = self.settings.sessions_dir
        adopted = self.store.user_dirs()
        for pending in self.store.expired_pending():
            if pending["config_dir"] and pending["config_dir"] not in adopted:
                remove_tree(pending["config_dir"], sessions)
            self.store.delete_pending(pending["id"])
        if sessions.is_dir():
            referenced = self.store.referenced_dirs()
            for entry in sessions.iterdir():
                # Orphans older than the pending lifetime: never a directory a login is still using.
                if str(entry) not in referenced and entry.stat().st_mtime < time.time() - 2 * self.settings.pending_ttl_seconds:
                    remove_tree(entry, sessions)
        self.store.purge()

    def ensure_dirs(self) -> None:
        for path in (self.settings.sessions_dir, self.settings.users_dir, self.settings.home_dir):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(path, 0o700)
