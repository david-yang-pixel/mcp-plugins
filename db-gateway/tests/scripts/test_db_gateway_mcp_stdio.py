"""DB Gateway MCP stdio smoke tests."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_db_gateway_mcp_starts_without_site_packages():
    """The MCP script is launched by shebang in Docker, so avoid third-party deps."""
    script = Path(__file__).resolve().parents[2] / "scripts" / "db_gateway_mcp.py"
    proc = subprocess.Popen(
        [sys.executable, "-S", str(script)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdin is not None
        assert proc.stdout is not None
        proc.stdin.write(json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {},
        }) + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
        assert line
        data = json.loads(line)
        assert data["id"] == 1
        assert data["result"]["serverInfo"]["name"] == "db-gateway-mcp"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
