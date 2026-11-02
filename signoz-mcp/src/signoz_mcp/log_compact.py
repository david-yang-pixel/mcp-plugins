"""Shrink log rows returned by signoz_search_logs without losing the useful parts."""

from __future__ import annotations

from typing import Any


DEDUP_MIN_CHARS = 120


def truncate_text(value: str, max_chars: int) -> tuple[str, bool]:
    if len(value) <= max_chars:
        return value, False
    head = max(1, int(max_chars * 0.6))
    tail = max(1, max_chars - head)
    omitted = len(value) - head - tail
    return f"{value[:head]}\n…[truncated {omitted} chars]…\n{value[-tail:]}", True


def _label(path: tuple[str, ...]) -> str:
    return ".".join(path) if len(path) == 1 else f"{path[0]}['{path[1]}']"


def _compact_row(
    row: dict[str, Any],
    denylist: frozenset[str],
    max_chars: int,
    dropped: set[str],
    counters: dict[str, int],
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, dict):
            kept: dict[str, Any] = {}
            for inner_key, inner_value in value.items():
                if inner_key in denylist:
                    dropped.add(str(inner_key))
                    continue
                if inner_value in ("", None):
                    continue
                kept[inner_key] = inner_value
            out[key] = kept
        else:
            out[key] = value

    # Collect long text slots and keep only the longest copy of repeated text
    # (e.g. a traceback present both in body and attributes['exception.stacktrace']).
    slots: list[tuple[tuple[str, ...], str]] = []
    for key, value in out.items():
        if isinstance(value, str) and len(value) >= DEDUP_MIN_CHARS:
            slots.append(((key,), value))
        elif isinstance(value, dict):
            for inner_key, inner_value in value.items():
                if isinstance(inner_value, str) and len(inner_value) >= DEDUP_MIN_CHARS:
                    slots.append(((key, inner_key), inner_value))
    slots.sort(key=lambda slot: (-len(slot[1].strip()), slot[0] != ("body",)))
    kept_slots: list[tuple[tuple[str, ...], str]] = []
    for path, value in slots:
        needle = value.strip()
        owner = next((kept_path for kept_path, kept in kept_slots if needle in kept), None)
        if owner is None:
            kept_slots.append((path, value))
            continue
        marker = f"[duplicate of {_label(owner)}]"
        if len(path) == 1:
            out[path[0]] = marker
        else:
            out[path[0]][path[1]] = marker
        counters["deduplicated"] += 1

    for key, value in list(out.items()):
        if isinstance(value, str):
            out[key], cut = truncate_text(value, max_chars)
            counters["truncated"] += int(cut)
        elif isinstance(value, dict):
            for inner_key, inner_value in list(value.items()):
                if isinstance(inner_value, str):
                    value[inner_key], cut = truncate_text(inner_value, max_chars)
                    counters["truncated"] += int(cut)
    return out


def compact_log_rows(
    rows: Any,
    *,
    denylist: frozenset[str],
    max_chars: int,
) -> tuple[Any, dict[str, Any]]:
    """Drop denylisted map keys and empty values, de-duplicate repeated long text,
    and truncate long strings (head + tail kept, marked inline)."""
    if not isinstance(rows, list):
        return rows, {"compact": True}
    dropped: set[str] = set()
    counters = {"deduplicated": 0, "truncated": 0}
    compacted = [
        _compact_row(row, denylist, max_chars, dropped, counters) if isinstance(row, dict) else row
        for row in rows
    ]
    meta: dict[str, Any] = {
        "compact": True,
        "maxFieldChars": max_chars,
        "droppedKeys": sorted(dropped),
        "deduplicated": counters["deduplicated"],
        "truncated": counters["truncated"],
    }
    return compacted, meta
