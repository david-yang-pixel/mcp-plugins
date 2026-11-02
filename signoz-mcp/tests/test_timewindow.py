from __future__ import annotations

from datetime import datetime, timezone

import pytest

from signoz_mcp.timewindow import format_utc, parse_iso_utc, resolve_window, split_window


NOW = datetime(2026, 11, 2, 12, 30, tzinfo=timezone.utc)
MAX = 31 * 24.0


def _utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-10-01", _utc(2026, 10, 1)),
        ("2026-10-01T08:00:00", _utc(2026, 10, 1, 8)),
        ("2026-10-01 08:00", _utc(2026, 10, 1, 8)),
        ("2026-10-01T08:00:00Z", _utc(2026, 10, 1, 8)),
        ("2026-10-01T16:00:00+08:00", _utc(2026, 10, 1, 8)),
    ],
)
def test_parse_iso_treats_naive_as_utc(raw, expected):
    assert parse_iso_utc(raw, "start") == expected


@pytest.mark.parametrize("raw", ["", "yesterday", "1696118400", None, 5])
def test_parse_iso_rejects_non_iso(raw):
    with pytest.raises(ValueError, match="start"):
        parse_iso_utc(raw, "start")


def test_relative_window_keeps_sz_hours_form():
    window = resolve_window({"timeRange": "30m"}, "1h", max_window_hours=MAX, now=NOW)

    assert window.mode == "relative"
    assert window.sz_args() == ["--hours", "0.5"]
    echo = window.echo(NOW)
    assert echo["start"] == "2026-11-02T12:00:00Z"
    assert echo["end"] == "2026-11-02T12:30:00Z"


def test_absolute_window_and_echo():
    window = resolve_window(
        {"start": "2026-10-01T00:00:00", "end": "2026-10-01T16:00:00+08:00"},
        "1h",
        max_window_hours=MAX,
        now=NOW,
    )

    assert window.sz_args() == ["--start", "2026-10-01T00:00:00Z", "--end", "2026-10-01T08:00:00Z"]
    assert window.echo() == {
        "mode": "absolute",
        "start": "2026-10-01T00:00:00Z",
        "end": "2026-10-01T08:00:00Z",
        "hours": 8,
    }


def test_start_end_take_precedence_over_time_range():
    window = resolve_window(
        {"start": "2026-10-01T00:00:00Z", "end": "2026-10-02T00:00:00Z", "timeRange": "1h"},
        "1h",
        max_window_hours=MAX,
        now=NOW,
    )

    assert window.hours == 24
    assert window.echo()["timeRangeIgnored"] is True


def test_start_only_ends_now_and_end_only_uses_time_range():
    start_only = resolve_window({"start": "2026-11-01T12:30:00Z"}, "1h", max_window_hours=MAX, now=NOW)
    end_only = resolve_window(
        {"end": "2026-10-01T06:00:00Z", "timeRange": "6h"}, "1h", max_window_hours=MAX, now=NOW
    )

    assert start_only.end == NOW and start_only.hours == 24
    assert format_utc(end_only.start) == "2026-10-01T00:00:00Z"


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"start": "2026-10-02T00:00:00Z", "end": "2026-10-01T00:00:00Z"}, "earlier"),
        ({"start": "2026-10-01T00:00:00Z", "end": "2026-10-01T00:00:00Z"}, "earlier"),
        ({"start": "2026-08-01T00:00:00Z", "end": "2026-10-01T00:00:00Z"}, "maximum"),
    ],
)
def test_absolute_window_validation(arguments, message):
    with pytest.raises(ValueError, match=message):
        resolve_window(arguments, "1h", max_window_hours=MAX, now=NOW)


def test_long_time_range_becomes_absolute_and_is_clamped():
    thirty_days = resolve_window({"timeRange": "30d"}, "1h", max_window_hours=MAX, now=NOW)
    too_long = resolve_window({"timeRange": "90d"}, "1h", max_window_hours=MAX, now=NOW)

    assert thirty_days.mode == "absolute"
    assert thirty_days.sz_args()[0] == "--start"
    assert thirty_days.hours == 720
    assert too_long.hours == MAX
    assert too_long.echo()["clamped"] is True


def test_split_window_aligns_inner_boundaries_to_utc_midnight():
    pieces = split_window(_utc(2026, 10, 1, 13, 47), _utc(2026, 10, 4, 6), 24)

    assert [(format_utc(a), format_utc(b)) for a, b in pieces] == [
        ("2026-10-01T13:47:00Z", "2026-10-02T00:00:00Z"),
        ("2026-10-02T00:00:00Z", "2026-10-03T00:00:00Z"),
        ("2026-10-03T00:00:00Z", "2026-10-04T00:00:00Z"),
        ("2026-10-04T00:00:00Z", "2026-10-04T06:00:00Z"),
    ]


def test_split_window_exact_boundaries():
    pieces = split_window(_utc(2026, 10, 1), _utc(2026, 10, 3), 24)

    assert len(pieces) == 2
    assert pieces[-1][1] == _utc(2026, 10, 3)
