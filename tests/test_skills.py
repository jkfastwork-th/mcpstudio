from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from mcp_studio.skills import (
    SkillDefinition,
    SkillManifestError,
    SkillRegistry,
    SkillScope,
    normalize_goal,
)


def manifest(**overrides):
    value = {
        "schema": "hirda-skill-v1",
        "id": "engineering.review",
        "name": "Engineering Review",
        "version": "1.0.0",
        "description": "Review software changes.",
        "scope": {"kind": "global"},
        "triggers": ["review changes"],
        "required_capabilities": ["read repository"],
        "required_tools": ["git diff"],
        "policy_hints": ["preserve user changes"],
        "workflow_steps": ["inspect", "review"],
        "verification_checks": ["run focused tests"],
        "metadata": {"team": "engineering"},
    }
    value.update(overrides)
    return value


def write_manifest(directory: Path, value, *, filename="skill.yaml") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    import yaml

    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")
    return path


def test_normalize_goal_is_case_whitespace_and_punctuation_stable():
    assert normalize_goal("  Review,   The CHANGE!!! ") == "review the change"
    assert normalize_goal("foo_bar") == normalize_goal("foo-bar")


def test_load_directory_validates_and_freezes_skill_definition(tmp_path):
    write_manifest(tmp_path, manifest())
    skill, = SkillRegistry().load_directory(tmp_path)
    assert skill.scope == SkillScope("global")
    assert skill.triggers == ("review changes",)
    assert skill.source.endswith("skill.yaml")
    with pytest.raises(FrozenInstanceError):
        skill.name = "Changed"
    with pytest.raises(TypeError):
        skill.metadata["team"] = "changed"


@pytest.mark.parametrize("field,value", [
    ("runtime", "model-x"),
    ("permissions", ["admin"]),
    ("metadata", {"provider": "remote"}),
    ("metadata", {"nested": {"agents": ["worker"]}}),
    ("metadata", {"command": "echo unsafe"}),
    ("metadata", {"api_token": "inline-secret"}),
])
def test_rejects_authority_executable_and_secret_like_fields(tmp_path, field, value):
    payload = manifest(**{field: value})
    write_manifest(tmp_path, payload)
    with pytest.raises(SkillManifestError):
        SkillRegistry().load_directory(tmp_path)


@pytest.mark.parametrize("field", [
    "model", "models", "runtime", "runtimes", "machine", "machines", "agent",
    "agents", "permission", "permissions", "provider", "router", "routing",
    "code", "executable", "adapter", "command", "commands", "script", "scripts",
])
def test_rejects_every_forbidden_key_family_recursively(tmp_path, field):
    write_manifest(tmp_path, manifest(metadata={"nested": {field: "forbidden"}}))
    with pytest.raises(SkillManifestError):
        SkillRegistry().load_directory(tmp_path)


@pytest.mark.parametrize("values", [["", "read"], ["read", " read "], ["Read", "read"]])
def test_rejects_empty_and_duplicate_string_lists(tmp_path, values):
    write_manifest(tmp_path, manifest(required_tools=values))
    with pytest.raises(SkillManifestError, match="required_tools"):
        SkillRegistry().load_directory(tmp_path)


def test_rejects_bad_id_scope_and_non_mapping_root(tmp_path):
    write_manifest(tmp_path, manifest(id="../outside"))
    with pytest.raises(SkillManifestError, match="skill_id_invalid"):
        SkillRegistry().load_directory(tmp_path)

    (tmp_path / "skill.yaml").write_text("- not a mapping\n", encoding="utf-8")
    with pytest.raises(SkillManifestError, match="manifest_root_must_be_mapping"):
        SkillRegistry().load_directory(tmp_path)

    write_manifest(tmp_path, manifest(scope={"kind": "workspace"}))
    with pytest.raises(SkillManifestError, match="scope_value_required"):
        SkillRegistry().load_directory(tmp_path)


def test_rejects_symlink_manifest_and_oversized_manifest(tmp_path):
    target = write_manifest(tmp_path / "source", manifest())
    link_dir = tmp_path / "links"
    link_dir.mkdir()
    link = link_dir / "linked.yaml"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available")
    with pytest.raises(SkillManifestError, match="symlink_manifest_forbidden"):
        SkillRegistry().load_directory(link_dir)

    oversized_dir = tmp_path / "oversized"
    oversized_dir.mkdir()
    (oversized_dir / "large.yaml").write_text("x" * 20, encoding="utf-8")
    with pytest.raises(SkillManifestError, match="manifest_too_large"):
        SkillRegistry().load_directory(oversized_dir, max_manifest_bytes=10)


