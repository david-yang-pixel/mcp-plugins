"""Offline tests for scripts/sz; post_query is replaced so no network is used."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
from pathlib import Path

import pytest


SZ_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sz"
NOW = 1_790_000_000  # fixed epoch seconds


@pytest.fixture
def sz(monkeypatch):
    loader = importlib.machinery.SourceFileLoader("sz_cli_script_tests", str(SZ_SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    monkeypatch.setattr(module.time, "time", lambda: NOW + 0.75)
    monkeypatch.setenv("SIGNOZ_API_KEY", "sn_test")
    monkeypatch.delenv("SZ_MAX_WINDOW_DAYS", raising=False)
    return module


@pytest.fixture
def captured(sz, monkeypatch):
    calls = []

    def fake_post_query(sql, start_ms, end_ms, panel_type="list"):
        calls.append({"sql": sql, "start_ms": start_ms, "end_ms": end_ms, "panel": panel_type})
        return {"data": {"result": [{"list": [{"data": {"n": 1}}]}]}}

    monkeypatch.setattr(sz, "post_query", fake_post_query)
    return calls


def test_query_hours_path_is_unchanged(sz, captured, capsys):
    sz.main(["query", "SELECT {start_ns} AS s, {end_ns} AS e", "--hours", "3", "--json", "-q"])

    assert json.loads(capsys.readouterr().out) == [{"n": 1}]
    start_ns = (NOW - 3 * 3600) * 1_000_000_000
    end_ns = NOW * 1_000_000_000
    assert captured[0]["sql"] == f"SELECT {start_ns} AS s, {end_ns} AS e"
    assert captured[0]["start_ms"] == (NOW - 3 * 3600) * 1000
    assert captured[0]["end_ms"] == NOW * 1000


def test_hours_is_still_clamped_to_168(sz, captured, capsys):
    sz.main(["query", "SELECT {start_ns}", "--hours", "1000", "--json", "-q"])

    assert captured[0]["sql"] == f"SELECT {(NOW - 168 * 3600) * 1_000_000_000}"


def test_absolute_window_ns_and_ms_values(sz, captured, capsys):
    sz.main([
        "query", "SELECT {start_ns}, {end_ns}",
        "--start", "2026-10-01T00:00:00Z", "--end", "2026-10-02T00:00:00.5Z",
        "--json", "-q",
    ])

    start_s = 1_790_812_800
    assert captured[0]["sql"] == (
        f"SELECT {start_s * 1_000_000_000}, {(start_s + 86400) * 1_000_000_000 + 500_000_000}"
    )
    assert captured[0]["start_ms"] == start_s * 1000
    assert captured[0]["end_ms"] == (start_s + 86400) * 1000 + 500


def test_end_only_uses_hours_as_duration(sz, captured, capsys):
    sz.main(["errors", "--end", "2026-10-01T06:00:00Z", "--hours", "6", "--json", "-q"])

    start_ns = 1_790_812_800 * 1_000_000_000
    end_ns = (1_790_812_800 + 6 * 3600) * 1_000_000_000
    assert f"timestamp >= {start_ns}" in captured[0]["sql"]
    assert f"timestamp <= {end_ns}" in captured[0]["sql"]


def test_start_only_ends_now(sz, captured, capsys):
    sz.main(["services", "--start", "2026-09-20T00:00:00Z", "--json", "-q"])

    assert f"timestamp <= {int((NOW + 0.75) * 1e6) * 1000}" in captured[0]["sql"]


@pytest.mark.parametrize(
    "argv, message",
    [
        (["--start", "2026-10-02T00:00:00Z", "--end", "2026-10-01T00:00:00Z"], "earlier"),
        (["--start", "2026-10-01T00:00:00Z", "--end", "2026-10-01T08:00:00+08:00"], "earlier"),
        (["--start", "2026-08-01T00:00:00Z", "--end", "2026-10-01T00:00:00Z"], "31 days"),
        (["--start", "yesterday"], "ISO 8601"),
    ],
)
def test_invalid_absolute_windows_exit_with_message(sz, captured, capsys, argv, message):
    with pytest.raises(SystemExit) as excinfo:
        sz.main(["query", "SELECT 1", *argv, "--json", "-q"])

    assert excinfo.value.code == 2
    assert message in capsys.readouterr().err
    assert captured == []


def test_max_window_days_is_configurable(sz, captured, capsys, monkeypatch):
    monkeypatch.setenv("SZ_MAX_WINDOW_DAYS", "90")

    sz.main([
        "query", "SELECT 1",
        "--start", "2026-08-01T00:00:00Z", "--end", "2026-10-01T00:00:00Z",
        "--json", "-q",
    ])

    assert len(captured) == 1
