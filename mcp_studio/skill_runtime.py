from __future__ import annotations

import copy
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .skills import SkillManifestError, SkillRegistry, SkillResolution


SKILL_RESOLUTION_SCHEMA = "hirda-skill-resolution-v1"
DEFAULT_MAX_MANIFEST_BYTES = 256 * 1024
_REQUEST_KEYS = {"domain", "explicit_skill_ids", "advisory_skill_ids"}


class SkillRequestError(ValueError):
    """A Skills selection supplied in WorkSubmit metadata is malformed."""


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
        self.registry = SkillRegistry()
        self.status = "disabled"
        self.error_code: str | None = None
        self.loaded_skill_count = 0
        self.reload()

    def reload(self) -> str:
        """Atomically reload manifests, dropping all entries if any fail."""
        if self.enabled is False:
            self.registry = SkillRegistry()
            self.status = "disabled"
            self.error_code = None
            self.loaded_skill_count = 0
            return self.status
        if not isinstance(self.enabled, bool):
            self._set_failure("error", "registry_config_invalid")
            return self.status
        try:
            if (
                not isinstance(self.max_manifest_bytes, int)
                or isinstance(self.max_manifest_bytes, bool)
                or self.max_manifest_bytes < 1
            ):
                self._set_failure("error", "registry_config_invalid")
                return self.status
            root = Path(self.path).expanduser()
            if not root.is_absolute():
                root = Path(self.base_dir).expanduser() / root
            if not root.is_dir():
                self._set_failure("missing", "skill_registry_directory_missing")
                return self.status
            registry = SkillRegistry()
            loaded = registry.load_directory(root, max_manifest_bytes=self.max_manifest_bytes)
        except SkillManifestError:
            self._set_failure("error", "manifest_invalid")
            return self.status
        except OSError:
            self._set_failure("error", "registry_io_error")
            return self.status
        except Exception:
            self._set_failure("error", "registry_load_failed")
            return self.status
        self.registry = registry
        self.loaded_skill_count = len(loaded)
        self.status = "ready"
        self.error_code = None
        return self.status

    def _set_failure(self, status: str, error_code: str) -> None:
        self.registry = SkillRegistry()
        self.loaded_skill_count = 0
        self.status = status
        self.error_code = error_code

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
        if self.status == "disabled":
            return enriched

        domain = _validate_domain(metadata.get("domain"))
        explicit_ids = _validate_ids(metadata.get("explicit_skill_ids", ()))
        advisory_ids = _validate_ids(metadata.get("advisory_skill_ids", ()))
        if self.status == "ready":
            instruction = enriched.get("instruction")
            task_text = instruction.strip() if isinstance(instruction, str) and instruction.strip() else str(enriched.get("label", ""))
            resolution = self.registry.resolve(
                task_text,
                workspace=enriched.get("workspace"),
                domain=domain,
                explicit_skill_ids=explicit_ids,
                advisory_skill_ids=advisory_ids,
            )
            serialized = _serialize_resolution(
                resolution,
                registry_status=self.status,
                loaded_skill_count=self.loaded_skill_count,
                domain=domain,
            )
        else:
            serialized = _empty_resolution(
                registry_status=self.status,
                loaded_skill_count=0,
                domain=domain,
                unresolved_explicit_ids=explicit_ids,
                error_code=self.error_code,
            )
        metadata["skill_resolution"] = serialized
        return enriched


def _validate_domain(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
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