def test_scope_specific_variant_shadows_less_specific_variant(tmp_path):
    registry = SkillRegistry()
    write_manifest(tmp_path, manifest(scope={"kind": "global"}), filename="global.yaml")
    write_manifest(tmp_path, manifest(scope={"kind": "domain", "value": "coding"}), filename="domain.yaml")
    write_manifest(tmp_path, manifest(scope={"kind": "workspace", "value": "repo-a"}), filename="workspace.yaml")
    registry.load_directory(tmp_path)

    result = registry.resolve("review changes", workspace="repo-a", domain="coding")
    assert len(result.matches) == 1
    assert result.matches[0].skill.scope == SkillScope("workspace", "repo-a")

    domain_result = registry.resolve("review changes", workspace="repo-b", domain="coding")
    assert domain_result.matches[0].skill.scope == SkillScope("domain", "coding")


def test_explicit_precedence_and_nonapplicable_explicit_ids_are_reported(tmp_path):
    registry = SkillRegistry()
    write_manifest(tmp_path, manifest(id="global-review"), filename="global.yaml")
    write_manifest(tmp_path, manifest(
        id="workspace-review", scope={"kind": "workspace", "value": "repo-a"},
    ), filename="workspace.yaml")
    registry.load_directory(tmp_path)

    result = registry.resolve(
        "ordinary task", workspace="repo-a", explicit_skill_ids=["global-review"],
    )
    assert [match.skill.id for match in result.matches][:1] == ["global-review"]
    missing = registry.resolve(
        "ordinary task", workspace="repo-b", explicit_skill_ids=["workspace-review", "unknown"],
    )
    assert missing.unresolved_explicit_ids == ("workspace-review", "unknown")


def test_trigger_matching_is_deterministic_and_advisory_only_reorders_applicable_skills(tmp_path):
    registry = SkillRegistry()
    write_manifest(tmp_path, manifest(id="general", triggers=[]), filename="general.yaml")
    write_manifest(tmp_path, manifest(id="db-review", triggers=["database review"]), filename="db.yaml")
    write_manifest(tmp_path, manifest(
        id="workspace-general", scope={"kind": "workspace", "value": "repo-a"}, triggers=[],
    ), filename="workspace.yaml")
    registry.load_directory(tmp_path)

    first = registry.resolve("Please do a database review", workspace="repo-a")
    second = registry.resolve("Please do a database review", workspace="repo-a")
    assert [item.skill.id for item in first.matches] == [item.skill.id for item in second.matches]
    assert first.matches[0].skill.id == "workspace-general"
    assert next(item for item in first.matches if item.skill.id == "db-review").trigger_matches == ("database review",)

    advised = registry.resolve(
        "Please do a database review", workspace="repo-a", advisory_skill_ids=["db-review", "not-loaded"],
    )
    # A lower-scope advisory cannot overtake an otherwise applicable workspace skill.
    assert advised.matches[0].skill.id == "workspace-general"
    assert all(item.skill.id != "not-loaded" for item in advised.matches)


def test_advisory_boosts_an_applicable_skill_but_does_not_add_one(tmp_path):
    registry = SkillRegistry()
    write_manifest(tmp_path, manifest(id="alpha", triggers=[]), filename="alpha.yaml")
    write_manifest(tmp_path, manifest(id="beta", triggers=[]), filename="beta.yaml")
    write_manifest(tmp_path, manifest(
        id="workspace-only", scope={"kind": "workspace", "value": "elsewhere"}, triggers=[],
    ), filename="workspace.yaml")
    registry.load_directory(tmp_path)

    plain = registry.resolve("unmatched", workspace="repo-a")
    advised = registry.resolve("unmatched", workspace="repo-a", advisory_skill_ids=["beta", "workspace-only"])
    assert [match.skill.id for match in plain.matches] == ["alpha", "beta"]
    assert [match.skill.id for match in advised.matches] == ["beta", "alpha"]
    assert all(match.skill.id != "workspace-only" for match in advised.matches)


def test_aggregates_unique_capabilities_tools_hints_and_steps_stably(tmp_path):
    registry = SkillRegistry()
    write_manifest(tmp_path, manifest(
        id="alpha", required_capabilities=["read", "write"], required_tools=["git", "tests"],
        policy_hints=["preserve state"], workflow_steps=["inspect", "change"],
        verification_checks=["tests pass"],
    ), filename="alpha.yaml")
    write_manifest(tmp_path, manifest(
        id="beta", required_capabilities=["write", "review"], required_tools=["tests", "lint"],
        policy_hints=["preserve state", "ask first"], workflow_steps=["change", "verify"],
        verification_checks=["tests pass", "lint pass"],
    ), filename="beta.yaml")
    registry.load_directory(tmp_path)
    result = registry.resolve("unmatched request")

    assert result.required_capabilities == ("read", "write", "review")
    assert result.required_tools == ("git", "tests", "lint")
    assert result.policy_hints == ("preserve state", "ask first")
    assert result.workflow_steps == ("inspect", "change", "verify")
    assert result.verification_checks == ("tests pass", "lint pass")


def test_register_rejects_duplicate_scope_variant():
    skill = SkillDefinition(
        id="review", name="Review", version="1", description="Review changes.", scope=SkillScope("global"),
    )
    registry = SkillRegistry()
    registry.register(skill)
    with pytest.raises(SkillManifestError, match="variant_duplicate"):
        registry.register(skill)
