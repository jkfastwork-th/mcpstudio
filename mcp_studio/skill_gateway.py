from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from .capabilities import CapabilityResolver
from .db import Database
from .skill_runtime import SkillRequestError, SkillRuntime


SKILL_GATEWAY_RESOLUTION_SCHEMA = "hirda-skill-gateway-resolution-v1"
SKILL_INVOCATION_SCHEMA = "hirda-skill-invocation-v1"
SKILL_EXPERIENCE_SCHEMA = "hirda-skill-experience-v1"

_SECRETISH = re.compile(
    r"(?:secret|token|password|credential|authorization|api[_-]?key|private[_-]?key)",
    re.I,
)
_MAX_TEXT = 12000
_MAX_COLLECTION = 100


def _sanitize(value: Any, *, depth: int = 0) -> Any:
    if depth > 8:
        return "<depth-limit>"
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= _MAX_COLLECTION:
                result["__truncated__"] = True
                break
            key_text = str(key)
            if _SECRETISH.search(key_text):
                result[key_text] = "<redacted>"
            else:
                result[key_text] = _sanitize(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        return [
            _sanitize(item, depth=depth + 1)
            for item in items[:_MAX_COLLECTION]
        ]
    if isinstance(value, str):
        return value[:_MAX_TEXT]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:_MAX_TEXT]


def _infer_domain(intent: str | None, domain: str | None) -> str | None:
    if domain and str(domain).strip():
        return str(domain).strip()
    intent_key = str(intent or "").strip().casefold()
    if intent_key.startswith("social."):
        return "social-hub"
    return None


