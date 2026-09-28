from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any
from uuid import uuid4

from mcp_studio.integrations import IntegrationManifest


PSTACK_REVISION = "c02fd4922b25ee005f42042463d741d236c2c35e"
PSTACK_REPOSITORY = "michael-denyer/pstack-claude"

_WORKFLOW_SOURCES = {
    "bug-fix": "plugins/pstack/skills/poteto-mode/playbooks/bug-fix.md",
    "feature": "plugins/pstack/skills/poteto-mode/playbooks/feature.md",
    "review": "plugins/pstack/skills/interrogate/SKILL.md",
}

_ROLE_PROFILES = {
    "coordinator": ("coding", "deep"),
    "code": ("coding", "deep"),
    "investigator": ("research", "deep"),
    "reviewer": ("coding", "deep"),
    "judgment": ("reasoning_high", "deep"),
    "prose": ("reflection", "auto"),
}


class PstackAdapterError(RuntimeError):
    pass


def _descriptor(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str],
) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    }


_TOOL_CATALOG = {
    "route_task": _descriptor(
        "route_task",
        "Choose a pinned Pstack workflow and build a HIRDA-owned coordinator request.",
        {
            "goal": {"type": "string", "minLength": 1, "maxLength": 120000},
            "workflow": {
                "type": ["string", "null"],
                "enum": ["bug-fix", "feature", "review", None],
                "default": None,
            },
        },
        ["goal"],
    ),
    "load_workflow": _descriptor(
        "load_workflow",
        "Load the exact pinned Pstack methodology text for one supported workflow.",
        {
            "workflow": {
                "type": "string",
                "enum": ["bug-fix", "feature", "review"],
            }
        },
        ["workflow"],
    ),
    "build_role_request": _descriptor(
        "build_role_request",
        "Translate a Pstack role task into a runtime-neutral HIRDA cognitive request.",
        {
            "role": {
                "type": "string",
                "enum": sorted(_ROLE_PROFILES),
            },
            "task": {"type": "string", "minLength": 1, "maxLength": 120000},
            "workflow": {
                "type": ["string", "null"],
                "enum": ["bug-fix", "feature", "review", None],
                "default": None,
            },
        },
        ["role", "task"],
    ),
}


