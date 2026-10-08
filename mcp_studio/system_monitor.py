from __future__ import annotations

import asyncio
import os
import shutil
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_KIB = 1024
_MIB = 1024 * 1024


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_kib_fields(text: str) -> dict[str, int]:
    """Parse /proc-style `Key: <value> kB` records into KiB integers."""
    out: dict[str, int] = {}
    for raw in text.splitlines():
        if ":" not in raw:
            continue
        key, value = raw.split(":", 1)
        token = value.strip().split()
        if not token:
            continue
        try:
            out[key] = int(token[0])
        except ValueError:
            continue
    return out


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
        return ""


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
        return b""


def _proc_stat_fields(pid: int) -> tuple[int, int, int] | None:
    """Return (cpu_ticks, start_ticks, ppid) from /proc/<pid>/stat."""
    raw = _read_text(Path("/proc") / str(pid) / "stat").strip()
    if not raw:
        return None
    try:
        _, rest = raw.rsplit(")", 1)
        fields = rest.strip().split()
        # fields[0] is state (field 3 in procfs documentation).
        ppid = int(fields[1])
        utime = int(fields[11])
        stime = int(fields[12])
        starttime = int(fields[19])
        return utime + stime, starttime, ppid
    except (ValueError, IndexError):
        return None


def _host_cpu_ticks() -> tuple[int, int] | None:
    first = _read_text(Path("/proc/stat")).splitlines()
    if not first or not first[0].startswith("cpu "):
        return None
    try:
        values = [int(x) for x in first[0].split()[1:]]
    except ValueError:
        return None
    total = sum(values)
    idle = (values[3] if len(values) > 3 else 0) + (values[4] if len(values) > 4 else 0)
    return total, idle


def _process_uptime_seconds(pid: int, start_ticks: int | None = None) -> float | None:
    stat = _proc_stat_fields(pid)
    if stat is None and start_ticks is None:
        return None
    if start_ticks is None:
        start_ticks = stat[1] if stat else None
    if start_ticks is None:
        return None
    try:
        uptime = float(_read_text(Path("/proc/uptime")).split()[0])
        hz = float(os.sysconf("SC_CLK_TCK"))
        return max(0.0, uptime - (start_ticks / hz))
    except (ValueError, IndexError, OSError):
        return None


def _process_summary(pid: int) -> dict[str, Any] | None:
    proc = Path("/proc") / str(pid)
    status_text = _read_text(proc / "status")
    if not status_text:
        return None
    fields = parse_kib_fields(status_text)
    status_lines: dict[str, str] = {}
    for line in status_text.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            status_lines[key] = value.strip()
    cmdline = _read_bytes(proc / "cmdline").replace(b"\0", b" ").decode("utf-8", "replace").strip()
    comm = _read_text(proc / "comm").strip()
    return {
        "pid": pid,
        "name": comm or status_lines.get("Name") or str(pid),
        "cmdline": cmdline[:320],
        "rss_mib": round(fields.get("VmRSS", 0) / 1024.0, 2),
        "swap_mib": round(fields.get("VmSwap", 0) / 1024.0, 2),
        "data_mib": round(fields.get("VmData", 0) / 1024.0, 2),
        "threads": int(status_lines.get("Threads", "0") or 0),
    }


def _direct_children(pid: int) -> list[int]:
    raw = _read_text(Path("/proc") / str(pid) / "task" / str(pid) / "children")
    out: list[int] = []
    for token in raw.split():
        try:
            out.append(int(token))
        except ValueError:
            continue
    return out


