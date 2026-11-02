"""Time window parsing shared by the structured tools and the chunked executor."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any


SZ_MAX_RELATIVE_HOURS = 168.0  # scripts/sz clamps --hours to this value.
_TIME_RANGE_RE = re.compile(r"^(\d+(?:\.\d+)?)([mhd])$")
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def parse_iso_utc(value: Any, field_name: str) -> datetime:
    """Parse ISO 8601; values without an offset are treated as UTC."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be an ISO 8601 string such as 2026-10-01T08:00:00Z")
    raw = value.strip()
    if len(raw) > 64:
        raise ValueError(f"{field_name} is too long")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(
            f"{field_name} must be ISO 8601, e.g. 2026-10-01, 2026-10-01T08:00:00 (UTC) "
            "or 2026-10-01T16:00:00+08:00"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    if parsed < _EPOCH:
        raise ValueError(f"{field_name} must not be before 1970-01-01")
    return parsed


def format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def format_hours(hours: float) -> str:
    return str(int(hours)) if float(hours).is_integer() else str(hours)


def parse_time_range_hours(value: Any, default: str) -> float:
    raw = default if value in (None, "") else str(value).strip().lower()
    match = _TIME_RANGE_RE.fullmatch(raw)
    if not match:
        raise ValueError("timeRange must look like 30m, 2h, or 1d")
    amount = float(match.group(1))
    unit = match.group(2)
    hours = amount / 60 if unit == "m" else amount * 24 if unit == "d" else amount
    return max(0.01, hours)


@dataclass(frozen=True)
class TimeWindow:
    mode: str  # "relative" (sz --hours) or "absolute" (sz --start/--end)
    hours: float
    start: datetime | None = None
    end: datetime | None = None
    time_range: str | None = None
    time_range_ignored: bool = False
    clamped: bool = False

    @property
    def is_absolute(self) -> bool:
        return self.mode == "absolute"

    def sz_args(self) -> list[str]:
        if self.is_absolute:
            assert self.start is not None and self.end is not None
            return ["--start", format_utc(self.start), "--end", format_utc(self.end)]
        return ["--hours", format_hours(self.hours)]

    def materialize(self, now: datetime | None = None) -> "TimeWindow":
        """Return an absolute window; relative windows end at ``now``."""
        if self.is_absolute:
            return self
        end = now or utc_now()
        start = end - timedelta(hours=self.hours)
        return TimeWindow(
            mode="absolute",
            hours=self.hours,
            start=start,
            end=end,
            time_range=self.time_range,
            clamped=self.clamped,
        )

    def echo(self, now: datetime | None = None) -> dict[str, Any]:
        resolved = self.materialize(now)
        assert resolved.start is not None and resolved.end is not None
        payload: dict[str, Any] = {
            "mode": self.mode,
            "start": format_utc(resolved.start),
            "end": format_utc(resolved.end),
            "hours": round(self.hours, 4),
        }
        if self.time_range is not None:
            payload["timeRange"] = self.time_range
        if not self.is_absolute:
            payload["note"] = "relative window; start/end are approximate (computed at request time)"
        if self.time_range_ignored:
            payload["timeRangeIgnored"] = True
        if self.clamped:
            payload["clamped"] = True
        return payload


def resolve_window(
    arguments: dict[str, Any],
    default_range: str,
    *,
    max_window_hours: float,
    now: datetime | None = None,
) -> TimeWindow:
    """Resolve start/end/timeRange. start/end take precedence over timeRange.

    - start + end: absolute window.
    - start only: start .. now.
    - end only: (end - timeRange or default) .. end.
    - timeRange only: relative window; windows longer than sz's 168h --hours cap
      become an absolute window ending now. Values above the configured maximum
      are clamped (as before) and flagged.
    """
    start_raw = arguments.get("start")
    end_raw = arguments.get("end")
    time_range_raw = arguments.get("timeRange")
    time_range = None if time_range_raw in (None, "") else str(time_range_raw).strip()
    has_start = start_raw not in (None, "")
    has_end = end_raw not in (None, "")

    if has_start or has_end:
        current = now or utc_now()
        end = parse_iso_utc(end_raw, "end") if has_end else current
        if has_start:
            start = parse_iso_utc(start_raw, "start")
        else:
            start = end - timedelta(hours=parse_time_range_hours(time_range_raw, default_range))
        if start >= end:
            raise ValueError(
                f"start ({format_utc(start)}) must be earlier than end ({format_utc(end)})"
            )
        hours = (end - start).total_seconds() / 3600
        if hours > max_window_hours + 1e-9:
            raise ValueError(
                f"time window is {hours:.1f}h; the maximum is {format_hours(max_window_hours)}h "
                f"({format_hours(round(max_window_hours / 24, 2))} days). "
                "Split it into several start/end windows."
            )
        return TimeWindow(
            mode="absolute",
            hours=hours,
            start=start,
            end=end,
            time_range=time_range if not has_start else None,
            time_range_ignored=has_start and has_end and time_range is not None,
        )

    hours = parse_time_range_hours(time_range_raw, default_range)
    clamped = hours > max_window_hours
    hours = min(hours, max_window_hours)
    if hours <= SZ_MAX_RELATIVE_HOURS:
        return TimeWindow(mode="relative", hours=hours, time_range=time_range, clamped=clamped)
    current = now or utc_now()
    return TimeWindow(
        mode="absolute",
        hours=hours,
        start=current - timedelta(hours=hours),
        end=current,
        time_range=time_range,
        clamped=clamped,
    )


def split_window(start: datetime, end: datetime, chunk_hours: float) -> list[tuple[datetime, datetime]]:
    """Split [start, end] into sub-windows whose inner boundaries are aligned to
    multiples of ``chunk_hours`` since the Unix epoch (UTC midnight for 24h)."""
    chunk_seconds = max(60, int(round(chunk_hours * 3600)))
    start_s = (start - _EPOCH).total_seconds()
    boundary_s = (math.floor(start_s / chunk_seconds) + 1) * chunk_seconds
    pieces: list[tuple[datetime, datetime]] = []
    cursor = start
    while True:
        boundary = _EPOCH + timedelta(seconds=boundary_s)
        if boundary >= end:
            pieces.append((cursor, end))
            return pieces
        pieces.append((cursor, boundary))
        cursor = boundary
        boundary_s += chunk_seconds


def windows_payload(pieces: list[tuple[datetime, datetime]], max_items: int = 12) -> list[dict[str, str]]:
    return [
        {"start": format_utc(piece_start), "end": format_utc(piece_end)}
        for piece_start, piece_end in pieces[:max_items]
    ]
