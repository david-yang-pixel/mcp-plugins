"""Run structured-tool plans: single query, or chunked sub-window queries with merge."""

from __future__ import annotations

import asyncio
import math
import time
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable

from .settings import Settings
from .structured_tools import ChunkSpec, StructuredPlan
from .timewindow import TimeWindow, format_hours, format_utc, split_window, windows_payload


Runner = Callable[[Any, Settings], Awaitable[dict[str, Any]]]

_HIDDEN_COLUMNS = ("__sum", "__count", "__ts")


def _number(value: Any) -> int | float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        text = str(value).strip()
        if text.lstrip("-").isdigit():
            return int(text)
        return float(text)
    except (TypeError, ValueError):
        return None


def merge_chunk_rows(chunk_rows: list[list[dict[str, Any]]], spec: ChunkSpec) -> list[dict[str, Any]]:
    """Merge per-sub-window rows by key columns, then finalize, sort, and limit."""
    merged: dict[tuple[Any, ...], dict[str, Any]] = {}
    for rows in chunk_rows:
        for row in rows:
            if not isinstance(row, dict):
                continue
            key = tuple(str(row.get(column)) for column in spec.key_columns)
            current = merged.get(key)
            if current is None:
                merged[key] = dict(row)
                continue
            if spec.finalize == "latest":
                if (_number(row.get("__ts")) or 0) > (_number(current.get("__ts")) or 0):
                    merged[key] = dict(row)
                continue
            for column, op in spec.merge_ops.items():
                left, right = _number(current.get(column)), _number(row.get(column))
                if left is None or right is None:
                    current[column] = right if left is None else left
                elif op == "sum":
                    current[column] = left + right
                elif op == "min":
                    current[column] = min(left, right)
                else:
                    current[column] = max(left, right)

    results: list[dict[str, Any]] = []
    for row in merged.values():
        if spec.finalize == "avg":
            total, count = _number(row.get("__sum")), _number(row.get("__count"))
            row["value"] = (total / count) if total is not None and count else None
        else:
            for column in spec.merge_ops:
                number = _number(row.get(column))
                if number is not None:
                    row[column] = number
        for column in _HIDDEN_COLUMNS:
            row.pop(column, None)
        results.append(row)

    if spec.sort_desc:
        results.sort(
            key=lambda r: (_number(r.get(spec.sort_column)) is None, -(_number(r.get(spec.sort_column)) or 0))
        )
    else:
        results.sort(key=lambda r: str(r.get(spec.sort_column)))
    return results[: spec.limit]


def _hours_between(start: datetime, end: datetime) -> float:
    return (end - start).total_seconds() / 3600


