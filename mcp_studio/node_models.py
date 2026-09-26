from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .node_backends import normalize_backend_probe_specs


NODE_MODELS_SCHEMA = "hirda-node-models-v1"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def _extract_json_path(payload: Any, path: list[str]) -> Any:
    current = payload
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def _normalize_model_item(
    raw: Any,
    *,
    id_field: str,
    name_field: str,
) -> dict[str, Any] | None:
    if isinstance(raw, str):
        model_id = raw.strip()
        return {"id": model_id, "name": None} if model_id else None
    if not isinstance(raw, dict):
        return None
    model_id = str(raw.get(id_field) or "").strip()
    if not model_id:
        return None
    name_value = raw.get(name_field)
    name = str(name_value).strip() if name_value not in (None, "") else None
    return {"id": model_id[:512], "name": name[:512] if name else None}


def _parse_models(output: str, probe: dict[str, Any]) -> list[dict[str, Any]] | None:
    if probe["format"] == "lines":
        raw_items: Any = [line.strip() for line in output.splitlines() if line.strip()]
    else:
        try:
            payload = json.loads(output)
        except json.JSONDecodeError:
            return None
        raw_items = _extract_json_path(payload, probe["json_path"])
        if raw_items is None and not probe["json_path"]:
            raw_items = payload
    if not isinstance(raw_items, list):
        return None

    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_items[:4096]:
        item = _normalize_model_item(
            raw,
            id_field=probe["id_field"],
            name_field=probe["name_field"],
        )
        if item is None or item["id"] in seen:
            continue
        result.append(item)
        seen.add(item["id"])
    return result


def probe_configured_models(
    raw_specs: Any,
    *,
    observed_at: str | None = None,
) -> dict[str, Any]:
    specs = normalize_backend_probe_specs(raw_specs)
    inventories: list[dict[str, Any]] = []

    for spec in specs:
        model_probe = spec.get("model_probe")
        if not isinstance(model_probe, dict):
            continue

        backend_binary = _resolve_binary(spec["command"])
        if not backend_binary:
            inventories.append(
                {
                    "backend_id": spec["id"],
                    "status": "backend_unavailable",
                    "models": [],
                }
            )
            continue

        model_binary = _resolve_binary(model_probe["command"])
        if not model_binary:
            inventories.append(
                {
                    "backend_id": spec["id"],
                    "status": "probe_unavailable",
                    "models": [],
                }
            )
            continue

        argv = [model_binary, *model_probe["command"][1:]]
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=model_probe["timeout_seconds"],
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            inventories.append(
                {
                    "backend_id": spec["id"],
                    "status": "probe_failed",
                    "models": [],
                }
            )
            continue

        if proc.returncode != 0:
            inventories.append(
                {
                    "backend_id": spec["id"],
                    "status": "probe_failed",
                    "models": [],
                }
            )
            continue

        models = _parse_models(proc.stdout or "", model_probe)
        if models is None:
            inventories.append(
                {
                    "backend_id": spec["id"],
                    "status": "invalid_output",
                    "models": [],
                }
            )
            continue

        inventories.append(
            {
                "backend_id": spec["id"],
                "status": "ready",
                "models": models,
            }
        )

    return {
        "schema": NODE_MODELS_SCHEMA,
        "observed_at": str(observed_at or _utcnow()),
        "inventories": inventories,
    }


def normalize_model_observation(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or raw.get("schema") != NODE_MODELS_SCHEMA:
        return None
    observed_at = str(raw.get("observed_at") or "").strip()
    inventories_raw = raw.get("inventories")
    if not observed_at or not isinstance(inventories_raw, list):
        return None

    inventories: list[dict[str, Any]] = []
    seen_backends: set[str] = set()
    for item in inventories_raw[:128]:
        if not isinstance(item, dict):
            continue
        backend_id = str(item.get("backend_id") or "").strip().lower()
        if not backend_id or backend_id in seen_backends:
            continue
        status = str(item.get("status") or "").strip().lower()
        if status not in {
            "ready",
            "backend_unavailable",
            "probe_unavailable",
            "probe_failed",
            "invalid_output",
        }:
            continue
        models_raw = item.get("models")
        if not isinstance(models_raw, list):
            continue
        models: list[dict[str, Any]] = []
        seen_models: set[str] = set()
        for raw_model in models_raw[:4096]:
            model = _normalize_model_item(
                raw_model,
                id_field="id",
                name_field="name",
            )
            if model is None or model["id"] in seen_models:
                continue
            models.append(model)
            seen_models.add(model["id"])
        inventories.append(
            {
                "backend_id": backend_id,
                "status": status,
                "models": models,
            }
        )
        seen_backends.add(backend_id)

    return {
        "schema": NODE_MODELS_SCHEMA,
        "observed_at": observed_at,
        "inventories": inventories,
    }


def refresh_local_node_models(
    registry: Any,
    raw_specs: Any,
) -> dict[str, Any] | None:
    node_id = str(getattr(registry, "local_machine_id", "") or "").strip()
    enrich = getattr(registry, "enrich_node", None)
    lifecycle = getattr(registry, "lifecycle", None)
    if not node_id or not callable(enrich) or not callable(lifecycle):
        return None

    known = lifecycle(node_id)
    facets = ((known.get("enrichment") or {}).get("facets") or {})
    backend_data = ((facets.get("runtime.backends") or {}).get("data") or {})
    backend_ids = {
        str(item.get("id") or "").strip().lower()
        for item in backend_data.get("backends", [])
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    }

    observation = probe_configured_models(raw_specs)
    observation["node_id"] = node_id
    observation["inventories"] = [
        item
        for item in observation["inventories"]
        if item["backend_id"] in backend_ids
    ]
    enrich(
        node_id,
        facet="runtime.models",
        data=observation,
        source="local_probe",
        observed_at=observation["observed_at"],
    )
    return observation