class SystemHealthSampler:
    """Low-overhead host/process sampler with bounded in-memory history.

    This intentionally never reads the HIRDA event ledger and never persists
    samples to SQLite. It is safe for frequent UI polling because API reads only
    copy the current in-memory snapshot/ring buffer.
    """

    def __init__(
        self,
        *,
        database_path: str | Path,
        interval_seconds: float = 10.0,
        history_limit: int = 360,
        process_pid: int | None = None,
        root_path: str | Path = "/",
    ):
        self.database_path = Path(database_path)
        self.interval_seconds = max(2.0, float(interval_seconds))
        self.history_limit = max(6, int(history_limit))
        self.process_pid = int(process_pid or os.getpid())
        self.root_path = Path(root_path)
        self._history: deque[dict[str, Any]] = deque(maxlen=self.history_limit)
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._last_proc_cpu: tuple[float, int] | None = None
        self._last_host_cpu: tuple[int, int] | None = None
        self.current: dict[str, Any] = {}
        self.last_error: str | None = None

    async def start(self) -> None:
        self._stopping.clear()
        self.sample_once()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="hirda-system-health-sampler")

    async def stop(self) -> None:
        self._stopping.set()
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        while not self._stopping.is_set():
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self.interval_seconds)
            except asyncio.TimeoutError:
                try:
                    self.sample_once()
                    self.last_error = None
                except Exception as exc:  # sampler must never take down HIRDA
                    self.last_error = f"{type(exc).__name__}: {exc}"

    def _process_cpu_percent(self, ticks: int, now: float) -> float | None:
        prev = self._last_proc_cpu
        self._last_proc_cpu = (now, ticks)
        if prev is None:
            return None
        elapsed = now - prev[0]
        delta_ticks = ticks - prev[1]
        if elapsed <= 0 or delta_ticks < 0:
            return None
        try:
            hz = float(os.sysconf("SC_CLK_TCK"))
        except OSError:
            return None
        return round((delta_ticks / hz) / elapsed * 100.0, 2)

    def _host_cpu_percent(self, current: tuple[int, int] | None) -> float | None:
        if current is None:
            return None
        prev = self._last_host_cpu
        self._last_host_cpu = current
        if prev is None:
            return None
        delta_total = current[0] - prev[0]
        delta_idle = current[1] - prev[1]
        if delta_total <= 0:
            return None
        return round(max(0.0, min(100.0, (delta_total - delta_idle) / delta_total * 100.0)), 2)

    def _alerts(self, sample: dict[str, Any]) -> list[dict[str, str]]:
        alerts: list[dict[str, str]] = []
        proc = sample["process"]
        host = sample["host"]
        disk = sample["disk"]

        def add(kind: str, severity: str, message: str) -> None:
            alerts.append({"kind": kind, "severity": severity, "message": message})

        cpu = proc.get("cpu_percent")
        if cpu is not None and cpu >= 80:
            add("process_cpu", "critical", f"HIRDA CPU is {cpu:.1f}%")
        elif cpu is not None and cpu >= 50:
            add("process_cpu", "warning", f"HIRDA CPU is {cpu:.1f}%")

        total_mem = float(proc.get("rss_plus_swap_mib") or 0)
        if total_mem >= 1536:
            add("process_memory", "critical", f"HIRDA RSS+swap is {total_mem:.0f} MiB")
        elif total_mem >= 1024:
            add("process_memory", "warning", f"HIRDA RSS+swap is {total_mem:.0f} MiB")

        swap_mib = float(proc.get("swap_mib") or 0)
        if swap_mib >= 1024:
            add("process_swap", "critical", f"HIRDA process swap is {swap_mib:.0f} MiB")
        elif swap_mib >= 512:
            add("process_swap", "warning", f"HIRDA process swap is {swap_mib:.0f} MiB")

        available_ratio = host.get("memory_available_ratio")
        if available_ratio is not None and available_ratio <= 0.10:
            add("host_memory", "critical", f"Host available memory is {available_ratio * 100:.1f}%")
        elif available_ratio is not None and available_ratio <= 0.20:
            add("host_memory", "warning", f"Host available memory is {available_ratio * 100:.1f}%")

        free_ratio = disk.get("free_ratio")
        if free_ratio is not None and free_ratio <= 0.05:
            add("disk", "critical", f"Root disk free space is {free_ratio * 100:.1f}%")
        elif free_ratio is not None and free_ratio <= 0.10:
            add("disk", "warning", f"Root disk free space is {free_ratio * 100:.1f}%")
        return alerts

    def sample_once(self) -> dict[str, Any]:
        now_mono = time.monotonic()
        pid = self.process_pid
        proc_status_text = _read_text(Path("/proc") / str(pid) / "status")
        proc_fields = parse_kib_fields(proc_status_text)
        proc_stat = _proc_stat_fields(pid)
        cpu_ticks = proc_stat[0] if proc_stat else 0
        start_ticks = proc_stat[1] if proc_stat else None

        mem = parse_kib_fields(_read_text(Path("/proc/meminfo")))
        mem_total = mem.get("MemTotal", 0)
        mem_available = mem.get("MemAvailable", 0)
        swap_total = mem.get("SwapTotal", 0)
        swap_free = mem.get("SwapFree", 0)

        try:
            load1, load5, load15 = os.getloadavg()
        except OSError:
            load1 = load5 = load15 = 0.0

        try:
            disk = shutil.disk_usage(self.root_path)
        except OSError:
            disk = shutil.disk_usage("/")

        db = self.database_path
        db_bytes = db.stat().st_size if db.exists() else 0
        wal = Path(str(db) + "-wal")
        wal_bytes = wal.stat().st_size if wal.exists() else 0

        rss_mib = proc_fields.get("VmRSS", 0) / 1024.0
        swap_mib = proc_fields.get("VmSwap", 0) / 1024.0
        data_mib = proc_fields.get("VmData", 0) / 1024.0
        status_lines: dict[str, str] = {}
        for line in proc_status_text.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                status_lines[k] = v.strip()

        children = [x for child in _direct_children(pid) if (x := _process_summary(child)) is not None]
        children.sort(key=lambda row: (row["rss_mib"] + row["swap_mib"]), reverse=True)

        host_cpu = self._host_cpu_percent(_host_cpu_ticks())
        process_cpu = self._process_cpu_percent(cpu_ticks, now_mono) if proc_stat else None
        allocator = os.environ.get("MALLOC_ARENA_MAX")
        allocator_mode = "B" if allocator else "A"

        sample: dict[str, Any] = {
            "observed_at": _iso_now(),
            "process": {
                "pid": pid,
                "cpu_percent": process_cpu,
                "rss_mib": round(rss_mib, 2),
                "swap_mib": round(swap_mib, 2),
                "rss_plus_swap_mib": round(rss_mib + swap_mib, 2),
                "data_mib": round(data_mib, 2),
                "vsize_mib": round(proc_fields.get("VmSize", 0) / 1024.0, 2),
                "threads": int(status_lines.get("Threads", "0") or 0),
                "uptime_seconds": round(_process_uptime_seconds(pid, start_ticks) or 0.0, 1),
            },
            "host": {
                "cpu_percent": host_cpu,
                "load_1m": round(load1, 3),
                "load_5m": round(load5, 3),
                "load_15m": round(load15, 3),
                "logical_cpu_count": os.cpu_count() or 1,
                "memory_total_mib": round(mem_total / 1024.0, 2),
                "memory_available_mib": round(mem_available / 1024.0, 2),
                "memory_available_ratio": round(mem_available / mem_total, 4) if mem_total else None,
                "swap_total_mib": round(swap_total / 1024.0, 2),
                "swap_used_mib": round((swap_total - swap_free) / 1024.0, 2),
            },
            "disk": {
                "total_gib": round(disk.total / (1024.0 ** 3), 2),
                "used_gib": round(disk.used / (1024.0 ** 3), 2),
                "free_gib": round(disk.free / (1024.0 ** 3), 2),
                "free_ratio": round(disk.free / disk.total, 4) if disk.total else None,
            },
            "database": {
                "path": str(db),
                "main_mib": round(db_bytes / _MIB, 2),
                "wal_mib": round(wal_bytes / _MIB, 2),
            },
            "allocator": {
                "experiment": allocator_mode,
                "malloc_arena_max": allocator,
                "label": f"B · MALLOC_ARENA_MAX={allocator}" if allocator else "A · default allocator",
            },
            "children": children[:20],
        }
        sample["alerts"] = self._alerts(sample)
        self.current = sample
        self._history.append(
            {
                "observed_at": sample["observed_at"],
                "process_cpu_percent": process_cpu,
                "rss_mib": sample["process"]["rss_mib"],
                "swap_mib": sample["process"]["swap_mib"],
                "rss_plus_swap_mib": sample["process"]["rss_plus_swap_mib"],
                "data_mib": sample["process"]["data_mib"],
                "host_cpu_percent": host_cpu,
                "memory_available_mib": sample["host"]["memory_available_mib"],
                "disk_free_gib": sample["disk"]["free_gib"],
            }
        )
        return sample

    def snapshot(self) -> dict[str, Any]:
        return {
            "status": "healthy" if not self.last_error else "degraded",
            "interval_seconds": self.interval_seconds,
            "history_limit": self.history_limit,
            "history_count": len(self._history),
            "last_error": self.last_error,
            "current": dict(self.current),
            "history": list(self._history),
        }
