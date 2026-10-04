from __future__ import annotations

import asyncio
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

from .capabilities import CapabilityResolver
from .db import Database
from .skill_runtime import SkillRuntime
from .skills import SkillDefinition, SkillManifestError, SkillRegistry, parse_skill_manifest


SKILL_CANDIDATE_SCHEMA = "hirda-skill-candidate-v1"
SKILL_CANDIDATE_CERT_SCHEMA = "hirda-skill-candidate-certification-v1"
SKILL_DISTILLATION_SCHEMA = "hirda-skill-distillation-v1"

_JSON_OBJECT = re.compile(r"\{.*\}", re.S)


def _next_patch_version(version: str) -> str:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", str(version or "").strip())
    if not match:
        return "0.1.0"
    major, minor, patch = (int(item) for item in match.groups())
    return f"{major}.{minor}.{patch + 1}"


def _json_object(text: str) -> dict[str, Any]:
    match = _JSON_OBJECT.search(str(text or "").strip())
    if not match:
        raise ValueError("distiller_json_missing")
    raw_object = match.group(0)
    try:
        decoded = json.loads(raw_object)
    except json.JSONDecodeError as exc:
        if "control character" not in str(exc).casefold():
            raise
        decoded = json.loads(_escape_json_control_chars(raw_object))
    if not isinstance(decoded, dict):
        raise ValueError("distiller_json_root_invalid")
    return decoded


def _escape_json_control_chars(text: str) -> str:
    """Escape raw control characters that appear inside JSON strings."""
    output: list[str] = []
    in_string = False
    escaped = False
    escapes = {
        "\b": "\\b",
        "\f": "\\f",
        "\n": "\\n",
        "\r": "\\r",
        "\t": "\\t",
    }
    for char in text:
        if escaped:
            output.append(char)
            escaped = False
            continue
        if in_string and char == "\\":
            output.append(char)
            escaped = True
            continue
        if char == '"':
            in_string = not in_string
            output.append(char)
            continue
        if in_string and ord(char) < 0x20:
            output.append(escapes.get(char, f"\\u{ord(char):04x}"))
            continue
        output.append(char)
    return "".join(output)


def _experience_summary(item: dict[str, Any]) -> dict[str, Any]:
    trace = item.get("trace")
    trace = trace if isinstance(trace, dict) else {}
    quality = item.get("quality_certification")
    quality = quality if isinstance(quality, dict) else {}
    execution = item.get("execution_certification")
    execution = execution if isinstance(execution, dict) else {}
    return {
        "experience_id": item.get("id"),
        "skill_ids": list(item.get("skill_ids") or []),
        "domain": item.get("domain"),
        "intent": item.get("intent"),
        "channel": item.get("channel"),
        "outcome": item.get("outcome"),
        "execution_passed": execution.get("passed") is True,
        "quality_passed": quality.get("passed") is True,
        "quality_score": quality.get("score"),
        "workflow_steps": list(trace.get("workflow_steps") or []),
        "verification_checks": list(trace.get("verification_checks") or []),
        "selected_provider_ids": list(trace.get("selected_provider_ids") or []),
    }


