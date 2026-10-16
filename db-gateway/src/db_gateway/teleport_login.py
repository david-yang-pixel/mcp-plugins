"""Shared `tsh login` runner: spawns tsh via pexpect with PTY, feeds
password + OTP, returns success/error.

Used by:
- services/teleport_auth_web.py — interactive web form (manual login)
- scripts/auth-watchdog.py    — automatic renewal using stored TOTP secret

The login flow has several gotchas worth keeping in one place:
1. HTTP(S)_PROXY env vars MUST be cleared (host clash/v2ray MITMs HTTPS,
   makes Teleport think credentials are wrong).
2. Existing session (cert) shortcuts the password prompt; pre-logout +
   purge keys/ + current-profile is required to force re-auth.
3. Cert files written by container's root must be chown'd back to host's
   zd-agent uid/gid (HOST_TSH_UID/HOST_TSH_GID env), else tsh-proxy-db
   systemd service can't read them after restart.
"""
from __future__ import annotations

import asyncio
import os

from db_gateway.log import get_logger

logger = get_logger(module="integrations.teleport_login")


async def run_tsh_login(
    *,
    proxy: str,
    user: str,
    password: str,
    otp: str,
    auth_home: str,
    timeout_s: int = 60,
) -> tuple[bool, str]:
    """Spawn `tsh login` via pexpect, feed password + OTP, wait for completion.

    Returns ``(success, message)``.
    """
    try:
        import pexpect
    except ImportError:
        return False, "pexpect module not installed in container"

    env = dict(os.environ)
    env["TELEPORT_HOME"] = auth_home
    # Clear all proxy env — Teleport refuses MITM'd HTTPS as "invalid credentials"
    for proxy_var in (
        "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
        "ALL_PROXY", "all_proxy",
    ):
        env.pop(proxy_var, None)
    env["NO_PROXY"] = "*"
    env["no_proxy"] = "*"

    logger.info("tsh_login_start", proxy=proxy, user=user, no_proxy_enforced=True)

    def _run_sync() -> tuple[bool, str]:
        # Step 0: 清掉现有 session
        try:
            import subprocess
            import shutil
            from pathlib import Path as _P

            r = subprocess.run(
                ["tsh", "logout", "--all"],
                env=env, capture_output=True, timeout=10, check=False,
            )
            logger.info(
                "tsh_logout_result",
                rc=r.returncode,
                stdout=r.stdout.decode("utf-8", errors="replace")[:200],
                stderr=r.stderr.decode("utf-8", errors="replace")[:200],
            )
            # 兜底：硬删 keys / current-profile / <cluster>.yaml
            # 集群名 = proxy 去掉端口（tsh 用集群名做 profile 文件名而不是 proxy 字符串）
            # 例：proxy="teleport.dify.dev:443" → 文件名 "teleport.dify.dev.yaml"
            cluster_name = proxy.split(":", 1)[0]
            keys_dir = _P(auth_home) / "keys"
            if keys_dir.exists():
                shutil.rmtree(keys_dir, ignore_errors=True)
            for fname in ("current-profile", f"{cluster_name}.yaml"):
                fpath = _P(auth_home) / fname
                if fpath.exists():
                    try:
                        fpath.unlink()
                    except Exception:
                        pass
            logger.info(
                "tsh_profile_purged",
                auth_home=auth_home,
                cluster_name=cluster_name,
            )
        except Exception as e:
            return False, f"pre-logout failed: {e}"

        # Step 1: 正式 login
        try:
            child = pexpect.spawn(
                "tsh", ["login", f"--proxy={proxy}", f"--user={user}"],
                env=env, timeout=timeout_s, encoding="utf-8",
            )

            idx = child.expect([
                r"[Ee]nter password.*:",
                r"[Pp]assword.*:",
                pexpect.EOF,
                pexpect.TIMEOUT,
            ])
            if idx in (2, 3):
                return False, f"Unexpected output before password prompt:\n{child.before}"
            child.sendline(password)

            idx = child.expect([
                r"[Ee]nter.*OTP.*:",
                r"[Oo]ne.*time.*password.*:",
                r"[Tt]wo-?factor.*:",
                r"[Aa]uthentication.*code.*:",
                pexpect.EOF,
                pexpect.TIMEOUT,
            ])
            if idx in (4, 5):
                return False, f"Unexpected output (likely bad password)\n{child.before}"
            child.sendline(otp)

            child.expect([pexpect.EOF, pexpect.TIMEOUT])
            rc = child.wait()
            output = child.before or ""

            if rc == 0:
                # chown back to host zd-agent uid/gid
                host_uid = os.environ.get("HOST_TSH_UID")
                host_gid = os.environ.get("HOST_TSH_GID")
                if host_uid and host_gid:
                    try:
                        import subprocess
                        subprocess.run(
                            ["chown", "-R", f"{host_uid}:{host_gid}", auth_home],
                            check=False, timeout=10,
                        )
                    except Exception as e:
                        logger.warning("chown_tsh_failed", error=str(e))
                return True, "tsh login succeeded"
            return False, f"tsh login failed (rc={rc}):\n{output[-800:]}"
        except Exception as e:
            return False, f"pexpect error: {e}"

    return await asyncio.to_thread(_run_sync)
