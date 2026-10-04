from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Literal, Mapping

import yaml


_MAX_MANIFEST_BYTES = 256 * 1024
_SKILL_ID = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_AUTHORITY_KEYS = {
    "model", "models", "runtime", "runtimes", "machine", "machines",
    "agent", "agents", "permission", "permissions", "provider", "router", "routing",
}
_EXECUTABLE_KEYS = {"code", "executable", "adapter", "command", "commands", "script", "scripts"}
_SECRET_KEY = re.compile(r"(?:secret|token|password|credential|authorization|api[_-]?key|private[_-]?key)", re.I)
_TOP_LEVEL_KEYS = {
    "schema", "id", "name", "version", "description", "scope", "triggers",
    "intents", "required_capabilities", "required_tools", "runtime_constraints",
    "permission_requirements", "policy_hints", "workflow_steps",
    "verification_checks", "provenance", "metadata",
}
_SCOPE_RANK = {"global": 0, "domain": 1, "workspace": 2}
_GLOBAL_STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in",
    "is", "it", "of", "on", "or", "the", "to", "with", "work", "task",
}


class SkillManifestError(ValueError):
    """A skill manifest is malformed or attempts to declare authority."""


@dataclass(frozen=True, slots=True)
class SkillScope:
    kind: Literal["global", "domain", "workspace"]
    value: str | None = None

    def __post_init__(self) -> None:
        kind = str(self.kind).strip().casefold()
        if kind not in _SCOPE_RANK:
            raise ValueError("skill_scope_kind_invalid")
        if self.value is not None and not isinstance(self.value, str):
            raise ValueError("skill_scope_value_invalid")
        value = self.value.strip() if self.value is not None else None
        if kind == "global":
            if value:
                raise ValueError("global_skill_scope_must_not_have_value")
            value = None
        elif not value:
            raise ValueError("scoped_skill_scope_value_required")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "value", value)


@dataclass(frozen=True, slots=True)
class SkillDefinition:
    id: str
    name: str
    version: str
    description: str
    scope: SkillScope
    schema: str = "hirda-skill-v1"
    triggers: tuple[str, ...] = ()
    intents: tuple[str, ...] = ()
    required_capabilities: tuple[str, ...] = ()
    required_tools: tuple[str, ...] = ()
    runtime_constraints: tuple[str, ...] = ()
    permission_requirements: tuple[str, ...] = ()
    policy_hints: tuple[str, ...] = ()
    workflow_steps: tuple[str, ...] = ()
    verification_checks: tuple[str, ...] = ()
    provenance: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    metadata: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    source: str = "memory"

    def __post_init__(self) -> None:
        if not isinstance(self.scope, SkillScope):
            raise ValueError("skill_scope_required")
        if not _SKILL_ID.fullmatch(str(self.id or "")):
            raise ValueError("skill_id_invalid")
        for name in ("name", "version", "description"):
            if not str(getattr(self, name) or "").strip():
                raise ValueError(f"{name}_required")
        if self.schema not in {"hirda-skill-v1", "hirda-skill-v2"}:
            raise ValueError("skill_schema_invalid")
        for name in (
            "triggers", "intents", "required_capabilities", "required_tools",
            "runtime_constraints", "permission_requirements", "policy_hints",
            "workflow_steps", "verification_checks",
        ):
            values = tuple(getattr(self, name))
            normalized = [normalize_goal(value) for value in values]
            if any(not isinstance(value, str) or not value.strip() for value in values):
                raise ValueError(f"{name}_contains_empty_or_non_string_item")
            if len(normalized) != len(set(normalized)):
                raise ValueError(f"{name}_contains_duplicate_item")
            object.__setattr__(self, name, tuple(value.strip() for value in values))
        if not isinstance(self.provenance, Mapping):
            raise ValueError("provenance_must_be_mapping")
        if not isinstance(self.metadata, Mapping):
            raise ValueError("metadata_must_be_mapping")
        _check_forbidden_keys(self.provenance)
        _check_forbidden_keys(self.metadata)
        object.__setattr__(self, "provenance", _freeze(self.provenance))
        object.__setattr__(self, "metadata", _freeze(self.metadata))


