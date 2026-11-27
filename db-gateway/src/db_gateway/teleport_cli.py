"""Read-only tsh layer for plugin_lambda_logs and kube_read.

Two facts measured on the server (spec §9.2) shape this module:

* tsh rewrites the whole key ring (user cert, key, CAs, SSH cert) on every
  ``tsh apps login`` / ``tsh aws`` / ``tsh kubectl``. Running it against the
  shared ``teleport.auth_home`` would make ``pg_pool.keys_signature`` flush the
  connection pools and could race tsh-auth-watchdog's renewal. The tools
  therefore run tsh against a container-local copy (the *shadow* home) and only
  ever read the shared directory.
* The host-mounted tsh 18.0.0 fails ``tsh aws`` signature verification against
  the 18.11 proxy, so the tools use the tsh bundled in the image
  (``teleport_tools.tsh_path``).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

from db_gateway.log import get_logger
from db_gateway.postgres import TeleportAuthExpired
from db_gateway.teleport import get_tsh_status  # noqa: F401  (used by the login layer)

logger = get_logger(module="teleport_cli")

SHADOW_SIGNATURE_FILE = ".source-signature.json"
SHADOW_IGNORE_SUFFIXES = ("-app", "-kube", "-db")

_shadow_lock: asyncio.Lock | None = None


def _reset_state() -> None:
    """Drop module-level locks and caches (tests; each test has its own loop)."""
    global _shadow_lock
    _shadow_lock = None


def source_signature(auth_home: str) -> list[list[str]] | None:
    """``[[relative path, sha256], ...]`` of the user TLS certs ``keys/*/*.crt``.

    tsh rewrites these files with identical content, so the signature only
    changes when tsh-auth-watchdog renews the session. None while not logged in
    (no ``current-profile`` or no cert), e.g. during the renewal window.
    """
    root = Path(auth_home)
    if not (root / "current-profile").is_file():
        return None
    entries: list[list[str]] = []
    try:
        for cert in sorted((root / "keys").glob("*/*.crt")):
            if cert.is_file():
                digest = hashlib.sha256(cert.read_bytes()).hexdigest()
                entries.append([str(cert.relative_to(root)), digest])
    except OSError:
        return None
    return entries or None


def _read_shadow_signature(shadow: Path) -> list[list[str]] | None:
    try:
        return json.loads((shadow / SHADOW_SIGNATURE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _ignore(_dir: str, names: list[str]) -> set[str]:
    return {n for n in names if n == "bin" or n.endswith(SHADOW_IGNORE_SUFFIXES)}


def _sync_shadow(source: str, shadow: Path, sig: list[list[str]]) -> bool:
    """Copy ``source`` into ``shadow`` atomically; False if the source moved under us."""
    tmp = shadow.with_name(f"{shadow.name}.tmp-{os.getpid()}")
    old = shadow.with_name(f"{shadow.name}.old-{os.getpid()}")
    for _attempt in range(2):
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            shutil.copytree(source, tmp, ignore=_ignore, symlinks=True)
        except (OSError, shutil.Error):
            continue
        if source_signature(source) != sig:
            continue  # tsh-auth-watchdog renewed while we copied
        (tmp / SHADOW_SIGNATURE_FILE).write_text(json.dumps(sig), encoding="utf-8")
        shadow.parent.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(old, ignore_errors=True)
        if shadow.exists():
            shadow.rename(old)
        tmp.rename(shadow)
        shutil.rmtree(old, ignore_errors=True)
        return True
    shutil.rmtree(tmp, ignore_errors=True)
    return False


async def ensure_shadow_home(cfg: Any) -> str:
    """Return the shadow TELEPORT_HOME, re-syncing it after a renewal.

    Raises TeleportAuthExpired when neither the shared home nor an earlier copy
    is usable.
    """
    global _shadow_lock
    if _shadow_lock is None:
        _shadow_lock = asyncio.Lock()
    source = cfg.teleport.auth_home
    shadow = Path(cfg.teleport_tools.shadow_home)
    async with _shadow_lock:
        sig = source_signature(source)
        current = _read_shadow_signature(shadow)
        if sig is not None and sig == current:
            return str(shadow)
        if sig is not None and await asyncio.to_thread(_sync_shadow, source, shadow, sig):
            logger.info("teleport_shadow_synced", shadow=str(shadow))
            return str(shadow)
        if current is not None:
            # Renewal window (or a copy that raced it): the old certificate is
            # still within its validity, keep using it.
            return str(shadow)
        raise TeleportAuthExpired("Teleport 会话不可用（可能正在续期），稍后重试")
