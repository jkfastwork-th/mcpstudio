from __future__ import annotations

from typing import Any, Iterable


CAPABILITY_REGISTRY_SCHEMA = "hirda-capability-registry-v2"
CAPABILITY_RESOLUTION_SCHEMA = "hirda-capability-resolution-v1"


def _unique(values: Iterable[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            output.append(text)
    return output


def _provider_summary(provider: dict[str, Any]) -> dict[str, Any]:
    tools = [dict(item) for item in provider.get("tools") or [] if isinstance(item, dict)]
    permissions = _unique(str(item.get("permission") or "") for item in tools)
    return {
        "id": provider.get("id"),
        "name": provider.get("name"),
        "source": provider.get("source"),
        "stage": provider.get("stage"),
        "blocked": bool(provider.get("blocked")),
        "available": bool(provider.get("available")),
        "permissions": permissions,
        "tools": tools,
        "routing": dict(provider.get("routing") or {}),
    }


class CapabilityResolver:
    """Resolve Skill requirements to HIRDA providers without granting authority.

    The resolver is descriptive. It does not invoke tools or bypass machine/tool
    permissions; dispatch still goes through the existing HIRDA gates.
    """

    def __init__(self, integration_manager: Any) -> None:
        self.integrations = integration_manager

    def resolve(
        self,
        *,
        required_capabilities: Iterable[str] = (),
        required_tools: Iterable[str] = (),
    ) -> dict[str, Any]:
        capability_requirements = _unique(required_capabilities)
        tool_requirements = _unique(required_tools)
        capability_results: list[dict[str, Any]] = []
        tool_results: list[dict[str, Any]] = []
        unresolved_capabilities: list[str] = []
        unresolved_tools: list[str] = []
        selected_provider_ids: list[str] = []

        for requirement in capability_requirements:
            providers = [
                _provider_summary(item)
                for item in self.integrations.capability_providers(requirement)
            ]
            available = [item for item in providers if item["available"]]
            selected = dict(available[0]) if available else None
            fallbacks = [dict(item) for item in available[1:]]
            if selected is None:
                unresolved_capabilities.append(requirement)
            else:
                selected_provider_ids.append(str(selected.get("id") or ""))
            capability_results.append(
                {
                    "requirement": requirement,
                    "selected": selected,
                    "fallbacks": fallbacks,
                    "candidates": providers,
                    "status": "resolved" if selected is not None else "unresolved",
                }
            )

        for requirement in tool_requirements:
            providers = [
                dict(item)
                for item in self.integrations.tool_providers(requirement)
            ]
            available = [item for item in providers if bool(item.get("available"))]
            selected = dict(available[0]) if available else None
            fallbacks = [dict(item) for item in available[1:]]
            if selected is None:
                unresolved_tools.append(requirement)
            else:
                selected_provider_ids.append(str(selected.get("id") or ""))
            tool_results.append(
                {
                    "requirement": requirement,
                    "selected": selected,
                    "fallbacks": fallbacks,
                    "candidates": providers,
                    "status": "resolved" if selected is not None else "unresolved",
                }
            )

        unresolved_count = len(unresolved_capabilities) + len(unresolved_tools)
        total_count = len(capability_results) + len(tool_results)
        status = (
            "ready"
            if unresolved_count == 0
            else "unresolved"
            if total_count and unresolved_count == total_count
            else "partial"
        )
        return {
            "schema": CAPABILITY_RESOLUTION_SCHEMA,
            "authority": "descriptive_only",
            "dispatch_authority": "hirda",
            "status": status,
            "resolved": unresolved_count == 0,
            "required_capabilities": capability_requirements,
            "required_tools": tool_requirements,
            "capabilities": capability_results,
            "tools": tool_results,
            "selected_provider_ids": _unique(selected_provider_ids),
            "unresolved_capabilities": unresolved_capabilities,
            "unresolved_tools": unresolved_tools,
        }

    def resolve_skill_metadata(self, metadata: dict[str, Any]) -> dict[str, Any]:
        resolution = metadata.get("skill_resolution")
        resolution = resolution if isinstance(resolution, dict) else {}
        return self.resolve(
            required_capabilities=resolution.get("required_capabilities") or (),
            required_tools=resolution.get("required_tools") or (),
        )


def build_capability_registry(
    integration_manager: Any,
    skill_runtime: Any,
) -> dict[str, Any]:
    """Project tools, integration capabilities, and Skills into one registry."""
    capabilities: list[dict[str, Any]] = []
    provider_ids: set[str] = set()
    provider_entries: dict[str, dict[str, Any]] = {}

    for descriptor in integration_manager.backend_tools():
        name = str(descriptor.get("name") or "").strip()
        if not name:
            continue
        route = integration_manager.backend_route(name)
        integration_id, raw_name = route if route else ("unknown", name)
        provider_ids.add(integration_id)
        capabilities.append(
            {
                "id": f"mcp:{integration_id}:{raw_name}",
                "kind": "tool",
                "source": "mcp",
                "provider": integration_id,
                "name": name,
                "raw_name": raw_name,
                "permission": integration_manager.backend_permission(name),
                "description": str(descriptor.get("description") or ""),
                "input_schema": dict(descriptor.get("inputSchema") or {}),
            }
        )

    # Include provider-level semantic capabilities declared by integration
    # manifests. The manager owns lifecycle/availability; this registry only
    # projects that state.
    for integration_id in sorted(provider_ids):
        try:
            registration = integration_manager.get(integration_id)
        except KeyError:
            continue
        manifest = registration.adapter.manifest
        provider_entries[integration_id] = {
            "id": integration_id,
            "name": manifest.name,
            "source": registration.source,
            "stage": registration.stage,
            "blocked": bool(registration.blocked),
            "available": bool(registration.stage == "ready" and not registration.blocked),
            "capabilities": list(manifest.capabilities),
            "routing": dict(manifest.routing),
            "metadata": dict(manifest.metadata),
        }
        for semantic_capability in manifest.capabilities:
            capabilities.append(
                {
                    "id": f"provider:{integration_id}:{semantic_capability}",
                    "kind": "provider_capability",
                    "source": "integration",
                    "provider": integration_id,
                    "name": semantic_capability,
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
                "runtime_constraints": list(skill.runtime_constraints),
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
        "provider_count": len(provider_entries),
        "providers": [provider_entries[key] for key in sorted(provider_entries)],
        "capability_count": len(capabilities),
        "capabilities": capabilities,
    }
