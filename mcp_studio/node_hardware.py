from __future__ import annotations

import ctypes
import os
import platform
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


NODE_HARDWARE_SCHEMA = "hirda-node-hardware-v1"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _nonnegative_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if result >= 0 else None


def _physical_memory_bytes() -> int | None:
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
                return int(status.ullTotalPhys)
        except (AttributeError, OSError, ValueError):
            return None
        return None

    if hasattr(os, "sysconf"):
        try:
            page_size = int(os.sysconf("SC_PAGE_SIZE"))
            page_count = int(os.sysconf("SC_PHYS_PAGES"))
            total = page_size * page_count
            return total if total >= 0 else None
        except (OSError, ValueError, TypeError):
            return None
    return None


def _storage_usage(storage_path: str | Path | None) -> dict[str, int | None]:
    target = Path(storage_path or Path.cwd()).expanduser().resolve(strict=False)
    try:
        usage = shutil.disk_usage(target)
    except (FileNotFoundError, OSError):
        anchor = Path(target.anchor) if target.anchor else Path.cwd().anchor
        try:
            usage = shutil.disk_usage(anchor or Path.cwd())
        except (FileNotFoundError, OSError):
            return {
                "total_bytes": None,
                "used_bytes": None,
                "free_bytes": None,
            }
    return {
        "total_bytes": _nonnegative_int(usage.total),
        "used_bytes": _nonnegative_int(usage.used),
        "free_bytes": _nonnegative_int(usage.free),
    }


def probe_hardware_resources(
    storage_path: str | Path | None = None,
    *,
    observed_at: str | None = None,
) -> dict[str, Any]:
    logical_count = _nonnegative_int(os.cpu_count())
    return {
        "schema": NODE_HARDWARE_SCHEMA,
        "observed_at": str(observed_at or _utcnow()),
        "architecture": platform.machine() or "unknown",
        "processor": platform.processor() or None,
        "cpu": {
            "logical_count": logical_count,
        },
        "memory": {
            "total_bytes": _physical_memory_bytes(),
        },
        "storage": _storage_usage(storage_path),
    }


def normalize_hardware_resources(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or raw.get("schema") != NODE_HARDWARE_SCHEMA:
        return None

    cpu = raw.get("cpu") if isinstance(raw.get("cpu"), dict) else {}
    memory = raw.get("memory") if isinstance(raw.get("memory"), dict) else {}
    storage = raw.get("storage") if isinstance(raw.get("storage"), dict) else {}

    observed_at = str(raw.get("observed_at") or "").strip()
    if not observed_at:
        return None

    architecture = str(raw.get("architecture") or "unknown").strip() or "unknown"
    processor_value = raw.get("processor")
    processor = str(processor_value).strip() if processor_value is not None else None
    if processor == "":
        processor = None

    return {
        "schema": NODE_HARDWARE_SCHEMA,
        "observed_at": observed_at,
        "architecture": architecture,
        "processor": processor,
        "cpu": {
            "logical_count": _nonnegative_int(cpu.get("logical_count")),
        },
        "memory": {
            "total_bytes": _nonnegative_int(memory.get("total_bytes")),
        },
        "storage": {
            "total_bytes": _nonnegative_int(storage.get("total_bytes")),
            "used_bytes": _nonnegative_int(storage.get("used_bytes")),
            "free_bytes": _nonnegative_int(storage.get("free_bytes")),
        },
    }


def refresh_local_node_hardware(
    registry: Any,
    *,
    storage_path: str | Path | None = None,
) -> dict[str, Any] | None:
    node_id = str(getattr(registry, "local_machine_id", "") or "").strip()
    if not node_id:
        return None

    getter = getattr(registry, "get_node", None)
    if not callable(getter):
        getter = getattr(registry, "get", None)
    enrich = getattr(registry, "enrich_node", None)
    if not callable(getter) or not callable(enrich):
        return None

    node = getter(node_id)
    if not bool(getattr(node, "local", False)):
        return None

    observation = probe_hardware_resources(storage_path)
    enrich(
        node_id,
        facet="hardware.resources",
        data=observation,
        source="local_probe",
        observed_at=observation["observed_at"],
    )
    return observation
