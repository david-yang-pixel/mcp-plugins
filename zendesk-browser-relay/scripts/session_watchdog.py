from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").strip()
        key, separator, value = line.partition("=")
        if not separator:
            continue
        key = key.strip()
        if not key:
            continue
        env[key] = parse_env_value(value.strip())
    return env


def parse_env_value(value: str) -> str:
    if value == "":
        return ""
    try:
        parts = shlex.split(value, posix=True)
    except ValueError:
        return value.strip("\"'")
    if len(parts) == 1:
        return parts[0]
    return value


def request_session(base_url: str, api_key: str, timeout: float) -> dict[str, object]:
    req = Request(
        f"{base_url.rstrip('/')}/session",
        headers={"Authorization": f"Bearer {api_key}"},
        method="GET",
    )
    with urlopen(req, timeout=timeout) as response:
        raw = response.read().decode("utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("session response is not an object")
    return data


def is_logged_in(data: dict[str, object]) -> bool:
    return data.get("ok") is True and data.get("status") == "logged_in"


def restart_service(service: str) -> None:
    subprocess.run(["systemctl", "restart", service], check=True)


def check_once(base_url: str, api_key: str, timeout: float) -> tuple[bool, str]:
    try:
        data = request_session(base_url, api_key, timeout)
    except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        return False, type(exc).__name__
    if is_logged_in(data):
        return True, "logged_in"
    status = str(data.get("status") or "unknown")[:120]
    return False, status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", default="/etc/zendesk-browser-relay/env")
    parser.add_argument("--base-url", default="http://127.0.0.1:8097")
    parser.add_argument("--service", default="zendesk-browser-relay")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--restart", action="store_true")
    args = parser.parse_args(argv)

    env = load_env(Path(args.env_file))
    api_key = env.get("RELAY_API_KEY", "").strip()
    if not api_key:
        print("RELAY_API_KEY is missing", file=sys.stderr)
        return 2

    ok, reason = check_once(args.base_url, api_key, args.timeout)
    if ok:
        print("zendesk-browser-relay session ok: logged_in")
        return 0
    print(f"zendesk-browser-relay session check failed: {reason}", file=sys.stderr)
    if not args.restart:
        return 1

    restart_service(args.service)
    time.sleep(2)
    ok, reason = check_once(args.base_url, api_key, args.timeout)
    if ok:
        print("zendesk-browser-relay session recovered after restart")
        return 0
    print(
        f"zendesk-browser-relay session still unhealthy after restart: {reason}",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
