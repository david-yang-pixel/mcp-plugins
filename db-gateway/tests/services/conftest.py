"""Fixtures for the read-only Teleport tools (teleport_cli, lambda_logs, kube_read)."""
from __future__ import annotations

import sys

import pytest

from db_gateway.config import AppConfig
from tsh_helpers import FakeTsh


@pytest.fixture
def fake_tsh(tmp_path):
    return FakeTsh(tmp_path / "fake_tsh")


TOTP_SECRET = "JBSWY3DPEHPK3PXP"   # synthetic test secret


@pytest.fixture
def cfg(tmp_path, fake_tsh):
    return AppConfig(
        teleport={"proxy": "teleport.dify.dev:443", "user": "u", "tsh_path": fake_tsh.path,
                  "sessions_dir": str(tmp_path / "sessions"),
                  "password": "test-password", "totp_secret": TOTP_SECRET},
        db_gateway={"audit_log_dir": str(tmp_path / "audit")},
    )


@pytest.fixture(autouse=True)
def _reset_teleport_state(monkeypatch):
    """Fresh module state per test (locks, caches, daemons and the session manager)."""
    for name in ("db_gateway.teleport_session", "db_gateway.teleport_cli", "db_gateway.lambda_logs",
                 "db_gateway.kube_read", "db_gateway.tsh_daemons", "db_gateway.aws_logs"):
        mod = sys.modules.get(name)
        if mod is not None and hasattr(mod, "_reset_state"):
            mod._reset_state()