@dataclass(frozen=True, slots=True)
class SkillMatch:
    skill: SkillDefinition
    explicit: bool
    trigger_matches: tuple[str, ...]
    intent_match: bool
    advisory: bool
    scope_rank: int
    rank: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class SkillResolution:
    matches: tuple[SkillMatch, ...]
    unresolved_explicit_ids: tuple[str, ...]
    required_capabilities: tuple[str, ...]
    required_tools: tuple[str, ...]
    runtime_constraints: tuple[str, ...]
    permission_requirements: tuple[str, ...]
    policy_hints: tuple[str, ...]
    workflow_steps: tuple[str, ...]
    verification_checks: tuple[str, ...]

    @property
    def skills(self) -> tuple[SkillDefinition, ...]:
        return tuple(match.skill for match in self.matches)


def normalize_goal(value: Any) -> str:
    value = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return " ".join("".join(char if char.isalnum() else " " for char in value).split())


def _freeze(
    value: Any,
    memo: dict[int, Any] | None = None,
    active: set[int] | None = None,
) -> Any:
    if memo is None:
        memo = {}
    if active is None:
        active = set()
    is_container = isinstance(value, (Mapping, list, tuple, set, frozenset))
    object_id = id(value)
    if is_container and object_id in memo:
        return memo[object_id]
    if is_container and object_id in active:
        raise SkillManifestError("manifest_recursive_alias_forbidden")
    if is_container:
        active.add(object_id)
    if isinstance(value, Mapping):
        frozen = MappingProxyType({str(key): _freeze(item, memo, active) for key, item in value.items()})
    elif isinstance(value, (list, tuple)):
        frozen = tuple(_freeze(item, memo, active) for item in value)
    elif isinstance(value, (set, frozenset)):
        frozen = frozenset(_freeze(item, memo, active) for item in value)
    else:
        return value
    active.remove(object_id)
    memo[object_id] = frozen
    return frozen


def _normalized_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value).casefold()).strip("_")


def _check_forbidden_keys(value: Any, path: str = "", seen: set[int] | None = None) -> None:
    if seen is None:
        seen = set()
    if isinstance(value, (Mapping, list, tuple, set, frozenset)):
        object_id = id(value)
        if object_id in seen:
            return
        seen.add(object_id)
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = _normalized_key(key)
            key_path = f"{path}.{key}" if path else str(key)
            if normalized in _AUTHORITY_KEYS:
                raise SkillManifestError(f"authority_field_forbidden:{key_path}")
            if normalized in _EXECUTABLE_KEYS:
                raise SkillManifestError(f"executable_field_forbidden:{key_path}")
            if _SECRET_KEY.search(normalized):
                raise SkillManifestError(f"secret_like_field_forbidden:{key_path}")
            _check_forbidden_keys(item, key_path, seen)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for index, item in enumerate(value):
            _check_forbidden_keys(item, f"{path}[{index}]", seen)


