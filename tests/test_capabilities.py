from types import SimpleNamespace

from mcp_studio.capabilities import (
    CAPABILITY_REGISTRY_SCHEMA,
    build_capability_registry,
)


class _Integrations:
    def backend_tools(self):
        return [
            {
                "name": "hirda__pstack__route_task",
                "description": "Route a task",
                "inputSchema": {"type": "object"},
            }
        ]

    def backend_route(self, name):
        assert name == "hirda__pstack__route_task"
        return ("pstack", "route_task")


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
    assert result["sources"] == {"mcp": 1, "skill": 1}

    mcp = next(item for item in result["capabilities"] if item["source"] == "mcp")
    skill = next(item for item in result["capabilities"] if item["source"] == "skill")

    assert mcp["id"] == "mcp:pstack:route_task"
    assert mcp["name"] == "hirda__pstack__route_task"
    assert mcp["raw_name"] == "route_task"
    assert skill["id"] == "skill:code-review"
    assert skill["required_tools"] == ["hirda__pstack__route_task"]
