from __future__ import annotations

from typing import Any, Callable

from .node_backends import normalize_backend_observation
from .node_hardware import normalize_hardware_resources


ObservationNormalizer = Callable[[Any], dict[str, Any] | None]

_OBSERVATION_NORMALIZERS: dict[str, ObservationNormalizer] = {
    "hardware.resources": normalize_hardware_resources,
    "runtime.backends": normalize_backend_observation,
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
    for facet, normalized in accepted.items():
        enrich(
            node_id,
            facet=facet,
            data=normalized,
            source=source,
            observed_at=str(normalized["observed_at"]),
        )
    return accepted
