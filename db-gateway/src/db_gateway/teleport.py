"""Teleport integration: tsh status parsing + headless login trigger."""
from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional


@dataclass
class TshStatus:
    user: str
    cluster: str
    roles: list[str]
    valid_until: datetime
    remaining: timedelta


_DUR_PATTERN = re.compile(r"(\d+)\s*([hms])")


def _parse_duration(s: str) -> timedelta:
    total = timedelta()
    for num, unit in _DUR_PATTERN.findall(s):
        n = int(num)
        if unit == "h":
            total += timedelta(hours=n)
        elif unit == "m":
            total += timedelta(minutes=n)
        elif unit == "s":
            total += timedelta(seconds=n)
    return total


def parse_tsh_status(output: str) -> Optional[TshStatus]:
    """Parse `tsh status` output. Returns None if not logged in or unparseable."""
    if "Not logged in" in output or "no profile" in output.lower():
        return None

    user_m = re.search(r"Logged in as:\s*(\S+)", output)
    cluster_m = re.search(r"Cluster:\s*(\S+)", output)
    roles_m = re.search(r"Roles:\s*(.+)", output)
    valid_m = re.search(
        r"Valid until:\s*([0-9:\- ]+\s*[+\-]\d{4})\s*\w+\s*\[valid for\s*([^\]]+)\]",
        output,
    )

    if not (user_m and valid_m):
        return None

    try:
        valid_until = datetime.strptime(valid_m.group(1).strip(), "%Y-%m-%d %H:%M:%S %z")
    except ValueError:
        return None

    rem_str = valid_m.group(2).strip()
    remaining = _parse_duration(rem_str)

    roles_raw = roles_m.group(1) if roles_m else ""
    roles = [r.strip() for r in roles_raw.split(",") if r.strip()]

    return TshStatus(
        user=user_m.group(1),
        cluster=cluster_m.group(1) if cluster_m else "",
        roles=roles,
        valid_until=valid_until,
        remaining=remaining,
    )


async def get_tsh_status(
    *, proxy: str, user: str, auth_home: str,
) -> Optional[TshStatus]:
    """Run `tsh status` subprocess, parse output."""
    env = dict(os.environ)
    env["TELEPORT_HOME"] = auth_home
    proc = await asyncio.create_subprocess_exec(
        "tsh", "status",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, _stderr = await proc.communicate()
    if proc.returncode != 0:
        return None
    return parse_tsh_status(stdout.decode("utf-8", errors="replace"))


async def trigger_headless_login(
    *, proxy: str, user: str, auth_home: str,
    url_timeout_seconds: int = 30,
) -> str:
    """Launch `tsh login --headless`, capture the approval URL from stderr.

    Does NOT wait for the user to click the URL. The process keeps running and
    will terminate when the user approves (or when this caller kills it).
    Returns the URL. Returns empty string if URL not found within timeout.
    """
    env = dict(os.environ)
    env["TELEPORT_HOME"] = auth_home
    cmd = ["tsh", "login", "--headless", f"--proxy={proxy}", f"--user={user}"]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )

    url = ""
    url_pattern = re.compile(r"https://\S+/headless/\S+")

    async def _read_url() -> str:
        assert proc.stderr is not None
        while True:
            line = await proc.stderr.readline()
            if not line:
                return ""
            text = line.decode("utf-8", errors="replace")
            m = url_pattern.search(text)
            if m:
                return m.group(0)

    try:
        url = await asyncio.wait_for(_read_url(), timeout=url_timeout_seconds)
    except asyncio.TimeoutError:
        url = ""

    return url
