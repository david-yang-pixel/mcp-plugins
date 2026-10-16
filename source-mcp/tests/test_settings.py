from __future__ import annotations

import pytest
from pydantic import ValidationError

from source_mcp.settings import Settings


def test_full_repo_search_concurrency_defaults_to_one(monkeypatch):
    monkeypatch.delenv("SOURCE_MCP_FULL_REPO_SEARCH_CONCURRENCY", raising=False)

    assert Settings(_env_file=None).full_repo_search_concurrency == 1


@pytest.mark.parametrize("configured_concurrency", ["0", "5"])
def test_full_repo_search_concurrency_rejects_unsafe_values(
    monkeypatch,
    configured_concurrency,
):
    monkeypatch.setenv(
        "SOURCE_MCP_FULL_REPO_SEARCH_CONCURRENCY",
        configured_concurrency,
    )

    with pytest.raises(ValidationError):
        Settings(_env_file=None)
