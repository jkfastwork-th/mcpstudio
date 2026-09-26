from __future__ import annotations

import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


NODE_BACKENDS_SCHEMA = "hirda-node-backends-v1"
_BACKEND_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_BACKEND_KIND_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,31}$")
_CAPABILITY_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")


class NodeBackendDiscoveryError(ValueError):
    pass


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _string_list(value: Any, *, limit: int = 32) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        text = str(item or "").strip().lower()
        if text and _CAPABILITY_RE.fullmatch(text) and text not in result:
            result.append(text)
        if len(result) >= limit:
            break
    return result


def _normalize_model_probe(raw: Any, *, backend_id: str) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise NodeBackendDiscoveryError(
            f"node backend probe {backend_id} model_probe must be an object"
        )

    command_raw = raw.get("command")
    if isinstance(command_raw, str):
        command = [command_raw.strip()] if command_raw.strip() else []
    elif isinstance(command_raw, list):
        command = [
            str(part).strip()
            for part in command_raw
            if str(part).strip()
        ][:32]
    else:
        command = []
    if not command:
        raise NodeBackendDiscoveryError(
            f"node backend probe {backend_id} model_probe requires command"
        )

    output_format = str(raw.get("format") or "lines").strip().lower()
    if output_format not in {"lines", "json"}:
        raise NodeBackendDiscoveryError(
            f"node backend probe {backend_id} model_probe format must be lines or json"
        )

    json_path_raw = raw.get("json_path")
    if json_path_raw is None:
        json_path: list[str] = []
    elif isinstance(json_path_raw, list):
        json_path = [
            str(part).strip()
            for part in json_path_raw
            if str(part).strip()
        ][:8]
    else:
        raise NodeBackendDiscoveryError(
            f"node backend probe {backend_id} model_probe json_path must be a list"
        )

    id_field = str(raw.get("id_field") or "id").strip()
    name_field = str(raw.get("name_field") or "name").strip()
    if not id_field or len(id_field) > 64:
        raise NodeBackendDiscoveryError(
            f"node backend probe {backend_id} model_probe id_field is invalid"
        )
    if not name_field or len(name_field) > 64:
        raise NodeBackendDiscoveryError(
            f"node backend probe {backend_id} model_probe name_field is invalid"
        )

    timeout_seconds = float(raw.get("timeout_seconds") or 5.0)
    timeout_seconds = max(0.25, min(30.0, timeout_seconds))
    return {
        "command": command,
        "format": output_format,
        "json_path": json_path,
        "id_field": id_field,
        "name_field": name_field,
        "timeout_seconds": timeout_seconds,
    }


def _normalize_health_probe(raw: Any, *, backend_id: str) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise NodeBackendDiscoveryError(
            f"node backend probe {backend_id} health_probe must be an object"
        )

    command_raw = raw.get("command")
    if isinstance(command_raw, str):
        command = [command_raw.strip()] if command_raw.strip() else []
    elif isinstance(command_raw, list):
        command = [
            str(part).strip()
            for part in command_raw
            if str(part).strip()
        ][:32]
    else:
        command = []
    if not command:
        raise NodeBackendDiscoveryError(
            f"node backend probe {backend_id} health_probe requires command"
        )

    timeout_seconds = float(raw.get("timeout_seconds") or 2.0)
    timeout_seconds = max(0.25, min(10.0, timeout_seconds))
    return {
        "command": command,
        "timeout_seconds": timeout_seconds,
    }


