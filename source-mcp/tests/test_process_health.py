from __future__ import annotations

from source_mcp.process_health import process_health_snapshot


def test_process_health_reports_zombies_and_pid_pressure(tmp_path):
    proc_root = tmp_path / "proc"
    cgroup_root = tmp_path / "cgroup"
    (proc_root / "1").mkdir(parents=True)
    (proc_root / "2").mkdir()
    cgroup_root.mkdir()
    (proc_root / "1" / "stat").write_text("1 (python) S 0 0 0\n", encoding="utf-8")
    (proc_root / "2" / "stat").write_text("2 (git worker) Z 0 0 0\n", encoding="utf-8")
    (cgroup_root / "pids.current").write_text("85\n", encoding="utf-8")
    (cgroup_root / "pids.max").write_text("100\n", encoding="utf-8")

    health = process_health_snapshot(
        proc_root=proc_root,
        cgroup_root=cgroup_root,
        zombie_warning_threshold=1,
        pids_warning_ratio=0.8,
    )

    assert health == {
        "zombie_count": 1,
        "pids_current": 85,
        "pids_max": 100,
        "pids_usage_ratio": 0.85,
        "degraded": True,
        "degraded_reasons": ["zombie_count", "pids_usage_ratio"],
    }


def test_process_health_supports_unlimited_or_unavailable_pid_limit(tmp_path):
    proc_root = tmp_path / "proc"
    cgroup_root = tmp_path / "cgroup"
    proc_root.mkdir()
    cgroup_root.mkdir()
    (cgroup_root / "pids.current").write_text("3\n", encoding="utf-8")
    (cgroup_root / "pids.max").write_text("max\n", encoding="utf-8")

    health = process_health_snapshot(
        proc_root=proc_root,
        cgroup_root=cgroup_root,
        zombie_warning_threshold=10,
        pids_warning_ratio=0.8,
    )

    assert health["pids_current"] == 3
    assert health["pids_max"] is None
    assert health["pids_usage_ratio"] is None
    assert health["degraded"] is False
