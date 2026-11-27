"""teleport_cli: shadow TELEPORT_HOME and the tsh subprocess layer."""
import shutil
from pathlib import Path

import pytest

from db_gateway.postgres import TeleportAuthExpired
from db_gateway.teleport_cli import ensure_shadow_home
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