def normalize_backend_probe_specs(raw: Any) -> list[dict[str, Any]]:
    if raw in (None, []):
        return []
    if not isinstance(raw, list):
        raise NodeBackendDiscoveryError("node backend probes must be a list")

    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw[:64]:
        if not isinstance(item, dict):
            raise NodeBackendDiscoveryError("node backend probe must be an object")
        backend_id = str(item.get("id") or "").strip().lower()
        kind = str(item.get("kind") or "execution").strip().lower()
        if not _BACKEND_ID_RE.fullmatch(backend_id):
            raise NodeBackendDiscoveryError("invalid node backend id")
        if backend_id in seen:
            raise NodeBackendDiscoveryError(f"duplicate node backend id: {backend_id}")
        if not _BACKEND_KIND_RE.fullmatch(kind):
            raise NodeBackendDiscoveryError(f"invalid node backend kind: {kind}")

        command_raw = item.get("command")
        if isinstance(command_raw, str):
            command = [command_raw.strip()] if command_raw.strip() else []
        elif isinstance(command_raw, list):
            command = [str(part).strip() for part in command_raw if str(part).strip()]
        else:
            command = []
        if not command:
            raise NodeBackendDiscoveryError(
                f"node backend probe {backend_id} requires command"
            )

        version_args_raw = item.get("version_args")
        if version_args_raw is None:
            version_args: list[str] = []
        elif isinstance(version_args_raw, list):
            version_args = [
                str(part).strip()
                for part in version_args_raw
                if str(part).strip()
            ][:16]
        else:
            raise NodeBackendDiscoveryError(
                f"node backend probe {backend_id} version_args must be a list"
            )

        timeout_seconds = float(item.get("timeout_seconds") or 2.0)
        timeout_seconds = max(0.25, min(10.0, timeout_seconds))
        model_probe = _normalize_model_probe(
            item.get("model_probe"),
            backend_id=backend_id,
        )
        health_probe = _normalize_health_probe(
            item.get("health_probe"),
            backend_id=backend_id,
        )
        result.append(
            {
                "id": backend_id,
                "kind": kind,
                "command": command,
                "version_args": version_args,
                "timeout_seconds": timeout_seconds,
                "capabilities": _string_list(item.get("capabilities")),
                "model_capabilities": _string_list(item.get("model_capabilities")),
                "model_probe": model_probe,
                "health_probe": health_probe,
            }
        )
        seen.add(backend_id)
    return result


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


def _probe_version(
    binary: str,
    command: list[str],
    version_args: list[str],
    *,
    timeout_seconds: float,
) -> str | None:
    if not version_args:
        return None
    argv = [binary, *command[1:], *version_args]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = (proc.stdout or proc.stderr or "").strip().splitlines()
    if not text:
        return None
    return text[0][:240]


def probe_configured_backends(
    raw_specs: Any,
    *,
    observed_at: str | None = None,
) -> dict[str, Any]:
    specs = normalize_backend_probe_specs(raw_specs)
    backends: list[dict[str, Any]] = []
    for spec in specs:
        binary = _resolve_binary(spec["command"])
        available = bool(binary)
        backends.append(
            {
                "id": spec["id"],
                "kind": spec["kind"],
                "available": available,
                "binary": binary,
                "version": (
                    _probe_version(
                        binary,
                        spec["command"],
                        spec["version_args"],
                        timeout_seconds=spec["timeout_seconds"],
                    )
                    if binary
                    else None
                ),
                "capabilities": list(spec["capabilities"]),
            }
        )
    return {
        "schema": NODE_BACKENDS_SCHEMA,
        "observed_at": str(observed_at or _utcnow()),
        "backends": backends,
    }


def normalize_backend_observation(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or raw.get("schema") != NODE_BACKENDS_SCHEMA:
        return None
    observed_at = str(raw.get("observed_at") or "").strip()
    if not observed_at:
        return None
    raw_backends = raw.get("backends")
    if not isinstance(raw_backends, list):
        return None

    backends: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw_backends[:64]:
        if not isinstance(item, dict):
            continue
        backend_id = str(item.get("id") or "").strip().lower()
        kind = str(item.get("kind") or "").strip().lower()
        if (
            not _BACKEND_ID_RE.fullmatch(backend_id)
            or backend_id in seen
            or not _BACKEND_KIND_RE.fullmatch(kind)
        ):
            continue
        binary_value = item.get("binary")
        version_value = item.get("version")
        backends.append(
            {
                "id": backend_id,
                "kind": kind,
                "available": bool(item.get("available")),
                "binary": (
                    str(binary_value).strip()[:1024]
                    if binary_value not in (None, "")
                    else None
                ),
                "version": (
                    str(version_value).strip()[:240]
                    if version_value not in (None, "")
                    else None
                ),
                "capabilities": _string_list(item.get("capabilities")),
            }
        )
        seen.add(backend_id)

    return {
        "schema": NODE_BACKENDS_SCHEMA,
        "observed_at": observed_at,
        "backends": backends,
    }


def refresh_local_node_backends(
    registry: Any,
    raw_specs: Any,
) -> dict[str, Any] | None:
    node_id = str(getattr(registry, "local_machine_id", "") or "").strip()
    enrich = getattr(registry, "enrich_node", None)
    if not node_id or not callable(enrich):
        return None

    getter = getattr(registry, "get_node", None)
    if not callable(getter):
        getter = getattr(registry, "get", None)
    if not callable(getter):
        return None
    node = getter(node_id)
    if not bool(getattr(node, "local", False)):
        return None

    observation = probe_configured_backends(raw_specs)
    enrich(
        node_id,
        facet="runtime.backends",
        data=observation,
        source="local_probe",
        observed_at=observation["observed_at"],
    )
    return observation
