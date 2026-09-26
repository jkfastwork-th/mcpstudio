from __future__ import annotations

import asyncio
import ctypes
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .node_backends import normalize_backend_probe_specs


NODE_HEALTH_SCHEMA = "hirda-node-health-v1"
DEFAULT_STALE_AFTER_SECONDS = 45.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_iso(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _ratio(numerator: int | float | None, denominator: int | float | None) -> float | None:
    if numerator is None or denominator in (None, 0):
        return None
    try:
        value = float(numerator) / float(denominator)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return round(max(0.0, value), 6)


def _memory_snapshot() -> tuple[int | None, int | None]:
    if os.name == "nt":
        class MemoryStatusEx(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        try:
            status = MemoryStatusEx()
            status.dwLength = ctypes.sizeof(MemoryStatusEx)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.ullTotalPhys), int(status.ullAvailPhys)
        except (AttributeError, OSError, ValueError):
            return None, None
        return None, None

    meminfo = Path("/proc/meminfo")
    if meminfo.is_file():
        values: dict[str, int] = {}
        try:
            for line in meminfo.read_text(encoding="utf-8").splitlines():
                key, _, value = line.partition(":")
                if key in {"MemTotal", "MemAvailable"}:
                    amount = value.strip().split()[0]
                    values[key] = int(amount) * 1024
        except (OSError, ValueError, IndexError):
            return None, None
        return values.get("MemTotal"), values.get("MemAvailable")

    return None, None


def _storage_snapshot(storage_path: str | Path | None) -> dict[str, Any]:
    target = Path(storage_path or Path.cwd()).expanduser().resolve(strict=False)
    try:
        usage = shutil.disk_usage(target)
    except (FileNotFoundError, OSError):
        return {
            "total_bytes": None,
            "free_bytes": None,
            "free_ratio": None,
        }
    return {
        "total_bytes": int(usage.total),
        "free_bytes": int(usage.free),
        "free_ratio": _ratio(usage.free, usage.total),
    }


def _load_snapshot() -> dict[str, Any]:
    logical_count = int(os.cpu_count() or 0) or None
    try:
        load_1m, load_5m, load_15m = os.getloadavg()
    except (AttributeError, OSError):
        load_1m = load_5m = load_15m = None
    total_memory, available_memory = _memory_snapshot()
    return {
        "logical_cpu_count": logical_count,
        "load_1m": round(load_1m, 6) if load_1m is not None else None,
        "load_5m": round(load_5m, 6) if load_5m is not None else None,
        "load_15m": round(load_15m, 6) if load_15m is not None else None,
        "load_ratio_1m": _ratio(load_1m, logical_count),
        "memory_total_bytes": total_memory,
        "memory_available_bytes": available_memory,
        "memory_available_ratio": _ratio(available_memory, total_memory),
    }


def _resolve_binary(command: list[str]) -> str | None:
    if not command:
        return None
    first = command[0]
    candidate = Path(first).expanduser()
    if candidate.is_absolute():
        return str(candidate) if candidate.is_file() else None
    if os.sep in first or (os.altsep and os.altsep in first):
        resolved = candidate.resolve(strict=False)
        return str(resolved) if resolved.is_file() else None
    return shutil.which(first)


def _backend_health(spec: dict[str, Any]) -> dict[str, Any]:
    backend_id = str(spec["id"])
    binary = _resolve_binary(spec["command"])
    if not binary:
        return {
            "backend_id": backend_id,
            "available": False,
            "state": "backend_unavailable",
            "latency_ms": None,
        }

    health_probe = spec.get("health_probe")
    if not isinstance(health_probe, dict):
        return {
            "backend_id": backend_id,
            "available": True,
            "state": "available",
            "latency_ms": None,
        }

    probe_binary = _resolve_binary(health_probe["command"])
    if not probe_binary:
        return {
            "backend_id": backend_id,
            "available": False,
            "state": "probe_unavailable",
            "latency_ms": None,
        }

    argv = [probe_binary, *health_probe["command"][1:]]
    started = time.monotonic()
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=health_probe["timeout_seconds"],
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {
            "backend_id": backend_id,
            "available": False,
            "state": "probe_failed",
            "latency_ms": round((time.monotonic() - started) * 1000, 3),
        }

    latency_ms = round((time.monotonic() - started) * 1000, 3)
    return {
        "backend_id": backend_id,
        "available": proc.returncode == 0,
        "state": "ready" if proc.returncode == 0 else "unhealthy",
        "latency_ms": latency_ms,
    }


def probe_node_health(
    raw_specs: Any,
    *,
    storage_path: str | Path | None = None,
    observed_at: str | None = None,
) -> dict[str, Any]:
    specs = normalize_backend_probe_specs(raw_specs)
    return {
        "schema": NODE_HEALTH_SCHEMA,
        "observed_at": str(observed_at or _iso(_utcnow())),
        "node": {
            "available": True,
            "state": "ready",
            "load": _load_snapshot(),
            "storage": _storage_snapshot(storage_path),
        },
        "backends": [_backend_health(spec) for spec in specs],
    }


def _number(
    value: Any,
    *,
    minimum: float = 0.0,
    maximum: float | None = None,
) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if number < minimum:
        return None
    if maximum is not None and number > maximum:
        return None
    return round(number, 6)


def _nonnegative_int(value: Any) -> int | None:
    number = _number(value)
    if number is None:
        return None
    try:
        integer = int(number)
    except (TypeError, ValueError, OverflowError):
        return None
    return integer if integer >= 0 else None


def normalize_health_observation(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or raw.get("schema") != NODE_HEALTH_SCHEMA:
        return None
    observed_at = str(raw.get("observed_at") or "").strip()
    if not observed_at or _parse_iso(observed_at) is None:
        return None

    raw_node = raw.get("node")
    if not isinstance(raw_node, dict):
        return None
    load = raw_node.get("load") if isinstance(raw_node.get("load"), dict) else {}
    storage = (
        raw_node.get("storage")
        if isinstance(raw_node.get("storage"), dict)
        else {}
    )

    node = {
        "available": bool(raw_node.get("available")),
        "state": str(raw_node.get("state") or "unknown").strip().lower()[:32],
        "load": {
            "logical_cpu_count": _nonnegative_int(load.get("logical_cpu_count")),
            "load_1m": _number(load.get("load_1m")),
            "load_5m": _number(load.get("load_5m")),
            "load_15m": _number(load.get("load_15m")),
            "load_ratio_1m": _number(load.get("load_ratio_1m")),
            "memory_total_bytes": _nonnegative_int(load.get("memory_total_bytes")),
            "memory_available_bytes": _nonnegative_int(load.get("memory_available_bytes")),
            "memory_available_ratio": _number(
                load.get("memory_available_ratio"),
                maximum=1.0,
            ),
        },
        "storage": {
            "total_bytes": _nonnegative_int(storage.get("total_bytes")),
            "free_bytes": _nonnegative_int(storage.get("free_bytes")),
            "free_ratio": _number(storage.get("free_ratio"), maximum=1.0),
        },
    }

    backends: list[dict[str, Any]] = []
    seen: set[str] = set()
    raw_backends = raw.get("backends")
    if isinstance(raw_backends, list):
        for item in raw_backends[:128]:
            if not isinstance(item, dict):
                continue
            backend_id = str(item.get("backend_id") or "").strip().lower()
            if not backend_id or backend_id in seen:
                continue
            backends.append(
                {
                    "backend_id": backend_id,
                    "available": bool(item.get("available")),
                    "state": str(item.get("state") or "unknown").strip().lower()[:32],
                    "latency_ms": _number(item.get("latency_ms")),
                }
            )
            seen.add(backend_id)

    return {
        "schema": NODE_HEALTH_SCHEMA,
        "observed_at": observed_at,
        "node": node,
        "backends": backends,
    }


def evaluate_health_availability(
    raw: Any,
    *,
    now: datetime | None = None,
    stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS,
) -> dict[str, Any]:
    normalized = normalize_health_observation(raw)
    if normalized is None:
        return {
            "fresh": False,
            "stale": True,
            "age_seconds": None,
            "available": False,
            "state": "unknown",
            "backends": [],
        }

    observed = _parse_iso(normalized["observed_at"])
    current = (now or _utcnow()).astimezone(timezone.utc)
    age_seconds = max(0.0, (current - observed).total_seconds()) if observed else None
    stale = (
        age_seconds is None
        or age_seconds > max(1.0, float(stale_after_seconds))
    )
    node = normalized["node"]
    return {
        "fresh": not stale,
        "stale": stale,
        "age_seconds": round(age_seconds, 3) if age_seconds is not None else None,
        "available": bool(node["available"]) and not stale,
        "state": "stale" if stale else node["state"],
        "load": node["load"],
        "storage": node["storage"],
        "backends": [
            {
                **item,
                "available": bool(item["available"]) and not stale,
                "state": "stale" if stale else item["state"],
            }
            for item in normalized["backends"]
        ],
    }


def refresh_local_node_health(
    registry: Any,
    raw_specs: Any,
    *,
    storage_path: str | Path | None = None,
) -> dict[str, Any] | None:
    node_id = str(getattr(registry, "local_machine_id", "") or "").strip()
    enrich = getattr(registry, "enrich_node", None)
    if not node_id or not callable(enrich):
        return None
    observation = probe_node_health(raw_specs, storage_path=storage_path)
    observation["node_id"] = node_id
    enrich(
        node_id,
        facet="health.availability",
        data=observation,
        source="local_probe",
        observed_at=observation["observed_at"],
    )
    return observation


def mark_node_unavailable(
    registry: Any,
    node_id: str,
    *,
    source: str,
    state: str = "offline",
) -> dict[str, Any] | None:
    lifecycle = getattr(registry, "lifecycle", None)
    enrich = getattr(registry, "enrich_node", None)
    if not callable(lifecycle) or not callable(enrich):
        return None

    try:
        facets = ((lifecycle(node_id).get("enrichment") or {}).get("facets") or {})
    except Exception:
        return None
    previous = ((facets.get("health.availability") or {}).get("data") or {})
    normalized = normalize_health_observation(previous)
    load = (
        dict(normalized["node"]["load"])
        if isinstance(normalized, dict)
        else {}
    )
    storage = (
        dict(normalized["node"]["storage"])
        if isinstance(normalized, dict)
        else {}
    )
    backends = []
    if isinstance(normalized, dict):
        for item in normalized.get("backends", []):
            if not isinstance(item, dict):
                continue
            backends.append(
                {
                    "backend_id": item["backend_id"],
                    "available": False,
                    "state": state,
                    "latency_ms": item.get("latency_ms"),
                }
            )

    observation = {
        "schema": NODE_HEALTH_SCHEMA,
        "observed_at": _iso(_utcnow()),
        "node_id": str(node_id),
        "node": {
            "available": False,
            "state": str(state or "offline").strip().lower()[:32],
            "load": load,
            "storage": storage,
        },
        "backends": backends,
    }
    enrich(
        node_id,
        facet="health.availability",
        data=observation,
        source=source,
        observed_at=observation["observed_at"],
    )
    return observation


class NodeHealthMonitor:
    def __init__(
        self,
        registry: Any,
        raw_specs: Any,
        *,
        storage_path: str | Path | None = None,
        interval_seconds: float = 15.0,
    ) -> None:
        self.registry = registry
        self.raw_specs = raw_specs
        self.storage_path = storage_path
        self.interval_seconds = max(5.0, float(interval_seconds))
        self._task: asyncio.Task[None] | None = None
        self.last_error: str | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        refresh_local_node_health(
            self.registry,
            self.raw_specs,
            storage_path=self.storage_path,
        )
        self._task = asyncio.create_task(
            self._loop(),
            name="hirda-node-health",
        )

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.interval_seconds)
                refresh_local_node_health(
                    self.registry,
                    self.raw_specs,
                    storage_path=self.storage_path,
                )
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
