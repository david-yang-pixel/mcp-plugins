#!/usr/bin/env python3
"""One-shot tsh login，供 docker-compose tsh-init 容器启动时调用。
从 .env 读取凭据，生成 TOTP，执行 tsh login，成功后退出 0。
失败则打印错误并以非 0 退出，阻止后续服务启动。
"""
import asyncio, os, sys
from pathlib import Path

_REPO_ROOT = Path("/app")
sys.path.insert(0, str(_REPO_ROOT / "src"))

from db_gateway.config import EnvSettings, load_config
from db_gateway.teleport_login import run_tsh_login


async def main():
    cfg = load_config()
    env = EnvSettings()

    if not env.teleport_password or not env.teleport_totp_secret:
        print("ERROR: TELEPORT_PASSWORD / TELEPORT_TOTP_SECRET 未配置", flush=True)
        sys.exit(1)

    import pyotp
    otp = pyotp.TOTP(env.teleport_totp_secret).now()
    print(f"正在登录 {cfg.teleport.proxy} ...", flush=True)
    ok, msg = await run_tsh_login(
        proxy=cfg.teleport.proxy,
        user=cfg.teleport.user,
        password=env.teleport_password,
        otp=otp,
        auth_home=cfg.teleport.auth_home,
        timeout_s=90,
    )
    if ok:
        print("tsh login 成功", flush=True)
    else:
        print(f"tsh login 失败: {msg}", flush=True)
        sys.exit(1)


asyncio.run(main())
