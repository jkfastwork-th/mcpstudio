from __future__ import annotations

from typing import Any, Callable

from .node_backends import normalize_backend_observation
from .node_hardware import normalize_hardware_resources
from .node_models import normalize_model_observation


ObservationNormalizer = Callable[[Any], dict[str, Any] | None]

_OBSERVATION_NORMALIZERS: dict[str, ObservationNormalizer] = {
    "hardware.resources": normalize_hardware_resources,
    "runtime.backends": normalize_backend_observation,
    "runtime.models": normalize_model_observation,
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


def _known_backend_ids(
    registry: Any,
    node_id: str,
    accepted: dict[str, dict[str, Any]],
) -> set[str]:
    current = accepted.get("runtime.backends")
    if current is None:
        lifecycle = getattr(registry, "lifecycle", None)
        if callable(lifecycle):
            try:
                facets = ((lifecycle(node_id).get("enrichment") or {}).get("facets") or {})
                current = ((facets.get("runtime.backends") or {}).get("data") or {})
            except Exception:
                current = None
    if not isinstance(current, dict):
        return set()
    return {
        str(item.get("id") or "").strip().lower()
        for item in current.get("backends", [])
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    }


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
    models = accepted.get("runtime.models")
    if isinstance(models, dict):
        known_backend_ids = _known_backend_ids(registry, node_id, accepted)
        models = dict(models)
        models["node_id"] = str(node_id)
        models["inventories"] = [
            item
            for item in models.get("inventories", [])
            if item.get("backend_id") in known_backend_ids
        ]
        accepted["runtime.models"] = models

    for facet, normalized in accepted.items():
        enrich(
            node_id,
            facet=facet,
            data=normalized,
            source=source,
            observed_at=str(normalized["observed_at"]),
        )
    return accepted