def _input_text(payload: Mapping[str, Any]) -> str:
    for key in ("instruction", "goal", "request", "text", "prompt"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    try:
        return json.dumps(_sanitize(payload), ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(_sanitize(payload))


def _skill_public(skill: Any) -> dict[str, Any]:
    return {
        "schema": skill.schema,
        "id": skill.id,
        "name": skill.name,
        "version": skill.version,
        "description": skill.description,
        "scope": {
            "kind": skill.scope.kind,
            "value": skill.scope.value,
        },
        "triggers": list(skill.triggers),
        "intents": list(skill.intents),
        "required_capabilities": list(skill.required_capabilities),
        "required_tools": list(skill.required_tools),
        "runtime_constraints": list(skill.runtime_constraints),
        "permission_requirements": list(skill.permission_requirements),
        "policy_hints": list(skill.policy_hints),
        "workflow_steps": list(skill.workflow_steps),
        "verification_checks": list(skill.verification_checks),
        "provenance": _sanitize(dict(skill.provenance)),
        "source": skill.source,
    }


class SkillGateway:
    """HIRDA-owned skill entry point for resolve/run/inspect.

    Skills remain declarative. This gateway prepares a governed invocation;
    GatewaySessionManager opens the PStack task and HIRDA still owns runtime,
    model, machine, provider, permissions and dispatch.
    """

    def __init__(
        self,
        db: Database,
        skill_runtime: SkillRuntime,
        capability_resolver: CapabilityResolver,
    ) -> None:
        self.db = db
        self.skill_runtime = skill_runtime
        self.capability_resolver = capability_resolver

    def resolve(
        self,
        *,
        intent: str | None,
        input_data: Mapping[str, Any] | None,
        workspace: str | None,
        domain: str | None = None,
        skill_id: str | None = None,
    ) -> dict[str, Any]:
        clean_input = copy.deepcopy(dict(input_data or {}))
        resolved_domain = _infer_domain(intent, domain)
        explicit = [skill_id] if skill_id else []
        instruction = _input_text(clean_input)
        payload = {
            "label": str(intent or skill_id or "skill invocation")[:240],
            "workspace": str(workspace or "."),
            "priority": 50,
            "lease_mode": "write",
            "dispatch_mode": "herdr",
            "instruction": instruction,
            "metadata": {
                "domain": resolved_domain,
                "skill_intent": str(intent or "").strip() or None,
                "explicit_skill_ids": explicit,
            },
        }
        try:
            enriched = self.skill_runtime.enrich_work_payload(payload)
        except (SkillRequestError, TypeError, ValueError) as exc:
            raise SkillRequestError(f"skill_resolution_failed:{exc}") from exc
        metadata = enriched.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        resolution = metadata.get("skill_resolution")
        resolution = resolution if isinstance(resolution, dict) else {}
        capability = self.capability_resolver.resolve_skill_metadata(metadata)
        selected = list(resolution.get("selected") or [])
        return {
            "schema": SKILL_GATEWAY_RESOLUTION_SCHEMA,
            "intent": str(intent or "").strip() or None,
            "domain": resolved_domain,
            "workspace": workspace,
            "selected": selected,
            "resolved": bool(selected) and bool(capability.get("resolved")),
            "skill_resolution": resolution,
            "capability_resolution": capability,
            "authority": {
                "skill": "declarative_procedural_knowledge",
                "runtime": "hirda",
                "model": "hirda",
                "machine": "hirda",
                "provider": "hirda",
                "permissions": "hirda",
                "dispatch": "hirda",
            },
        }

    async def inspect(
        self,
        *,
        skill_id: str,
        experience_limit: int = 10,
    ) -> dict[str, Any]:
        key = str(skill_id or "").strip()
        variants = [
            _skill_public(skill)
            for skill in self.skill_runtime.registry.snapshot()
            if skill.id == key
        ]
        experiences = await self.db.list_skill_experiences(
            skill_id=key,
            limit=max(1, min(int(experience_limit or 10), 100)),
        )
        return {
            "schema": "hirda-skill-inspection-v1",
            "skill_id": key,
            "found": bool(variants),
            "variants": variants,
            "experience_count": len(experiences),
            "experiences": experiences,
        }

    def prepare_run(
        self,
        *,
        intent: str | None,
        input_data: Mapping[str, Any] | None,
        workspace: str,
        domain: str | None = None,
        skill_id: str | None = None,
        priority: int = 50,
    ) -> dict[str, Any]:
        resolution = self.resolve(
            intent=intent,
            input_data=input_data,
            workspace=workspace,
            domain=domain,
            skill_id=skill_id,
        )
        selected = list(resolution.get("selected") or [])
        if not selected:
            raise SkillRequestError("skill_not_resolved")
        capability = dict(resolution.get("capability_resolution") or {})
        if not bool(capability.get("resolved")):
            raise SkillRequestError("skill_capability_unresolved")

        clean_input = _sanitize(dict(input_data or {}))
        invocation_id = f"skill-inv-{uuid4().hex[:16]}"
        skill_ids = [
            str(item.get("id") or "")
            for item in selected
            if str(item.get("id") or "").strip()
        ]
        skill_resolution = dict(resolution.get("skill_resolution") or {})
        workflow = list(skill_resolution.get("workflow_steps") or [])
        verification = list(skill_resolution.get("verification_checks") or [])
        permissions = list(
            skill_resolution.get("permission_requirements") or []
        )
        goal = (
            "HIRDA SKILL INVOCATION\n"
            f"Intent: {resolution.get('intent') or 'unspecified'}\n"
            f"Skills: {', '.join(skill_ids)}\n"
            f"Domain: {resolution.get('domain') or 'unspecified'}\n"
            f"Workflow: {json.dumps(workflow, ensure_ascii=False)}\n"
            f"Verification: {json.dumps(verification, ensure_ascii=False)}\n"
            f"Permission requirements: {json.dumps(permissions, ensure_ascii=False)}\n"
            "Governance: HIRDA retains runtime/model/machine/provider/permission/dispatch authority. "
            "Do not bypass PStack or call an undeclared capability.\n"
            f"Input: {json.dumps(clean_input, ensure_ascii=False, sort_keys=True)}"
        )
        invocation = {
            "schema": SKILL_INVOCATION_SCHEMA,
            "invocation_id": invocation_id,
            "intent": resolution.get("intent"),
            "domain": resolution.get("domain"),
            "skill_ids": skill_ids,
            "input": clean_input,
            "skill_resolution": skill_resolution,
            "capability_resolution": capability,
            "priority": max(0, min(int(priority), 100)),
            "authority": resolution.get("authority"),
        }
        return {
            "invocation": invocation,
            "goal": goal,
            "task_overrides": {
                "priority": invocation["priority"],
                "skill_invocation": invocation,
                "skill_resolution": skill_resolution,
                "capability_resolution": capability,
                "runtime_constraints": list(
                    skill_resolution.get("runtime_constraints") or []
                ),
            },
            "resolution": resolution,
        }

    async def record_experience(
        self,
        *,
        work: dict[str, Any],
        pstack_task: dict[str, Any] | None,
        execution_certification: dict[str, Any],
        quality_certification: dict[str, Any],
    ) -> dict[str, Any] | None:
        metadata = work.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        invocation = metadata.get("skill_invocation")
        invocation = invocation if isinstance(invocation, dict) else {}
        invocation_id = str(invocation.get("invocation_id") or "").strip()
        if not invocation_id:
            return None
        if execution_certification.get("passed") is not True:
            return None

        clean_input = _sanitize(invocation.get("input") or {})
        identity: dict[str, Any] = {}
        if isinstance(clean_input, dict):
            for key in ("identity", "account", "account_id", "profile", "page"):
                if key in clean_input:
                    identity[key] = clean_input[key]
        channel = (
            str(clean_input.get("channel") or "").strip()
            if isinstance(clean_input, dict)
            else ""
        ) or None
        result = work.get("result")
        result = result if isinstance(result, dict) else {}
        trace = {
            "schema": SKILL_EXPERIENCE_SCHEMA,
            "work_state": work.get("state"),
            "execution_state": work.get("execution_state"),
            "semantic_completion": result.get("semantic_completion"),
            "runtime_readiness": result.get("runtime_readiness"),
            "capability_resolution": invocation.get("capability_resolution"),
            "workflow_steps": (
                (invocation.get("skill_resolution") or {}).get("workflow_steps")
                if isinstance(invocation.get("skill_resolution"), dict)
                else []
            ),
            "verification_checks": (
                (invocation.get("skill_resolution") or {}).get(
                    "verification_checks"
                )
                if isinstance(invocation.get("skill_resolution"), dict)
                else []
            ),
            "selected_provider_ids": (
                (invocation.get("capability_resolution") or {}).get(
                    "selected_provider_ids"
                )
                if isinstance(invocation.get("capability_resolution"), dict)
                else []
            ),
        }
        task = pstack_task if isinstance(pstack_task, dict) else {}
        quality_passed = quality_certification.get("passed") is True
        experience = await self.db.record_skill_experience(
            {
                "invocation_id": invocation_id,
                "work_id": work.get("id"),
                "pstack_task_id": (
                    task.get("task_id")
                    or metadata.get("pstack_task_id")
                ),
                "skill_ids": list(invocation.get("skill_ids") or []),
                "domain": invocation.get("domain"),
                "intent": invocation.get("intent"),
                "input": clean_input,
                "identity": identity,
                "channel": channel,
                "outcome": (
                    "quality_pass"
                    if quality_passed
                    else "quality_fail"
                ),
                "execution_certification": execution_certification,
                "quality_certification": quality_certification,
                "trace": trace,
            }
        )
        await self.db.add_event(
            "skill.experience.recorded",
            (
                f"Skill experience {experience['id']} recorded for "
                f"{work.get('id')}."
            ),
            data={
                "experience_id": experience["id"],
                "invocation_id": invocation_id,
                "work_id": work.get("id"),
                "skill_ids": experience.get("skill_ids"),
                "outcome": experience.get("outcome"),
            },
            stream_id=f"skill:{invocation_id}",
            correlation_id=invocation_id,
        )
        return experience
