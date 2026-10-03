from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any

from .skills import SkillManifestError, SkillRegistry, SkillResolution


SKILL_RESOLUTION_SCHEMA = "hirda-skill-resolution-v1"
DEFAULT_MAX_MANIFEST_BYTES = 256 * 1024
MAX_SKILL_DOMAIN_CHARS = 120
MAX_SKILL_ID_CHARS = 128
MAX_SKILL_ID_COUNT = 32
MAX_TASK_TEXT_CHARS = 4096


class SkillRequestError(ValueError):
    """A Skills selection supplied in WorkSubmit metadata is malformed."""


@dataclass(frozen=True, slots=True)
class _RuntimeState:
    registry: SkillRegistry
    status: str
    loaded_skill_count: int
    error_code: str | None


class SkillRuntime:
    """Loads declarative Skills and attaches advisory-only resolution metadata."""

    def __init__(
        self,
        *,
        enabled: bool,
        path: str | Path,
        max_manifest_bytes: int = DEFAULT_MAX_MANIFEST_BYTES,
        base_dir: str | Path = ".",
    ) -> None:
        self.enabled = enabled
        self.path = path
        self.max_manifest_bytes = max_manifest_bytes
        self.base_dir = base_dir
        self._state_lock = Lock()
        self._state = _RuntimeState(SkillRegistry(), "disabled", 0, None)
        self.reload()

    def _snapshot(self) -> _RuntimeState:
        with self._state_lock:
            return self._state

    def _publish(self, state: _RuntimeState) -> None:
        with self._state_lock:
            self._state = state

    @property
    def registry(self) -> SkillRegistry:
        return self._snapshot().registry

    @property
    def status(self) -> str:
        return self._snapshot().status

    @property
    def error_code(self) -> str | None:
        return self._snapshot().error_code

    @property
    def loaded_skill_count(self) -> int:
        return self._snapshot().loaded_skill_count

    def reload(self) -> str:
        """Atomically reload manifests, dropping all entries if any fail."""
        if self.enabled is False:
            state = _RuntimeState(SkillRegistry(), "disabled", 0, None)
            self._publish(state)
            return state.status
        if not isinstance(self.enabled, bool):
            state = _RuntimeState(SkillRegistry(), "error", 0, "registry_config_invalid")
            self._publish(state)
            return state.status
        try:
            if (
                not isinstance(self.max_manifest_bytes, int)
                or isinstance(self.max_manifest_bytes, bool)
                or self.max_manifest_bytes < 1
            ):
                state = _RuntimeState(SkillRegistry(), "error", 0, "registry_config_invalid")
                self._publish(state)
                return state.status
            root = Path(self.path).expanduser()
            if not root.is_absolute():
                root = Path(self.base_dir).expanduser() / root
            if not root.is_dir():
                state = _RuntimeState(SkillRegistry(), "missing", 0, "skill_registry_directory_missing")
                self._publish(state)
                return state.status
            registry = SkillRegistry()
            loaded = registry.load_directory(root, max_manifest_bytes=self.max_manifest_bytes)
        except SkillManifestError:
            state = _RuntimeState(SkillRegistry(), "error", 0, "manifest_invalid")
            self._publish(state)
            return state.status
        except OSError:
            state = _RuntimeState(SkillRegistry(), "error", 0, "registry_io_error")
            self._publish(state)
            return state.status
        except Exception:
            state = _RuntimeState(SkillRegistry(), "error", 0, "registry_load_failed")
            self._publish(state)
            return state.status
        state = _RuntimeState(registry, "ready", len(loaded), None)
        self._publish(state)
        return state.status

    def enrich_work_payload(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Copy a WorkSubmit payload, resolve only when enabled, and preserve metadata."""
        if not isinstance(payload, Mapping):
            raise TypeError("work_payload_must_be_mapping")
        enriched = copy.deepcopy(dict(payload))
        raw_metadata = enriched.get("metadata", {})
        if not isinstance(raw_metadata, Mapping):
            raise TypeError("work_metadata_must_be_mapping")
        metadata = copy.deepcopy(dict(raw_metadata))
        enriched["metadata"] = metadata
        state = self._snapshot()
        if state.status == "disabled":
            return enriched

        domain = _validate_domain(metadata.get("domain"))
        explicit_ids = _validate_ids(metadata.get("explicit_skill_ids", ()))
        advisory_ids = _validate_ids(metadata.get("advisory_skill_ids", ()))
        if len(explicit_ids) + len(advisory_ids) > MAX_SKILL_ID_COUNT:
            raise SkillRequestError("skill_request_invalid")
        if state.status == "ready":
            label = enriched.get("label")
            instruction = enriched.get("instruction")
            label_text = label.strip() if isinstance(label, str) else ""
            instruction_text = instruction.strip() if isinstance(instruction, str) else ""
            task_text = "\n\n".join(part for part in (label_text, instruction_text) if part)
            task_text = task_text[:MAX_TASK_TEXT_CHARS]
            resolution = state.registry.resolve(
                task_text,
                workspace=enriched.get("workspace"),
                domain=domain,
                explicit_skill_ids=explicit_ids,
                advisory_skill_ids=advisory_ids,
            )
            serialized = _serialize_resolution(
                resolution,
                registry_status=state.status,
                loaded_skill_count=state.loaded_skill_count,
                domain=domain,
            )
        else:
            serialized = _empty_resolution(
                registry_status=state.status,
                loaded_skill_count=0,
                domain=domain,
                unresolved_explicit_ids=explicit_ids,
                error_code=state.error_code,
            )
        metadata["skill_resolution"] = serialized
        return enriched


def _validate_domain(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > MAX_SKILL_DOMAIN_CHARS:
        raise SkillRequestError("skill_request_invalid")
    return value.strip()


def _validate_ids(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise SkillRequestError("skill_request_invalid")
    result: list[str] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, str) or not raw.strip():
            raise SkillRequestError("skill_request_invalid")
        skill_id = raw.strip()
        if len(skill_id) > MAX_SKILL_ID_CHARS:
            raise SkillRequestError("skill_request_invalid")
        if skill_id in seen:
            raise SkillRequestError("skill_request_invalid")
        seen.add(skill_id)
        result.append(skill_id)
    return tuple(result)


def _serialize_resolution(
    resolution: SkillResolution,
    *,
    registry_status: str,
    loaded_skill_count: int,
    domain: str | None,
) -> dict[str, Any]:
    return {
        "schema": SKILL_RESOLUTION_SCHEMA,
        "registry_status": registry_status,
        "loaded_skill_count": loaded_skill_count,
        "domain": domain,
        "selected": [
            {
                "id": match.skill.id,
                "name": match.skill.name,
                "version": match.skill.version,
                "scope": {"kind": match.skill.scope.kind, "value": match.skill.scope.value},
                "source": match.skill.source,
                "explicit": bool(match.explicit),
                "advisory": bool(match.advisory),
                "trigger_matches": list(match.trigger_matches),
            }
            for match in resolution.matches
        ],
        "unresolved_explicit_ids": list(resolution.unresolved_explicit_ids),
        "required_capabilities": list(resolution.required_capabilities),
        "required_tools": list(resolution.required_tools),
        "policy_hints": list(resolution.policy_hints),
        "workflow_steps": list(resolution.workflow_steps),
        "verification_checks": list(resolution.verification_checks),
    }


def _empty_resolution(
    *,
    registry_status: str,
    loaded_skill_count: int,
    domain: str | None,
    unresolved_explicit_ids: tuple[str, ...],
    error_code: str | None,
) -> dict[str, Any]:
    result = {
        "schema": SKILL_RESOLUTION_SCHEMA,
        "registry_status": registry_status,
        "loaded_skill_count": loaded_skill_count,
        "domain": domain,
        "selected": [],
        "unresolved_explicit_ids": list(unresolved_explicit_ids),
        "required_capabilities": [],
        "required_tools": [],
        "policy_hints": [],
        "workflow_steps": [],
        "verification_checks": [],
    }
    if error_code is not None:
        result["error_code"] = error_code
    return result
