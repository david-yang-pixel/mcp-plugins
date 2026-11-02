from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_background_refresh(monkeypatch):
    # Tests must never start the background fetch loop or reach the network.
    monkeypatch.setenv("SOURCE_MCP_BACKGROUND_REFRESH_SECONDS", "0")
    from source_mcp import git_ops
    from source_mcp.settings import get_settings

    git_ops._BACKGROUND_STATE.clear()
    git_ops._BACKGROUND_RUN.clear()
    get_settings.cache_clear()
    yield
    git_ops._BACKGROUND_STATE.clear()
    git_ops._BACKGROUND_RUN.clear()
    get_settings.cache_clear()
