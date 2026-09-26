from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


NODE_CAPABILITIES_SCHEMA = "hirda-node-capabilities-v1"
_CAPABILITY_LIMIT = 128


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _capability_ids(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    result: list[str] = []
    for item in raw:
        value = str(item or "").strip().lower()
        if (
            value
            and len(value) <= 64
            and value[0].isalpha()
            and all(ch.isalnum() or ch in "_.-" for ch in value)
            and value not in result
        ):
            result.append(value)
        if len(result) >= _CAPABILITY_LIMIT:
            break
    return result


def build_capability_advertisement(
    node_capabilities: Any,
    backend_observation: Any,
    model_observation: Any,
    *,
    backend_specs: Any = None,
    observed_at: str | None = None,
) -> dict[str, Any]:
    backend_capabilities: list[dict[str, Any]] = []
    if isinstance(backend_observation, dict):
        for item in backend_observation.get("backends", [])[:128]:
            if not isinstance(item, dict):
                continue
            backend_id = str(item.get("id") or "").strip().lower()
            if not backend_id:
                continue
            backend_capabilities.append(
                {
                    "backend_id": backend_id,
                    "capabilities": _capability_ids(item.get("capabilities")),
                }
            )

    model_defaults: dict[str, list[str]] = {}
    if isinstance(backend_specs, list):
        for spec in backend_specs:
            if not isinstance(spec, dict):
                continue
            backend_id = str(spec.get("id") or "").strip().lower()
            if backend_id:
                model_defaults[backend_id] = _capability_ids(
                    spec.get("model_capabilities")
                )

    model_capabilities: list[dict[str, Any]] = []
    if isinstance(model_observation, dict):
        for inventory in model_observation.get("inventories", [])[:128]:
            if not isinstance(inventory, dict):
                continue
            backend_id = str(inventory.get("backend_id") or "").strip().lower()
            if not backend_id:
                continue
            capabilities = model_defaults.get(backend_id, [])
            for model in inventory.get("models", [])[:4096]:
                if not isinstance(model, dict):
                    continue
                model_id = str(model.get("id") or "").strip()
                if not model_id:
                    continue
                model_capabilities.append(
                    {
                        "backend_id": backend_id,
                        "model_id": model_id[:512],
                        "capabilities": list(capabilities),
                    }
                )

    return {
        "schema": NODE_CAPABILITIES_SCHEMA,
        "observed_at": str(observed_at or _utcnow()),
        "node": {
            "capabilities": _capability_ids(node_capabilities),
        },
        "backends": backend_capabilities,
        "models": model_capabilities,
    }


def normalize_capability_advertisement(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or raw.get("schema") != NODE_CAPABILITIES_SCHEMA:
        return None
    observed_at = str(raw.get("observed_at") or "").strip()
    if not observed_at:
        return None

    node_raw = raw.get("node")
    node_capabilities = (
        _capability_ids(node_raw.get("capabilities"))
        if isinstance(node_raw, dict)
        else []
    )

    backends: list[dict[str, Any]] = []
    seen_backends: set[str] = set()
    raw_backends = raw.get("backends")
    if isinstance(raw_backends, list):
        for item in raw_backends[:128]:
            if not isinstance(item, dict):
                continue
            backend_id = str(item.get("backend_id") or "").strip().lower()
            if not backend_id or backend_id in seen_backends:
                continue
            backends.append(
                {
                    "backend_id": backend_id,
                    "capabilities": _capability_ids(item.get("capabilities")),
                }
            )
            seen_backends.add(backend_id)

    models: list[dict[str, Any]] = []
    seen_models: set[tuple[str, str]] = set()
    raw_models = raw.get("models")
    if isinstance(raw_models, list):
        for item in raw_models[:4096]:
            if not isinstance(item, dict):
                continue
            backend_id = str(item.get("backend_id") or "").strip().lower()
            model_id = str(item.get("model_id") or "").strip()
            key = (backend_id, model_id)
            if not backend_id or not model_id or key in seen_models:
                continue
            models.append(
                {
                    "backend_id": backend_id,
                    "model_id": model_id[:512],
                    "capabilities": _capability_ids(item.get("capabilities")),
                }
            )
            seen_models.add(key)

    return {
        "schema": NODE_CAPABILITIES_SCHEMA,
        "observed_at": observed_at,
        "node": {
            "capabilities": node_capabilities,
        },
        "backends": backends,
        "models": models,
    }


def refresh_local_node_capabilities(
    registry: Any,
    raw_specs: Any = None,
) -> dict[str, Any] | None:
    node_id = str(getattr(registry, "local_machine_id", "") or "").strip()
    lifecycle = getattr(registry, "lifecycle", None)
    enrich = getattr(registry, "enrich_node", None)
    getter = getattr(registry, "get_node", None)
    if not callable(getter):
        getter = getattr(registry, "get", None)
    if not node_id or not callable(lifecycle) or not callable(enrich) or not callable(getter):
        return None

    node = getter(node_id)
    facets = ((lifecycle(node_id).get("enrichment") or {}).get("facets") or {})
    backend_observation = ((facets.get("runtime.backends") or {}).get("data") or {})
    model_observation = ((facets.get("runtime.models") or {}).get("data") or {})

    advertisement = build_capability_advertisement(
        list(getattr(node, "capabilities", ()) or ()),
        backend_observation,
        model_observation,
        backend_specs=raw_specs,
    )
    advertisement["node_id"] = node_id
    enrich(
        node_id,
        facet="capabilities.advertised",
        data=advertisement,
        source="local_probe",
        observed_at=advertisement["observed_at"],
    )
    return advertisement