def _string_list(value: Any, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise SkillManifestError(f"{field_name}_must_be_string_list")
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise SkillManifestError(f"{field_name}_contains_empty_or_non_string_item")
        text = item.strip()
        key = normalize_goal(text)
        if key in seen:
            raise SkillManifestError(f"{field_name}_contains_duplicate_item")
        seen.add(key)
        result.append(text)
    return tuple(result)


def _parse_manifest(raw: Any, *, source: str) -> SkillDefinition:
    if not isinstance(raw, Mapping):
        raise SkillManifestError("manifest_root_must_be_mapping")
    _check_forbidden_keys(raw)
    unknown = set(raw) - _TOP_LEVEL_KEYS
    if unknown:
        raise SkillManifestError("manifest_field_unknown:" + ",".join(sorted(map(str, unknown))))
    schema = str(raw.get("schema") or "").strip()
    if schema not in {"hirda-skill-v1", "hirda-skill-v2"}:
        raise SkillManifestError("manifest_schema_invalid")
    if schema == "hirda-skill-v1":
        v2_only = {"intents", "permission_requirements", "provenance"} & set(raw)
        if v2_only:
            raise SkillManifestError(
                "manifest_v2_field_requires_v2_schema:"
                + ",".join(sorted(v2_only))
            )
    skill_id = str(raw.get("id") or "").strip()
    if not _SKILL_ID.fullmatch(skill_id):
        raise SkillManifestError("skill_id_invalid")
    for field_name in ("name", "version", "description"):
        if not isinstance(raw.get(field_name), str) or not raw[field_name].strip():
            raise SkillManifestError(f"{field_name}_required")
    scope_raw = raw.get("scope")
    if not isinstance(scope_raw, Mapping) or set(scope_raw) - {"kind", "value"}:
        raise SkillManifestError("skill_scope_invalid")
    normalized_scope_kind = str(scope_raw.get("kind") or "").strip().casefold()
    if normalized_scope_kind == "global":
        scope_kind: Literal["global", "domain", "workspace"] = "global"
    elif normalized_scope_kind == "domain":
        scope_kind = "domain"
    elif normalized_scope_kind == "workspace":
        scope_kind = "workspace"
    else:
        raise SkillManifestError("skill_scope_invalid")
    try:
        scope = SkillScope(scope_kind, scope_raw.get("value"))
    except ValueError as exc:
        raise SkillManifestError(str(exc)) from exc
    provenance = raw.get("provenance") or {}
    metadata = raw.get("metadata") or {}
    if not isinstance(provenance, Mapping):
        raise SkillManifestError("provenance_must_be_mapping")
    if not isinstance(metadata, Mapping):
        raise SkillManifestError("metadata_must_be_mapping")
    return SkillDefinition(
        id=skill_id,
        name=raw["name"].strip(),
        version=raw["version"].strip(),
        description=raw["description"].strip(),
        scope=scope,
        schema=schema,
        triggers=_string_list(raw.get("triggers"), "triggers"),
        intents=_string_list(raw.get("intents"), "intents"),
        required_capabilities=_string_list(raw.get("required_capabilities"), "required_capabilities"),
        required_tools=_string_list(raw.get("required_tools"), "required_tools"),
        runtime_constraints=_string_list(raw.get("runtime_constraints"), "runtime_constraints"),
        permission_requirements=_string_list(
            raw.get("permission_requirements"),
            "permission_requirements",
        ),
        policy_hints=_string_list(raw.get("policy_hints"), "policy_hints"),
        workflow_steps=_string_list(raw.get("workflow_steps"), "workflow_steps"),
        verification_checks=_string_list(raw.get("verification_checks"), "verification_checks"),
        provenance=_freeze(provenance),
        metadata=_freeze(metadata),
        source=source,
    )


def parse_skill_manifest(
    raw: Mapping[str, Any],
    *,
    source: str = "memory",
) -> SkillDefinition:
    """Validate one manifest using the same fail-closed rules as file loading."""
    return _parse_manifest(raw, source=source)


class SkillRegistry:
    def __init__(self) -> None:
        self._skills: dict[tuple[str, str, str], SkillDefinition] = {}

    def register(self, skill: SkillDefinition) -> SkillDefinition:
        key = (skill.id, skill.scope.kind, normalize_goal(skill.scope.value or ""))
        if key in self._skills:
            raise SkillManifestError("skill_variant_duplicate")
        self._skills[key] = skill
        return skill

    def load_directory(
        self,
        directory: str | Path,
        *,
        max_manifest_bytes: int = _MAX_MANIFEST_BYTES,
    ) -> tuple[SkillDefinition, ...]:
        root = Path(directory)
        if root.is_symlink():
            raise SkillManifestError("symlink_directory_forbidden")
        if not root.is_dir():
            raise SkillManifestError("skill_directory_not_found")
        paths = sorted(
            path for path in root.rglob("*")
            if path.suffix.casefold() in {".yaml", ".yml"}
        )
        loaded: list[SkillDefinition] = []
        for path in paths:
            if path.is_symlink():
                raise SkillManifestError("symlink_manifest_forbidden")
            if not path.is_file():
                continue
            try:
                with path.open("rb") as manifest_file:
                    payload = manifest_file.read(max_manifest_bytes + 1)
                if len(payload) > max_manifest_bytes:
                    raise SkillManifestError("manifest_too_large")
                text = payload.decode("utf-8")
                raw = yaml.safe_load(text)
            except SkillManifestError:
                raise
            except (OSError, UnicodeError, yaml.YAMLError) as exc:
                raise SkillManifestError("manifest_parse_failed") from exc
            loaded.append(_parse_manifest(raw, source=str(path)))

        keys = [
            (skill.id, skill.scope.kind, normalize_goal(skill.scope.value or ""))
            for skill in loaded
        ]
        if len(set(keys)) != len(keys) or any(key in self._skills for key in keys):
            raise SkillManifestError("skill_variant_duplicate")
        for skill in loaded:
            self.register(skill)
        return tuple(loaded)

    def snapshot(self) -> tuple[SkillDefinition, ...]:
        return tuple(
            self._skills[key]
            for key in sorted(self._skills)
        )

    def resolve(
        self,
        task_text: str,
        *,
        workspace: str | None = None,
        domain: str | None = None,
        intent: str | None = None,
        explicit_skill_ids: Iterable[str] = (),
        advisory_skill_ids: Iterable[str] = (),
    ) -> SkillResolution:
        text = normalize_goal(task_text)
        intent_key = normalize_goal(intent or "")
        explicit = tuple(
            dict.fromkeys(
                str(value).strip()
                for value in explicit_skill_ids
                if str(value).strip()
            )
        )
        advisory = {
            str(value).strip()
            for value in advisory_skill_ids
            if str(value).strip()
        }
        explicit_set = set(explicit)

        def applicable(skill: SkillDefinition) -> bool:
            if skill.scope.kind == "global":
                return True
            if skill.scope.kind == "workspace":
                return (
                    workspace is not None
                    and normalize_goal(skill.scope.value)
                    == normalize_goal(workspace)
                )
            return (
                domain is not None
                and normalize_goal(skill.scope.value)
                == normalize_goal(domain)
            )

        variants: dict[str, list[SkillDefinition]] = {}
        for skill in self._skills.values():
            if applicable(skill):
                variants.setdefault(skill.id, []).append(skill)
        selected: list[SkillDefinition] = []
        for skills in variants.values():
            selected.append(
                max(
                    skills,
                    key=lambda skill: (
                        _SCOPE_RANK[skill.scope.kind],
                        normalize_goal(skill.scope.value or ""),
                        skill.source,
                    ),
                )
            )

        unresolved = tuple(
            skill_id for skill_id in explicit if skill_id not in variants
        )

        matches: list[SkillMatch] = []
        for skill in selected:
            trigger_matches = tuple(
                trigger
                for trigger in skill.triggers
                if normalize_goal(trigger)
                and _phrase_matches(text, normalize_goal(trigger))
            )
            intent_match = bool(
                intent_key
                and any(
                    normalize_goal(candidate) == intent_key
                    for candidate in skill.intents
                )
            )
            is_explicit = skill.id in explicit_set
            is_advisory = skill.id in advisory
            if (
                skill.triggers
                and not trigger_matches
                and not intent_match
                and not is_explicit
                and not is_advisory
            ):
                continue
            if (
                skill.intents
                and not intent_match
                and not trigger_matches
                and not is_explicit
                and not is_advisory
            ):
                continue
            scope_rank = _SCOPE_RANK[skill.scope.kind]
            rank = (
                int(is_explicit),
                scope_rank,
                int(intent_match),
                int(bool(trigger_matches)),
                len(trigger_matches),
                int(is_advisory),
            )
            matches.append(
                SkillMatch(
                    skill,
                    is_explicit,
                    trigger_matches,
                    intent_match,
                    is_advisory,
                    scope_rank,
                    rank,
                )
            )
        matches.sort(
            key=lambda match: (
                tuple(-value for value in match.rank),
                match.skill.id,
                normalize_goal(match.skill.scope.value or ""),
            )
        )
        return SkillResolution(
            matches=tuple(matches),
            unresolved_explicit_ids=unresolved,
            required_capabilities=_aggregate(matches, "required_capabilities"),
            required_tools=_aggregate(matches, "required_tools"),
            runtime_constraints=_aggregate(matches, "runtime_constraints"),
            permission_requirements=_aggregate(
                matches,
                "permission_requirements",
            ),
            policy_hints=_aggregate(matches, "policy_hints"),
            workflow_steps=_aggregate(matches, "workflow_steps"),
            verification_checks=_aggregate(matches, "verification_checks"),
        )


def _phrase_matches(text: str, phrase: str) -> bool:
    return f" {phrase} " in f" {text} "


def _aggregate(matches: Iterable[SkillMatch], field_name: str) -> tuple[str, ...]:
    output: list[str] = []
    seen: set[str] = set()
    for match in matches:
        for item in getattr(match.skill, field_name):
            key = normalize_goal(item)
            if key not in seen:
                seen.add(key)
                output.append(item)
    return tuple(output)
