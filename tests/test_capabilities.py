from types import SimpleNamespace

from mcp_studio.capabilities import (
    CAPABILITY_REGISTRY_SCHEMA,
    CapabilityResolver,
    build_capability_registry,
)


class _Integrations:
    def __init__(self):
        manifest = SimpleNamespace(
            name="PStack",
            capabilities=("coding", "engineering_workflow"),
            routing={"mode": "methodology-layer"},
            metadata={"backend_capability": True},
        )
        self.registration = SimpleNamespace(
            adapter=SimpleNamespace(manifest=manifest),
            source="folder:pstack",
            stage="ready",
            blocked=False,
        )

    def backend_tools(self):
        return [
            {
                "name": "hirda__pstack__route_task",
                "description": "Route a task",
                "inputSchema": {"type": "object"},
            }
        ]

    def backend_route(self, name):
        if name == "hirda__pstack__route_task":
            return ("pstack", "route_task")
        return None

    def backend_permission(self, name):
        return "read" if name == "hirda__pstack__route_task" else None

    def get(self, integration_id):
        if integration_id != "pstack":
            raise KeyError(integration_id)
        return self.registration

    def capability_providers(self, capability):
        if capability != "coding":
            return []
        return [
            {
                "id": "pstack",
                "name": "PStack",
                "source": "folder:pstack",
                "stage": "ready",
                "blocked": False,
                "available": True,
                "capabilities": ["coding", "engineering_workflow"],
                "tools": [
                    {
                        "name": "hirda__pstack__route_task",
                        "raw_name": "route_task",
                        "permission": "read",
                        "available": True,
                    }
                ],
                "routing": {"mode": "methodology-layer"},
                "metadata": {"backend_capability": True},
            }
        ]

    def tool_providers(self, tool_name):
        if tool_name not in {"route_task", "hirda__pstack__route_task"}:
            return []
        return [
            {
                "id": "pstack",
                "name": "PStack",
                "source": "folder:pstack",
                "stage": "ready",
                "blocked": False,
                "available": True,
                "tool": {
                    "name": "hirda__pstack__route_task",
                    "raw_name": "route_task",
                    "permission": "read",
                },
            }
        ]


class _Registry:
    def snapshot(self):
        return [
            SimpleNamespace(
                id="code-review",
                name="Code Review",
                version="1",
                description="Review code",
                required_capabilities=("coding",),
                required_tools=("hirda__pstack__route_task",),
                runtime_constraints=("computer.private",),
                scope=SimpleNamespace(kind="workspace", value="*"),
            )
        ]


class _Skills:
    def _snapshot(self):
        return SimpleNamespace(registry=_Registry())


def test_capability_registry_projects_mcp_and_skills_without_changing_routes():
    result = build_capability_registry(_Integrations(), _Skills())

    assert result["schema"] == CAPABILITY_REGISTRY_SCHEMA
    assert result["provider_neutral"] is True
    assert result["sources"]["mcp"] == 1
    assert result["sources"]["skill"] == 1
    assert result["sources"]["integration"] == 2
    assert result["provider_count"] == 1

    mcp = next(item for item in result["capabilities"] if item["source"] == "mcp")
    skill = next(item for item in result["capabilities"] if item["source"] == "skill")

    assert mcp["id"] == "mcp:pstack:route_task"
    assert mcp["name"] == "hirda__pstack__route_task"
    assert mcp["raw_name"] == "route_task"
    assert mcp["permission"] == "read"
    assert skill["id"] == "skill:code-review"
    assert skill["required_tools"] == ["hirda__pstack__route_task"]
    assert skill["runtime_constraints"] == ["computer.private"]


def test_capability_resolver_selects_provider_and_keeps_fallback_evidence():
    resolver = CapabilityResolver(_Integrations())
    result = resolver.resolve(
        required_capabilities=["coding", "missing.capability"],
        required_tools=["route_task"],
    )

    assert result["status"] == "partial"
    assert result["resolved"] is False
    assert result["selected_provider_ids"] == ["pstack"]
    assert result["unresolved_capabilities"] == ["missing.capability"]
    coding = result["capabilities"][0]
    assert coding["selected"]["id"] == "pstack"
    assert coding["selected"]["permissions"] == ["read"]
    tool = result["tools"][0]
    assert tool["selected"]["tool"]["name"] == "hirda__pstack__route_task"
    assert result["authority"] == "descriptive_only"
    assert result["dispatch_authority"] == "hirda"
