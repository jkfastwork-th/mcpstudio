from __future__ import annotations

from typing import Any, Callable

from .node_backends import normalize_backend_observation
from .node_capabilities import normalize_capability_advertisement
from .node_hardware import normalize_hardware_resources
from .node_models import normalize_model_observation


ObservationNormalizer = Callable[[Any], dict[str, Any] | None]

_OBSERVATION_NORMALIZERS: dict[str, ObservationNormalizer] = {
    "hardware.resources": normalize_hardware_resources,
    "runtime.backends": normalize_backend_observation,
    "runtime.models": normalize_model_observation,
    "capabilities.advertised": normalize_capability_advertisement,
}


def normalize_node_observations(
    raw_observations: Any,
) -> dict[str, dict[str, Any]]:
    if not isinstance(raw_observations, dict):
        return {}
    accepted: dict[str, dict[str, Any]] = {}
    for facet, normalizer in _OBSERVATION_NORMALIZERS.items():
        normalized = normalizer(raw_observations.get(facet))
        if normalized is not None:
            accepted[facet] = normalized
    return accepted


def _existing_facet_data(
    registry: Any,
    node_id: str,
    facet: str,
) -> dict[str, Any]:
    lifecycle = getattr(registry, "lifecycle", None)
    if not callable(lifecycle):
        return {}
    try:
        facets = ((lifecycle(node_id).get("enrichment") or {}).get("facets") or {})
    except Exception:
        return {}
    data = ((facets.get(facet) or {}).get("data") or {})
    return data if isinstance(data, dict) else {}


def _known_backend_ids(
    registry: Any,
    node_id: str,
    accepted: dict[str, dict[str, Any]],
) -> set[str]:
    current = accepted.get("runtime.backends")
    if current is None:
        current = _existing_facet_data(
            registry,
            node_id,
            "runtime.backends",
        )
    if not isinstance(current, dict):
        return set()
    return {
        str(item.get("id") or "").strip().lower()
        for item in current.get("backends", [])
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    }


def _known_model_refs(
    registry: Any,
    node_id: str,
    accepted: dict[str, dict[str, Any]],
) -> set[tuple[str, str]]:
    current = accepted.get("runtime.models")
    if current is None:
        current = _existing_facet_data(
            registry,
            node_id,
            "runtime.models",
        )
    if not isinstance(current, dict):
        return set()

    refs: set[tuple[str, str]] = set()
    for inventory in current.get("inventories", []):
        if not isinstance(inventory, dict):
            continue
        backend_id = str(inventory.get("backend_id") or "").strip().lower()
        if not backend_id:
            continue
        for model in inventory.get("models", []):
            if not isinstance(model, dict):
                continue
            model_id = str(model.get("id") or "").strip()
            if model_id:
                refs.add((backend_id, model_id))
    return refs


def ingest_node_observations(
    registry: Any,
    node_id: str,
    raw_observations: Any,
    *,
    source: str,
) -> dict[str, dict[str, Any]]:
    enrich = getattr(registry, "enrich_node", None)
    if not callable(enrich):
        return {}

    accepted = normalize_node_observations(raw_observations)
    known_backend_ids = _known_backend_ids(registry, node_id, accepted)

    models = accepted.get("runtime.models")
    if isinstance(models, dict):
        models = dict(models)
        models["node_id"] = str(node_id)
        models["inventories"] = [
            item
            for item in models.get("inventories", [])
            if item.get("backend_id") in known_backend_ids
        ]
        accepted["runtime.models"] = models

    capabilities = accepted.get("capabilities.advertised")
    if isinstance(capabilities, dict):
        known_model_refs = _known_model_refs(registry, node_id, accepted)
        capabilities = dict(capabilities)
        capabilities["node_id"] = str(node_id)
        capabilities["backends"] = [
            item
            for item in capabilities.get("backends", [])
            if item.get("backend_id") in known_backend_ids
        ]
        capabilities["models"] = [
            item
            for item in capabilities.get("models", [])
            if (
                item.get("backend_id"),
                item.get("model_id"),
            ) in known_model_refs
        ]
        accepted["capabilities.advertised"] = capabilities

    for facet, normalized in accepted.items():
        enrich(
            node_id,
            facet=facet,
            data=normalized,
            source=source,
            observed_at=str(normalized["observed_at"]),
        )
    return accepted