class PstackIntegration:
    def __init__(self, *, repository_root: Path | None = None) -> None:
        root = repository_root or Path(__file__).resolve().parents[2]
        self.repository_root = Path(root).resolve(strict=False)
        self.pstack_root = self.repository_root / "third_party" / "pstack-claude"
        self.manifest = IntegrationManifest.from_mapping(
            {
                "id": "pstack",
                "name": "Pstack Engineering Workflow",
                "capabilities": [
                    "engineering_workflow",
                    "workflow_routing",
                    "role_request_translation",
                ],
                "runtime": {"type": "local-python"},
                "tools": list(_TOOL_CATALOG),
                "permissions": {name: "read" for name in _TOOL_CATALOG},
                "health": {"type": "adapter_probe"},
                "routing": {
                    "mode": "methodology-layer",
                    "preferred_lanes": ["claude", "codex", "hermes"],
                },
                "metadata": {
                    "runtime_authority": "hirda",
                    "model_selection": "deferred_to_hirda",
                    "upstream_repository": PSTACK_REPOSITORY,
                    "upstream_revision": PSTACK_REVISION,
                },
            }
        )

    def _revision(self) -> str | None:
        if not self.pstack_root.is_dir():
            return None
        try:
            result = subprocess.run(
                ["git", "-C", str(self.pstack_root), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
                timeout=3,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return result.stdout.strip() or None

    def _workflow_path(self, workflow: str) -> Path:
        key = str(workflow or "").strip().casefold()
        relative = _WORKFLOW_SOURCES.get(key)
        if relative is None:
            raise PstackAdapterError("pstack_workflow_invalid")
        path = (self.pstack_root / relative).resolve(strict=False)
        if self.pstack_root.resolve(strict=False) not in path.parents:
            raise PstackAdapterError("pstack_workflow_path_escape")
        return path

    def _load_workflow(self, workflow: str) -> dict[str, Any]:
        path = self._workflow_path(workflow)
        if not path.is_file():
            raise PstackAdapterError("pstack_workflow_source_missing")
        text = path.read_text(encoding="utf-8")
        return {
            "workflow": workflow,
            "source": {
                "repository": PSTACK_REPOSITORY,
                "revision": PSTACK_REVISION,
                "path": str(path.relative_to(self.pstack_root)),
            },
            "methodology": text,
        }

    @staticmethod
    def _select_workflow(goal: str, requested: str | None = None) -> str:
        explicit = str(requested or "").strip().casefold()
        if explicit:
            if explicit not in _WORKFLOW_SOURCES:
                raise PstackAdapterError("pstack_workflow_invalid")
            return explicit
        lowered = goal.casefold()
        if re.search(r"\b(review|interrogate|audit|diff|pull request|pr)\b", lowered):
            return "review"
        if re.search(r"\b(bug|fix|broken|regression|error|crash|defect)\b", lowered):
            return "bug-fix"
        return "feature"

    @staticmethod
    def _role_profile(role: str) -> tuple[str, str]:
        key = str(role or "").strip().casefold()
        try:
            return _ROLE_PROFILES[key]
        except KeyError:
            raise PstackAdapterError("pstack_role_invalid") from None

    def _build_role_request(
        self,
        *,
        role: str,
        task: str,
        workflow: str | None,
        methodology: str | None = None,
    ) -> dict[str, Any]:
        role_key = str(role or "").strip().casefold()
        task_text = str(task or "").strip()
        if not task_text:
            raise PstackAdapterError("pstack_task_required")
        capability, depth = self._role_profile(role_key)
        workflow_key = (
            self._select_workflow(task_text, workflow)
            if workflow is not None
            else None
        )
        prompt_parts = [
            f"PSTACK ROLE: {role_key}",
            "HIRDA owns model and runtime selection. Do not pin or request a model.",
        ]
        if workflow_key:
            prompt_parts.append(f"PSTACK WORKFLOW: {workflow_key}")
        if methodology:
            prompt_parts.extend(["PSTACK METHODOLOGY:", methodology])
        prompt_parts.extend(["TASK:", task_text])
        request = {
            "schema": "hirda-cognitive-request-v1",
            "request_id": f"PSTACK-{uuid4().hex[:12].upper()}",
            "prompt": "\n\n".join(prompt_parts),
            "capability": capability,
            "preferred_depth": depth,
            "allow_fallback": True,
            "context": {
                "workflow_provider": "pstack",
                "workflow": workflow_key,
                "role": role_key,
                "upstream_repository": PSTACK_REPOSITORY,
                "upstream_revision": PSTACK_REVISION,
                "runtime_authority": "hirda",
            },
        }
        return {
            "schema": "hirda-pstack-role-request-v1",
            "role": role_key,
            "workflow": workflow_key,
            "runtime_authority": "hirda",
            "model_selection": "deferred",
            "hirda_request": request,
        }

    async def list_tools(self) -> list[dict[str, Any]]:
        return [dict(_TOOL_CATALOG[name]) for name in self.manifest.tools]

    async def validate(self) -> dict[str, Any]:
        revision = self._revision()
        missing = [
            workflow
            for workflow in _WORKFLOW_SOURCES
            if not self._workflow_path(workflow).is_file()
        ]
        ok = revision == PSTACK_REVISION and not missing
        return {
            "ok": ok,
            "repository": PSTACK_REPOSITORY,
            "expected_revision": PSTACK_REVISION,
            "observed_revision": revision,
            "missing_workflows": missing,
            "runtime_authority": "hirda",
        }

    async def certify(self) -> dict[str, Any]:
        samples = {
            "bug-fix": "Fix the crash when a managed session reconnects.",
            "feature": "Add a bounded workflow adapter for engineering tasks.",
            "review": "Review this diff for correctness and maintainability.",
        }
        routes = {}
        for workflow, goal in samples.items():
            selected = self._select_workflow(goal)
            request = self._build_role_request(
                role="coordinator",
                task=goal,
                workflow=selected,
            )
            routes[workflow] = {
                "selected": selected,
                "runtime_neutral": "preferred_runtime"
                not in request["hirda_request"],
            }
        return {
            "ok": all(
                item["selected"] == workflow and item["runtime_neutral"]
                for workflow, item in routes.items()
            ),
            "routes": routes,
        }

    async def status(self) -> dict[str, Any]:
        validation = await self.validate()
        return {
            **validation,
            "schema": "hirda-pstack-status-v1",
            "supported_workflows": sorted(_WORKFLOW_SOURCES),
            "supported_roles": sorted(_ROLE_PROFILES),
        }

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        del context
        if name == "load_workflow":
            workflow = str(arguments.get("workflow") or "").strip().casefold()
            return self._load_workflow(workflow)
        if name == "build_role_request":
            workflow = arguments.get("workflow")
            workflow_key = (
                str(workflow).strip().casefold()
                if workflow is not None
                else None
            )
            return self._build_role_request(
                role=str(arguments.get("role") or ""),
                task=str(arguments.get("task") or ""),
                workflow=workflow_key,
            )
        if name == "route_task":
            goal = str(arguments.get("goal") or "").strip()
            if not goal:
                raise PstackAdapterError("pstack_goal_required")
            workflow = self._select_workflow(
                goal,
                arguments.get("workflow"),
            )
            loaded = self._load_workflow(workflow)
            coordinator = self._build_role_request(
                role="coordinator",
                task=goal,
                workflow=workflow,
                methodology=loaded["methodology"],
            )
            return {
                "schema": "hirda-pstack-route-v1",
                "workflow": workflow,
                "source": loaded["source"],
                "methodology": loaded["methodology"],
                "runtime_authority": "hirda",
                "model_selection": "deferred",
                "coordinator": coordinator,
            }
        raise PstackAdapterError("pstack_tool_unknown")
