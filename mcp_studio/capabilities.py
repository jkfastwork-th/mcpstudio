from __future__ import annotations

from typing import Any


CAPABILITY_REGISTRY_SCHEMA = "hirda-capability-registry-v1"


def build_capability_registry(
    integration_manager: Any,
    skill_runtime: Any,
) -> dict[str, Any]:
    """Project MCP tools and declarative skills into one provider-neutral registry."""
    capabilities: list[dict[str, Any]] = []

    for descriptor in integration_manager.backend_tools():
        name = str(descriptor.get("name") or "").strip()
        if not name:
            continue
        route = integration_manager.backend_route(name)
        integration_id, raw_name = route if route else ("unknown", name)
        capabilities.append(
            {
                "id": f"mcp:{integration_id}:{raw_name}",
                "kind": "tool",
                "source": "mcp",
                "provider": integration_id,
                "name": name,
                "raw_name": raw_name,
                "description": str(descriptor.get("description") or ""),
                "input_schema": dict(descriptor.get("inputSchema") or {}),
            }
        )

    state = skill_runtime._snapshot()
    for skill in state.registry.snapshot():
        capabilities.append(
            {
                "id": f"skill:{skill.id}",
                "kind": "skill",
                "source": "skill",
                "provider": "hirda",
                "name": skill.name,
                "version": skill.version,
                "description": skill.description,
                "required_capabilities": list(skill.required_capabilities),
                "required_tools": list(skill.required_tools),
                "scope": {
                    "kind": skill.scope.kind,
                    "value": skill.scope.value,
                },
            }
        )

    capabilities.sort(
        key=lambda item: (
            str(item.get("source") or ""),
            str(item.get("provider") or ""),
            str(item.get("name") or ""),
        )
    )
    source_counts: dict[str, int] = {}
    for item in capabilities:
        source = str(item.get("source") or "unknown")
        source_counts[source] = source_counts.get(source, 0) + 1

    return {
        "schema": CAPABILITY_REGISTRY_SCHEMA,
        "provider_neutral": True,
        "sources": source_counts,
        "capability_count": len(capabilities),
        "capabilities": capabilities,
    }
