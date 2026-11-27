"""Fixtures for the read-only Teleport tools (teleport_cli, lambda_logs, kube_read)."""
from __future__ import annotations

import importlib
import sys
from unittest.mock import AsyncMock

import pytest

from db_gateway.config import AppConfig
from tsh_helpers import FakeTsh, write_tsh_src


@pytest.fixture
def fake_tsh(tmp_path):
    return FakeTsh(tmp_path / "fake_tsh")


@pytest.fixture
def cfg(tmp_path, fake_tsh):
    return AppConfig(
        teleport={"auth_home": str(write_tsh_src(tmp_path / "tsh-src")),
                  "proxy": "teleport.dify.dev:443", "user": "u"},
        db_gateway={"audit_log_dir": str(tmp_path / "audit")},
        teleport_tools={"tsh_path": fake_tsh.path, "shadow_home": str(tmp_path / "shadow"),
                        "kube": {"kubeconfig_path": str(tmp_path / "kube" / "kubeconfig")}},
    )


@pytest.fixture(autouse=True)
def _reset_teleport_state(monkeypatch):
    """Fresh module state per test; never run the real tsh for `tsh status`."""
    for name in ("db_gateway.teleport_cli", "db_gateway.lambda_logs", "db_gateway.kube_read"):
        mod = sys.modules.get(name)
        if mod is not None and hasattr(mod, "_reset_state"):
            mod._reset_state()
    try:
        teleport_cli = importlib.import_module("db_gateway.teleport_cli")
    except ImportError:
        return
    monkeypatch.setattr(teleport_cli, "get_tsh_status", AsyncMock(return_value=None))
