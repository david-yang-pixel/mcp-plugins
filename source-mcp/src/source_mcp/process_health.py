from __future__ import annotations

from pathlib import Path
from typing import Any


def process_health_snapshot(
    *,
    proc_root: Path = Path("/proc"),
    cgroup_root: Path = Path("/sys/fs/cgroup"),
    zombie_warning_threshold: int = 10,
    pids_warning_ratio: float = 0.8,
) -> dict[str, Any]:
    zombie_count = _count_zombies(proc_root)
    pids_current = _read_pid_value(cgroup_root / "pids.current")
    pids_max = _read_pid_value(cgroup_root / "pids.max")
    pids_usage_ratio = None
    if pids_current is not None and pids_max:
        pids_usage_ratio = round(pids_current / pids_max, 4)

    degraded_reasons: list[str] = []
    if zombie_warning_threshold > 0 and zombie_count >= zombie_warning_threshold:
        degraded_reasons.append("zombie_count")
    if (
        pids_usage_ratio is not None
        and pids_warning_ratio > 0
        and pids_usage_ratio >= pids_warning_ratio
    ):
        degraded_reasons.append("pids_usage_ratio")

    return {
        "zombie_count": zombie_count,
        "pids_current": pids_current,
        "pids_max": pids_max,
        "pids_usage_ratio": pids_usage_ratio,
        "degraded": bool(degraded_reasons),
        "degraded_reasons": degraded_reasons,
    }


def _count_zombies(proc_root: Path) -> int:
    zombie_count = 0
    for stat_path in proc_root.glob("[0-9]*/stat"):
        try:
            stat = stat_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        _, separator, remaining = stat.rpartition(")")
        if separator and remaining.split(maxsplit=1)[:1] == ["Z"]:
            zombie_count += 1
    return zombie_count


def _read_pid_value(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw or raw == "max":
        return None
    try:
        return int(raw)
    except ValueError:
        return None
