from __future__ import annotations

import copy
import re
from datetime import datetime, timezone
from typing import Any

from .machine_registry import MachineDescriptor, MachineRegistry, MachineRegistryError


NODE_REGISTRY_SCHEMA = "hirda-node-registry-v1"
_NODE_METADATA_KEY = "node_registry"
_NODE_FACET_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_ENROLLED_SOURCES = frozenset({"enrollment", "device_pairing"})


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class NodeRegistry(MachineRegistry):
    """Canonical internal registry for HIRDA compute/capability nodes.

    The inherited MachineRegistry surface is intentionally preserved so current
    machine APIs, policy checks, bindings, routers, and UI contracts keep
    working while the internal model evolves toward generic nodes.
    """

    schema = NODE_REGISTRY_SCHEMA

    def __init__(self, studio: Any, **kwargs: Any) -> None:
        self.health_stale_after_seconds = max(
            1.0,
            float(getattr(studio, "node_health_stale_after_seconds", 45.0)),
        )
        super().__init__(studio, **kwargs)
        for node in self.list():
            source = str(node.metadata.get("registry_source") or "").strip()
            if not source:
                source = "local" if node.local else "configured"
            self._stamp_node(node, source=source, increment=False)

    @staticmethod
    def _lifecycle_state_for_source(source: str) -> str:
        return "enrolled" if str(source) in _ENROLLED_SOURCES else "registered"

    @staticmethod
    def _source_timestamp(node: MachineDescriptor, source: str) -> str | None:
        metadata = node.metadata if isinstance(node.metadata, dict) else {}
        if source == "enrollment":
            enrollment = metadata.get("enrollment")
            if isinstance(enrollment, dict):
                value = str(enrollment.get("approved_at") or "").strip()
                if value:
                    return value
        if source == "device_pairing":
            value = str(metadata.get("paired_at") or "").strip()
            if value:
                return value
        return None

    def _stamp_node(
        self,
        node: MachineDescriptor,
        *,
        source: str,
        increment: bool,
        state: str | None = None,
    ) -> MachineDescriptor:
        now = _utcnow()
        metadata = dict(node.metadata)
        existing = metadata.get(_NODE_METADATA_KEY)
        lifecycle = dict(existing) if isinstance(existing, dict) else {}

        desired_state = str(state or self._lifecycle_state_for_source(source))
        if lifecycle.get("state") == "enrolled" and desired_state == "registered":
            desired_state = "enrolled"

        stable_start = self._source_timestamp(node, source) or now
        lifecycle.setdefault("schema", NODE_REGISTRY_SCHEMA)
        lifecycle["node_id"] = node.machine_id
        lifecycle["state"] = desired_state
        lifecycle["source"] = str(source or "runtime")
        lifecycle.setdefault("registered_at", stable_start)
        if desired_state == "enrolled":
            lifecycle.setdefault("enrolled_at", stable_start)
        lifecycle["updated_at"] = now

        try:
            revision = int(lifecycle.get("revision") or 0)
        except (TypeError, ValueError):
            revision = 0
        if increment or revision < 1:
            revision += 1
        lifecycle["revision"] = max(1, revision)

        raw_enrichment = lifecycle.get("enrichment")
        enrichment = dict(raw_enrichment) if isinstance(raw_enrichment, dict) else {}
        enrichment.setdefault("state", "pending")
        raw_facets = enrichment.get("facets")
        enrichment["facets"] = dict(raw_facets) if isinstance(raw_facets, dict) else {}
        lifecycle["enrichment"] = enrichment

        metadata[_NODE_METADATA_KEY] = lifecycle
        node.metadata = metadata
        return node

    def register_node(
        self,
        raw: dict[str, Any],
        *,
        source: str = "runtime",
        replace: bool = False,
        lifecycle_state: str | None = None,
    ) -> MachineDescriptor:
        payload = dict(raw)
        machine_id = str(payload.get("id") or "").strip()
        existing = self._machines.get(machine_id)
        if replace and existing is not None:
            previous_lifecycle = existing.metadata.get(_NODE_METADATA_KEY)
            if isinstance(previous_lifecycle, dict):
                metadata = (
                    dict(payload.get("metadata") or {})
                    if isinstance(payload.get("metadata"), dict)
                    else {}
                )
                metadata.setdefault(
                    _NODE_METADATA_KEY,
                    copy.deepcopy(previous_lifecycle),
                )
                payload["metadata"] = metadata

        node = super().register_machine(
            payload,
            source=source,
            replace=replace,
        )
        return self._stamp_node(
            node,
            source=source,
            state=lifecycle_state,
            increment=True,
        )

    def register_machine(
        self,
        raw: dict[str, Any],
        *,
        source: str = "runtime",
        replace: bool = False,
    ) -> MachineDescriptor:
        """Backward-compatible machine registration facade."""
        return self.register_node(raw, source=source, replace=replace)

    def get_node(self, node_id: str) -> MachineDescriptor:
        return self.get(node_id)

    def contains_node(self, node_id: str) -> bool:
        return self.contains(node_id)

    def list_nodes(self) -> list[MachineDescriptor]:
        return self.list()

    def lifecycle(self, node_id: str) -> dict[str, Any]:
        node = self.get_node(node_id)
        raw = node.metadata.get(_NODE_METADATA_KEY)
        if not isinstance(raw, dict):
            source = str(node.metadata.get("registry_source") or "runtime")
            self._stamp_node(node, source=source, increment=False)
            raw = node.metadata.get(_NODE_METADATA_KEY)
        return copy.deepcopy(dict(raw or {}))

    def enrich_node(
        self,
        node_id: str,
        *,
        facet: str,
        data: Any,
        source: str = "discovery",
        observed_at: str | None = None,
    ) -> MachineDescriptor:
        """Enrich an existing node without replacing identity or policy."""
        key = str(facet or "").strip().lower()
        if not _NODE_FACET_RE.fullmatch(key):
            raise MachineRegistryError("invalid node enrichment facet")

        node = self.get_node(node_id)
        registry_source = str(node.metadata.get("registry_source") or "runtime")
        self._stamp_node(node, source=registry_source, increment=False)

        metadata = dict(node.metadata)
        lifecycle = dict(metadata[_NODE_METADATA_KEY])
        enrichment = dict(lifecycle.get("enrichment") or {})
        facets = dict(enrichment.get("facets") or {})
        now = str(observed_at or _utcnow())

        facets[key] = {
            "source": str(source or "discovery"),
            "observed_at": now,
            "data": copy.deepcopy(data),
        }
        enrichment["facets"] = facets
        enrichment["state"] = "partial"
        enrichment["updated_at"] = now
        lifecycle["enrichment"] = enrichment
        lifecycle["updated_at"] = now

        try:
            revision = int(lifecycle.get("revision") or 0)
        except (TypeError, ValueError):
            revision = 0
        lifecycle["revision"] = max(1, revision + 1)

        metadata[_NODE_METADATA_KEY] = lifecycle
        node.metadata = metadata
        return node


    def availability(
        self,
        node_id: str,
        *,
        stale_after_seconds: float | None = None,
    ) -> dict[str, Any]:
        from .node_health import evaluate_health_availability

        lifecycle = self.lifecycle(node_id)
        facets = ((lifecycle.get("enrichment") or {}).get("facets") or {})
        raw = ((facets.get("health.availability") or {}).get("data") or {})
        result = evaluate_health_availability(
            raw,
            stale_after_seconds=(
                self.health_stale_after_seconds
                if stale_after_seconds is None
                else stale_after_seconds
            ),
        )
        result["node_id"] = str(node_id)
        return result
