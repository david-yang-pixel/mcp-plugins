"""Single source of truth for the sz commands exposed through ``sz_run``.

``sz_runner`` derives its allow-list from :data:`SZ_COMMANDS`, the ``sz_run`` tool
description and the ``["help"]`` response are generated from it, and
``tests/test_sz_commands.py`` checks it against the argparse definition in
``scripts/sz`` so documentation and implementation cannot drift apart.
"""

from __future__ import annotations

from typing import Any


_TIME_OPTIONS: tuple[tuple[str, str], ...] = (
    ("--start ISO", "Absolute window start, ISO 8601; no offset means UTC."),
    ("--end ISO", "Absolute window end, ISO 8601; default now. With only --end, start = end - --hours."),
)

SZ_COMMANDS: dict[str, dict[str, Any]] = {
    "errors": {
        "summary": "Search ERROR/FATAL logs, newest first (body truncated to 300 chars).",
        "positionals": (),
        "options": (
            ("--tenant ID", "Exact attributes_string['tenant_id']."),
            ("--grep TEXT", "Body substring."),
            ("--env ENV", "deployment.environment; omitted = no filter."),
            ("--deployment NAME", "k8s.deployment.name, e.g. dify-api-oltp."),
            ("--hours N", "Lookback hours, default 1, max 168."),
            *_TIME_OPTIONS,
            ("--limit N", "Rows, default 30, max 1000."),
        ),
    },
    "traceback": {
        "summary": "Fetch full exception.stacktrace for ERROR logs.",
        "positionals": (),
        "options": (
            ("--id LOG_ID", "Exact log id (most precise)."),
            ("--tenant ID", "Exact attributes_string['tenant_id']."),
            ("--grep TEXT", "Body substring."),
            ("--env ENV", "deployment.environment; omitted = no filter."),
            ("--hours N", "Lookback hours, default 24, max 168."),
            *_TIME_OPTIONS,
            ("--limit N", "Rows, default 5, max 1000."),
        ),
    },
    "query": {
        "summary": (
            "Run one read-only ClickHouse SQL (SELECT/WITH/SHOW/DESCRIBE/EXPLAIN). "
            "{start_ns}/{end_ns} placeholders are replaced with the window in nanoseconds."
        ),
        "positionals": (("SQL", "The SQL text."),),
        "options": (
            ("--hours N", "Window for the placeholders, default 1, max 168."),
            *_TIME_OPTIONS,
            ("--panel TYPE", "list (default), table, or graph."),
        ),
    },
    "schema": {
        "summary": "Describe a SigNoz ClickHouse table.",
        "positionals": (("TABLE", "Optional; default distributed_logs_v2, signoz_logs. prefix added if missing."),),
        "options": (),
    },
    "services": {
        "summary": "List active deployments/services with log counts.",
        "positionals": (),
        "options": (
            ("--env ENV", "deployment.environment filter."),
            ("--hours N", "Lookback hours, default 1, max 168."),
            *_TIME_OPTIONS,
            ("--limit N", "Rows, default 30, max 1000."),
        ),
    },
}

ALLOWED_COMMANDS: frozenset[str] = frozenset(SZ_COMMANDS)
HELP_WORDS: frozenset[str] = frozenset({"help", "--help", "-h"})


def option_flags(command: str) -> list[str]:
    """Return the bare flags (e.g. ``--hours``) documented for one command."""
    return [usage.split()[0] for usage, _ in SZ_COMMANDS[command]["options"]]


def command_usage(command: str) -> str:
    spec = SZ_COMMANDS[command]
    parts = [command]
    for name, _ in spec["positionals"]:
        parts.append(f"<{name}>" if command == "query" else f"[{name}]")
    parts.extend(f"[{usage}]" for usage, _ in spec["options"])
    return " ".join(parts)


def sz_run_description() -> str:
    commands = "; ".join(command_usage(name) for name in SZ_COMMANDS)
    return (
        "Run an allowed read-only server-side sz command with JSON output. "
        "Raw SQL must be one SELECT/WITH/SHOW/DESCRIBE/EXPLAIN query. "
        f"Commands: {commands}. "
        "query replaces {start_ns}/{end_ns} in the SQL with the window in nanoseconds. "
        "--hours is at most 168; --start/--end are ISO 8601, without an offset they are UTC. "
        'Pass ["help"] (or ["<command>", "--help"]) to get this list as JSON.'
    )


def _command_help(command: str) -> dict[str, Any]:
    spec = SZ_COMMANDS[command]
    return {
        "name": command,
        "usage": command_usage(command),
        "summary": spec["summary"],
        "positionals": [{"name": name, "help": text} for name, text in spec["positionals"]],
        "options": [{"flag": usage, "help": text} for usage, text in spec["options"]],
    }


def help_payload(args: Any) -> dict[str, Any] | None:
    """Return a help payload for help requests, or ``None`` for normal commands."""
    if not isinstance(args, list) or not args or not all(isinstance(a, str) for a in args):
        return None
    first = args[0].strip()
    if first in HELP_WORDS:
        wanted = [a for a in args[1:] if a in SZ_COMMANDS]
    elif first in SZ_COMMANDS and any(a in ("--help", "-h") for a in args[1:]):
        wanted = [first]
    else:
        return None
    names = wanted or list(SZ_COMMANDS)
    return {
        "success": True,
        "data": {
            "commands": [_command_help(name) for name in names],
            "notes": [
                "--json and -q are added automatically; do not pass them.",
                "Use --hours for a relative window or --start/--end for an absolute window.",
            ],
        },
    }
