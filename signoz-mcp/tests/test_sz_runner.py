from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from signoz_mcp.settings import Settings
from signoz_mcp.sz_runner import run_sz


@pytest.mark.asyncio
async def test_run_sz_parses_json_and_injects_env(tmp_path: Path):
    script = tmp_path / "fake_sz.py"
    script.write_text(
        "import json, os, sys\n"
        "assert sys.argv[-2:] == ['--json', '-q']\n"
        "print(json.dumps([{'url': os.environ['SIGNOZ_URL'], 'key': os.environ['SIGNOZ_API_KEY']}]))\n"
    )
    settings = Settings(
        SIGNOZ_MCP_API_KEY="x" * 32,
        SIGNOZ_MCP_SZ_PATH=str(script),
        SIGNOZ_MCP_TIMEOUT_SECONDS=5,
        SIGNOZ_URL="https://signoz.example.com",
        SIGNOZ_API_KEY="sn_test",
    )

    result = await run_sz(["query", "SELECT 1"], settings)

    assert result == {
        "success": True,
        "data": [{"url": "https://signoz.example.com", "key": "sn_test"}],
    }


@pytest.mark.asyncio
async def test_run_sz_returns_structured_error_on_nonzero(tmp_path: Path):
    script = tmp_path / "fake_sz.py"
    script.write_text("import sys\nprint('bad query', file=sys.stderr)\nsys.exit(2)\n")
    settings = Settings(
        SIGNOZ_MCP_API_KEY="x" * 32,
        SIGNOZ_MCP_SZ_PATH=str(script),
        SIGNOZ_MCP_TIMEOUT_SECONDS=5,
    )

    result = await run_sz(["query", "SELECT bad"], settings)

    assert result["success"] is False
    assert result["code"] == "SZ_NONZERO"
    assert "bad query" in result["error"]


@pytest.mark.asyncio
async def test_run_sz_rejects_bad_args():
    settings = Settings(SIGNOZ_MCP_API_KEY="x" * 32)

    with pytest.raises(ValueError):
        await run_sz("query SELECT 1", settings)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE important",
        "SELECT 1; DROP TABLE important",
        "SELECT 1 INTO OUTFILE '/tmp/leak'",
    ],
)
async def test_run_sz_rejects_non_read_only_raw_sql(sql):
    settings = Settings(SIGNOZ_MCP_API_KEY="x" * 32)

    with pytest.raises(ValueError, match="read-only"):
        await run_sz(["query", sql], settings)


@pytest.mark.asyncio
async def test_run_sz_rejects_unsafe_schema_table_name():
    settings = Settings(SIGNOZ_MCP_API_KEY="x" * 32)

    with pytest.raises(ValueError, match="table"):
        await run_sz(["schema", "logs; DROP TABLE x"], settings)


@pytest.mark.asyncio
async def test_run_sz_kills_timed_out_process(tmp_path: Path, monkeypatch):
    marker = tmp_path / "marker.txt"
    script = tmp_path / "fake_sz.py"
    script.write_text(
        "import os, pathlib, time\n"
        "time.sleep(0.5)\n"
        "pathlib.Path(os.environ['MARKER']).write_text('still-running')\n"
    )
    monkeypatch.setenv("MARKER", str(marker))
    settings = Settings(
        SIGNOZ_MCP_API_KEY="x" * 32,
        SIGNOZ_MCP_SZ_PATH=str(script),
        SIGNOZ_MCP_TIMEOUT_SECONDS=0.05,
    )

    result = await run_sz(["services"], settings)
    await asyncio.sleep(0.7)

    assert result["success"] is False
    assert result["code"] == "SZ_TIMEOUT"
    assert not marker.exists()
