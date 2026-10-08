from pathlib import Path

from mcp_studio.system_monitor import SystemHealthSampler, parse_kib_fields


def test_parse_kib_fields_handles_proc_status():
    parsed = parse_kib_fields(
        "VmSize:\t2532944 kB\nVmRSS:\t105620 kB\nVmSwap:\t1741004 kB\nThreads:\t19\n"
    )
    assert parsed["VmSize"] == 2532944
    assert parsed["VmRSS"] == 105620
    assert parsed["VmSwap"] == 1741004
    assert parsed["Threads"] == 19


def test_sampler_keeps_bounded_in_memory_history(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MALLOC_ARENA_MAX", "2")
    sampler = SystemHealthSampler(
        database_path=tmp_path / "studio.sqlite3",
        history_limit=6,
        interval_seconds=10,
    )
    for _ in range(10):
        sample = sampler.sample_once()

    snapshot = sampler.snapshot()
    assert snapshot["history_count"] == 6
    assert len(snapshot["history"]) == 6
    assert sample["allocator"]["experiment"] == "B"
    assert sample["allocator"]["malloc_arena_max"] == "2"
    assert sample["process"]["pid"] > 0
    assert sample["process"]["rss_plus_swap_mib"] >= sample["process"]["rss_mib"]
    assert "memory_available_ratio" in sample["host"]
    assert "free_ratio" in sample["disk"]
    assert snapshot["status"] == "healthy"


def test_sampler_snapshot_is_api_ready_and_has_no_ledger_dependency(tmp_path: Path):
    module_text = (
        Path(__file__).resolve().parents[1] / "mcp_studio" / "system_monitor.py"
    ).read_text(encoding="utf-8")
    assert "FROM events" not in module_text
    assert "Database(" not in module_text
    assert "add_event" not in module_text

    sampler = SystemHealthSampler(database_path=tmp_path / "studio.sqlite3")
    sampler.sample_once()
    payload = sampler.snapshot()

    assert set(payload) == {
        "status",
        "interval_seconds",
        "history_limit",
        "history_count",
        "last_error",
        "current",
        "history",
    }
    assert {"process", "host", "disk", "database", "allocator", "children", "alerts"} <= set(
        payload["current"]
    )