class SkillDistillationManager:
    """Distill certified experience with Hermes; HIRDA owns certification."""

    def __init__(
        self,
        db: Database,
        skill_runtime: SkillRuntime,
        capability_resolver: CapabilityResolver,
        cognitive_router: Any,
        *,
        min_experiences: int = 3,
        quality_threshold: float = 0.8,
        auto_promote: bool = False,
    ) -> None:
        self.db = db
        self.skill_runtime = skill_runtime
        self.capability_resolver = capability_resolver
        self.cognitive_router = cognitive_router
        self.min_experiences = max(2, int(min_experiences))
        self.quality_threshold = max(0.0, min(float(quality_threshold), 1.0))
        self.auto_promote = bool(auto_promote)
        self._background: set[asyncio.Task[Any]] = set()
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    def _skill_root(self) -> Path:
        root = Path(self.skill_runtime.path)
        if not root.is_absolute():
            root = Path(self.skill_runtime.base_dir) / root
        return root.resolve()

    def _existing(self, skill_id: str) -> SkillDefinition | None:
        variants = [
            skill
            for skill in self.skill_runtime.registry.snapshot()
            if skill.id == skill_id
        ]
        if not variants:
            return None
        return sorted(
            variants,
            key=lambda item: (
                item.scope.kind != "workspace",
                item.scope.kind != "domain",
                item.version,
            ),
        )[0]

    async def eligible_experiences(
        self,
        *,
        domain: str,
        intent: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        rows = await self.db.list_skill_experiences(
            domain=str(domain or "").strip() or None,
            limit=limit,
        )
        eligible: list[dict[str, Any]] = []
        for item in rows:
            if str(item.get("intent") or "").strip() != str(intent or "").strip():
                continue
            execution = item.get("execution_certification")
            execution = execution if isinstance(execution, dict) else {}
            quality = item.get("quality_certification")
            quality = quality if isinstance(quality, dict) else {}
            try:
                score = float(quality.get("score") or 0.0)
            except (TypeError, ValueError):
                score = 0.0
            if execution.get("passed") is not True:
                continue
            if quality.get("passed") is not True or score < self.quality_threshold:
                continue
            eligible.append(item)
        return eligible

    @staticmethod
    def _target_skill_id(
        experiences: list[dict[str, Any]],
    ) -> str | None:
        counts: Counter[str] = Counter()
        for item in experiences:
            for skill_id in item.get("skill_ids") or []:
                key = str(skill_id or "").strip()
                if key:
                    counts[key] += 1
        if not counts:
            return None
        return counts.most_common(1)[0][0]

    async def _already_distilled(
        self,
        experience_ids: list[str],
    ) -> bool:
        expected = set(experience_ids)
        for candidate in await self.db.list_skill_candidates(limit=200):
            if set(candidate.get("source_experience_ids") or []) != expected:
                continue
            status = str(candidate.get("status") or "").strip().casefold()
            if status in {"candidate", "certified", "promoted"}:
                return True
        return False

    async def distill(
        self,
        *,
        domain: str,
        intent: str,
        min_experiences: int | None = None,
    ) -> dict[str, Any]:
        minimum = max(2, int(min_experiences or self.min_experiences))
        experiences = await self.eligible_experiences(
            domain=domain,
            intent=intent,
        )
        if len(experiences) < minimum:
            return {
                "schema": SKILL_DISTILLATION_SCHEMA,
                "status": "not_enough_experience",
                "required": minimum,
                "eligible": len(experiences),
                "candidate": None,
            }

        source_ids = [str(item["id"]) for item in experiences]
        if await self._already_distilled(source_ids):
            return {
                "schema": SKILL_DISTILLATION_SCHEMA,
                "status": "already_distilled",
                "eligible": len(experiences),
                "candidate": None,
            }

        target_skill_id = self._target_skill_id(experiences)
        existing = self._existing(target_skill_id) if target_skill_id else None
        expected_id = target_skill_id or ""
        summaries = [_experience_summary(item) for item in experiences[:12]]
        prompt = (
            "You are Hermes Skill Distiller. Convert repeated, certified HIRDA "
            "experience into ONE provider-neutral procedural Skill Manifest v2. "
            "Return JSON only. Never include or choose model, runtime, machine, "
            "agent, provider, router, permissions, commands, code, credentials, "
            "tokens, account identities, or user secrets. Skills may only declare "
            "intent, capabilities, tools, runtime constraints, permission "
            "requirements, policy hints, workflow steps, verification checks, "
            "and provenance.\n"
            f"Domain: {domain}\nIntent: {intent}\n"
            f"Existing skill id (keep exactly when present): {expected_id or 'none'}\n"
            + (
                "Existing canonical dependency ids must be preserved exactly: "
                f"required_capabilities={list(existing.required_capabilities)}; "
                f"required_tools={list(existing.required_tools)}.\n"
                if existing is not None
                else ""
            )
            + "Required JSON keys: id,name,description,scope,triggers,intents,"
            "required_capabilities,required_tools,runtime_constraints,"
            "permission_requirements,policy_hints,workflow_steps,"
            "verification_checks. Scope must be an object with kind global or "
            "domain; domain scope value must equal the supplied domain.\n"
            f"Certified experience summaries: {json.dumps(summaries, ensure_ascii=False)}"
        )
        result = await self.cognitive_router.execute(
            {
                "schema": "hirda-cognitive-request-v1",
                "request_id": f"SKILL-DISTILL-{source_ids[0]}",
                "prompt": prompt,
                "capability": "reflection",
                "preferred_depth": "deep",
                "allow_fallback": False,
                "preferred_runtime": "hermes",
                "principal_id": "hirda-skill-distiller",
                "timeout_seconds": 60.0,
                "context": {
                    "purpose": "skill_distillation",
                    "authority": "hirda",
                    "domain": domain,
                    "intent": intent,
                    "experience_count": len(experiences),
                },
            }
        )
        if str(getattr(result, "status", "")) != "success":
            raise RuntimeError(
                f"hermes_skill_distillation_failed:{getattr(result, 'error', None)}"
            )
        if str(getattr(result, "runtime", "")).casefold() != "hermes":
            raise RuntimeError("skill_distillation_runtime_must_be_hermes")

        proposed = _json_object(str(getattr(result, "output", "") or ""))
        if existing is not None:
            proposed["id"] = existing.id
            proposed["version"] = _next_patch_version(existing.version)
            # Dependency identifiers are HIRDA registry contracts, not prose for
            # the distiller to rewrite. Skill evolution may improve workflow,
            # policy, and verification, but it must preserve canonical
            # capability/tool ids until HIRDA explicitly evolves the binding.
            proposed["required_capabilities"] = list(
                existing.required_capabilities
            )
            proposed["required_tools"] = list(existing.required_tools)
            operation = "update"
        else:
            proposed["version"] = "0.1.0"
            operation = "create"
        proposed["schema"] = "hirda-skill-v2"
        proposed.setdefault("scope", {"kind": "domain", "value": domain})
        proposed["provenance"] = {
            "origin": "hermes_distillation",
            "distiller_name": "hermes",
            "experience_count": len(experiences),
            "source_experience_ids": source_ids,
        }
        proposed.setdefault("metadata", {"domain": domain})

        parsed = parse_skill_manifest(
            proposed,
            source="hermes:candidate",
        )
        candidate = await self.db.create_skill_candidate(
            {
                "skill_id": parsed.id,
                "operation": operation,
                "status": "candidate",
                "manifest": proposed,
                "source_experience_ids": source_ids,
                "distiller_runtime": "hermes",
                "distiller_model": getattr(result, "model", None),
                "distiller_result": {
                    "schema": getattr(result, "schema", None),
                    "request_id": getattr(result, "request_id", None),
                    "lane": getattr(result, "lane", None),
                    "fallback_used": getattr(result, "fallback_used", False),
                },
            }
        )
        await self.db.add_event(
            "skill.candidate.created",
            f"Hermes distilled candidate {candidate['id']} for {parsed.id}.",
            data={
                "candidate_id": candidate["id"],
                "skill_id": parsed.id,
                "operation": operation,
                "experience_count": len(source_ids),
                "distiller": "hermes",
            },
            stream_id=f"skill-candidate:{candidate['id']}",
            correlation_id=candidate["id"],
        )
        return {
            "schema": SKILL_DISTILLATION_SCHEMA,
            "status": "candidate_created",
            "eligible": len(experiences),
            "candidate": candidate,
        }

    async def certify_candidate(
        self,
        candidate_id: str,
    ) -> dict[str, Any]:
        candidate = await self.db.get_skill_candidate(candidate_id)
        manifest = candidate.get("manifest")
        manifest = manifest if isinstance(manifest, dict) else {}
        checks: list[dict[str, Any]] = []

        def check(name: str, passed: bool, detail: Any = None) -> None:
            checks.append(
                {"name": name, "passed": bool(passed), "detail": detail}
            )

        skill: SkillDefinition | None = None
        try:
            skill = parse_skill_manifest(
                manifest,
                source=f"candidate:{candidate_id}",
            )
        except (SkillManifestError, TypeError, ValueError) as exc:
            check("manifest_valid", False, str(exc))
        else:
            check("manifest_valid", True, skill.id)

        check(
            "hermes_distiller",
            str(candidate.get("distiller_runtime") or "").casefold() == "hermes",
            candidate.get("distiller_runtime"),
        )
        source_ids = list(candidate.get("source_experience_ids") or [])
        check(
            "minimum_certified_experience",
            len(source_ids) >= self.min_experiences,
            len(source_ids),
        )
        source_passed = True
        for experience_id in source_ids:
            try:
                experience = await self.db.get_skill_experience(experience_id)
            except KeyError:
                source_passed = False
                break
            execution = experience.get("execution_certification") or {}
            quality = experience.get("quality_certification") or {}
            try:
                score = float(quality.get("score") or 0.0)
            except (TypeError, ValueError):
                score = 0.0
            if not (
                execution.get("passed") is True
                and quality.get("passed") is True
                and score >= self.quality_threshold
            ):
                source_passed = False
                break
        check("source_experience_certified", source_passed, source_ids)

        if skill is not None:
            registry = SkillRegistry()
            try:
                registry.register(skill)
                resolution = registry.resolve(
                    skill.intents[0] if skill.intents else skill.description,
                    domain=(
                        skill.scope.value
                        if skill.scope.kind == "domain"
                        else None
                    ),
                    intent=(skill.intents[0] if skill.intents else None),
                    explicit_skill_ids=[skill.id],
                )
                dry_run_ok = bool(resolution.matches)
            except Exception as exc:
                dry_run_ok = False
                check("resolver_dry_run", False, str(exc))
            else:
                check("resolver_dry_run", dry_run_ok, skill.id)

            skill_metadata = {
                "skill_resolution": {
                    "required_capabilities": list(skill.required_capabilities),
                    "required_tools": list(skill.required_tools),
                    "runtime_constraints": list(skill.runtime_constraints),
                    "permission_requirements": list(
                        skill.permission_requirements
                    ),
                }
            }
            capability = self.capability_resolver.resolve_skill_metadata(
                skill_metadata
            )
            check(
                "capability_resolution",
                bool(capability.get("resolved")),
                {
                    "status": capability.get("status"),
                    "unresolved_capabilities": capability.get(
                        "unresolved_capabilities"
                    ),
                    "unresolved_tools": capability.get("unresolved_tools"),
                },
            )

        passed = all(item["passed"] for item in checks)
        certification = {
            "schema": SKILL_CANDIDATE_CERT_SCHEMA,
            "passed": passed,
            "outcome": "PASS" if passed else "FAIL",
            "checks": checks,
            "candidate_id": candidate_id,
        }
        updated = await self.db.update_skill_candidate(
            candidate_id,
            status="certified" if passed else "rejected",
            certification=certification,
        )
        await self.db.add_event(
            "skill.candidate.certified",
            f"Skill candidate {candidate_id}: {certification['outcome']}.",
            severity="info" if passed else "warning",
            data=certification,
            stream_id=f"skill-candidate:{candidate_id}",
            correlation_id=candidate_id,
        )
        return updated

    async def promote_candidate(
        self,
        candidate_id: str,
    ) -> dict[str, Any]:
        candidate = await self.db.get_skill_candidate(candidate_id)
        if candidate.get("status") != "certified":
            raise ValueError("skill_candidate_not_certified")
        manifest = candidate.get("manifest")
        manifest = manifest if isinstance(manifest, dict) else {}
        skill = parse_skill_manifest(
            manifest,
            source=f"candidate:{candidate_id}",
        )
        root = self._skill_root()
        root.mkdir(parents=True, exist_ok=True)
        target = (root / f"{skill.id}.yaml").resolve()
        if root not in target.parents:
            raise ValueError("skill_candidate_path_invalid")
        temporary = target.with_suffix(".yaml.tmp")
        temporary.write_text(
            yaml.safe_dump(
                manifest,
                sort_keys=False,
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
        temporary.replace(target)
        self.skill_runtime.reload()
        promoted = next(
            (
                item
                for item in self.skill_runtime.registry.snapshot()
                if item.id == skill.id and item.version == skill.version
            ),
            None,
        )
        if promoted is None:
            raise RuntimeError("skill_candidate_promotion_reload_failed")
        updated = await self.db.update_skill_candidate(
            candidate_id,
            status="promoted",
            promoted_path=str(target),
        )
        await self.db.add_event(
            "skill.candidate.promoted",
            f"Skill candidate {candidate_id} promoted as {skill.id} {skill.version}.",
            data={
                "candidate_id": candidate_id,
                "skill_id": skill.id,
                "version": skill.version,
                "path": str(target),
            },
            stream_id=f"skill-candidate:{candidate_id}",
            correlation_id=candidate_id,
        )
        return updated

    async def maybe_distill(
        self,
        *,
        domain: str,
        intent: str,
    ) -> dict[str, Any] | None:
        key = (str(domain or "").strip(), str(intent or "").strip())
        if not all(key):
            return None
        lock = self._locks.setdefault(key, asyncio.Lock())
        if lock.locked():
            return None
        async with lock:
            experiences = await self.eligible_experiences(
                domain=key[0],
                intent=key[1],
            )
            if len(experiences) < self.min_experiences:
                return None
            return await self.distill(
                domain=key[0],
                intent=key[1],
            )


    async def _auto_cycle(
        self,
        *,
        domain: str,
        intent: str,
    ) -> dict[str, Any] | None:
        result = await self.maybe_distill(domain=domain, intent=intent)
        if not result or result.get("status") != "candidate_created":
            return result
        candidate = result.get("candidate") or {}
        candidate_id = str(candidate.get("id") or "").strip()
        if not candidate_id:
            return result
        certified = await self.certify_candidate(candidate_id)
        if self.auto_promote and certified.get("status") == "certified":
            await self.promote_candidate(candidate_id)
        return result

    def kick(self, experience: dict[str, Any] | None) -> bool:
        item = experience if isinstance(experience, dict) else {}
        domain = str(item.get("domain") or "").strip()
        intent = str(item.get("intent") or "").strip()
        quality = item.get("quality_certification")
        quality = quality if isinstance(quality, dict) else {}
        if not domain or not intent or quality.get("passed") is not True:
            return False
        task = asyncio.create_task(
            self._auto_cycle(domain=domain, intent=intent),
            name=f"skill-distill:{domain}:{intent}",
        )
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return True