def _contiguous(pieces: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    ranges: list[tuple[datetime, datetime]] = []
    for start, end in sorted(pieces):
        if ranges and ranges[-1][1] >= start:
            ranges[-1] = (ranges[-1][0], max(ranges[-1][1], end))
        else:
            ranges.append((start, end))
    return ranges


def _split_ranges(
    ranges: list[tuple[datetime, datetime]], max_hours: float
) -> list[tuple[datetime, datetime]]:
    pieces: list[tuple[datetime, datetime]] = []
    step = timedelta(hours=max_hours)
    for start, end in ranges:
        cursor = start
        while cursor < end:
            nxt = min(end, cursor + step)
            pieces.append((cursor, nxt))
            cursor = nxt
    return pieces


def _round_down_hours(hours: float) -> float:
    if hours >= 24:
        return float(math.floor(hours / 24) * 24)
    if hours >= 1:
        return float(math.floor(hours))
    return max(0.25, math.floor(hours * 4) / 4)


def single_query_suggestion(window: TimeWindow, settings: Settings) -> dict[str, Any]:
    resolved = window.materialize()
    assert resolved.start is not None and resolved.end is not None
    max_hours = _round_down_hours(max(0.25, window.hours / 2))
    pieces = _split_ranges([(resolved.start, resolved.end)], max_hours)
    return {
        "message": (
            f"The {format_hours(round(window.hours, 2))}h window did not finish within the "
            f"{format_hours(settings.timeout_seconds)}s query timeout. Narrow the window to at most "
            f"{format_hours(max_hours)}h (e.g. the start/end pairs in windows, newest last) "
            "or add filters such as deployment, severity, or tenant."
        ),
        "maxHours": max_hours,
        "windows": windows_payload(pieces),
    }


def _chunk_suggestion(
    *,
    settings: Settings,
    spec_hours: float,
    completed: list[tuple[datetime, datetime]],
    remaining: list[tuple[datetime, datetime]],
    elapsed: float,
    chunk_timed_out: bool,
) -> dict[str, Any]:
    if chunk_timed_out:
        max_hours = _round_down_hours(max(0.25, spec_hours / 2))
        reason = (
            f"a single {format_hours(spec_hours)}h sub-window exceeded the "
            f"{format_hours(settings.timeout_seconds)}s per-query timeout"
        )
    else:
        done_hours = sum(_hours_between(s, e) for s, e in completed)
        if done_hours and elapsed > 0:
            capacity = done_hours / elapsed * settings.total_timeout_seconds * 0.8
            max_hours = _round_down_hours(max(spec_hours, capacity))
        else:
            max_hours = _round_down_hours(spec_hours)
        reason = (
            f"the whole window did not finish within the "
            f"{format_hours(settings.total_timeout_seconds)}s total budget"
        )
    pieces = _split_ranges(_contiguous(remaining), max_hours)
    return {
        "message": (
            f"Partial result: {reason}. Re-run the remaining part with windows of at most "
            f"{format_hours(max_hours)}h (about {round(max_hours / 24, 1)} days), e.g. the "
            "start/end pairs in windows, or add filters such as deployment or severity."
        ),
        "maxHours": max_hours,
        "windows": windows_payload(pieces),
    }


async def _execute_chunked(
    plan: StructuredPlan,
    settings: Settings,
    run_chunk: Runner,
    now: datetime | None,
) -> dict[str, Any]:
    spec = plan.chunk
    assert spec is not None
    window = plan.window.materialize(now)
    assert window.start is not None and window.end is not None
    chunk_hours = max(0.25, float(settings.chunk_hours))
    pieces = split_window(window.start, window.end, chunk_hours)
    last_index = len(pieces) - 1
    limiter = asyncio.Semaphore(max(1, int(settings.chunk_concurrency)))
    stop = asyncio.Event()
    results: dict[int, dict[str, Any]] = {}

    async def run_piece(index: int) -> None:
        async with limiter:
            if stop.is_set():
                return
            start, end = pieces[index]
            args = [
                "query",
                spec.build_sql(index != last_index),
                "--start",
                format_utc(start),
                "--end",
                format_utc(end),
            ]
            try:
                payload = await run_chunk(args, settings)
            except Exception as exc:  # noqa: BLE001 - reported as a structured failure
                payload = {"success": False, "code": "SZ_FAILED", "error": str(exc)}
            results[index] = payload
            if not payload.get("success"):
                stop.set()

    started = time.monotonic()
    tasks = [asyncio.create_task(run_piece(index)) for index in range(len(pieces))]
    _, pending = await asyncio.wait(tasks, timeout=max(1.0, float(settings.total_timeout_seconds)))
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    elapsed = time.monotonic() - started

    completed_idx = sorted(i for i, p in results.items() if p.get("success"))
    failed_idx = sorted(i for i, p in results.items() if not p.get("success"))
    remaining_idx = [i for i in range(len(pieces)) if i not in completed_idx]
    rows_by_chunk = [
        results[i]["data"] if isinstance(results[i].get("data"), list) else []
        for i in completed_idx
    ]
    approximate = any(len(rows) >= spec.row_limit for rows in rows_by_chunk)
    merged = merge_chunk_rows(rows_by_chunk, spec)
    chunking = {
        "applied": True,
        "chunkHours": chunk_hours,
        "chunks": len(pieces),
        "completed": len(completed_idx),
        "approximate": approximate,
    }
    if approximate:
        chunking["note"] = (
            f"at least one sub-window hit the {spec.row_limit}-row cap; "
            "merged values for rarely seen groups may be incomplete"
        )
    window_echo = plan.window.echo(now)

    if len(completed_idx) == len(pieces):
        return {"success": True, "data": merged, "window": window_echo, "chunking": chunking}

    first_failure = results[failed_idx[0]] if failed_idx else None
    chunk_timed_out = bool(first_failure and first_failure.get("code") == "SZ_TIMEOUT")
    total_timed_out = first_failure is None
    if total_timed_out:
        code = "SZ_TOTAL_TIMEOUT"
        error = (
            f"chunked query exceeded the {format_hours(settings.total_timeout_seconds)}s total budget "
            f"after {len(completed_idx)}/{len(pieces)} sub-windows"
        )
    else:
        code = str(first_failure.get("code") or "SZ_FAILED")
        start, end = pieces[failed_idx[0]]
        error = (
            f"sub-window {format_utc(start)}..{format_utc(end)} failed: "
            f"{first_failure.get('error') or code}"
        )
    payload: dict[str, Any] = {
        "success": False,
        "code": code,
        "error": error,
        "window": window_echo,
        "chunking": chunking,
        "partial": {
            "data": merged,
            "completedWindows": windows_payload(_contiguous([pieces[i] for i in completed_idx]), 50),
            "remainingWindows": windows_payload(_contiguous([pieces[i] for i in remaining_idx]), 50),
        },
    }
    if chunk_timed_out or total_timed_out:
        payload["suggestion"] = _chunk_suggestion(
            settings=settings,
            spec_hours=chunk_hours,
            completed=[pieces[i] for i in completed_idx],
            remaining=[pieces[i] for i in remaining_idx],
            elapsed=elapsed,
            chunk_timed_out=chunk_timed_out,
        )
    return payload


async def execute_plan(
    plan: StructuredPlan,
    settings: Settings,
    *,
    run_single: Runner,
    run_chunk: Runner,
    now: datetime | None = None,
) -> dict[str, Any]:
    threshold = max(0.0, float(settings.chunk_threshold_hours))
    long_window = plan.window.hours > threshold
    if plan.chunk is not None and long_window:
        return await _execute_chunked(plan, settings, run_chunk, now)

    payload = dict(await run_single(plan.args, settings))
    if payload.get("success") and plan.postprocess is not None:
        data, meta = plan.postprocess(payload.get("data"))
        payload["data"] = data
        payload["compaction"] = meta
    payload["window"] = plan.window.echo(now)
    if long_window and plan.chunk_unavailable_reason:
        payload["chunking"] = {"applied": False, "reason": plan.chunk_unavailable_reason}
    if not payload.get("success") and payload.get("code") == "SZ_TIMEOUT":
        payload["suggestion"] = single_query_suggestion(plan.window, settings)
    return payload
