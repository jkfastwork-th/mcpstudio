from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest
from fastapi import HTTPException

_APP_CONFIG_DIR = tempfile.TemporaryDirectory(prefix="hirda-skills-submit-")
_APP_CONFIG = Path(_APP_CONFIG_DIR.name) / "config.yaml"
_APP_CONFIG.write_text(
    "studio:\n  database: " + str(Path(_APP_CONFIG_DIR.name) / "db.sqlite3") +
    "\n  worker_server_id: skills-test-server\nservers:\n  - id: skills-test-server\n"
    "    name: test\n    url: http://127.0.0.1:8001/mcp\n    enabled: true\n",
    encoding="utf-8",
)
_PREVIOUS_CONFIG = os.environ.get("MCP_STUDIO_CONFIG")
os.environ["MCP_STUDIO_CONFIG"] = str(_APP_CONFIG)
from mcp_studio import main
if _PREVIOUS_CONFIG is None:
    os.environ.pop("MCP_STUDIO_CONFIG", None)
else:
    os.environ["MCP_STUDIO_CONFIG"] = _PREVIOUS_CONFIG

from mcp_studio.models import WorkSubmit
from mcp_studio.settings import StudioConfig, load_settings
from mcp_studio.skill_runtime import SkillRequestError, SkillRuntime
from mcp_studio.skills import SkillDefinition, SkillRegistry, SkillScope


def _definition(
    skill_id: str = "review.skill",
    *,
    scope: SkillScope | None = None,
    source: str = "manifest.yaml",
    triggers: tuple[str, ...] = ("review changes",),
    tool: str = "git diff",
) -> SkillDefinition:
    return SkillDefinition(
        id=skill_id,
        name=f"{skill_id} skill",
        version="1.0",
        description="Procedural review guidance",
        scope=scope or SkillScope("global"),
        triggers=triggers,
        required_capabilities=("repo.read",),
        required_tools=(tool,),
        policy_hints=("preserve user changes",),
        workflow_steps=("inspect diff",),
        verification_checks=("run focused tests",),
        metadata={"internal": "must not leak"},
        source=source,
    )


def _manifest(root: Path, *, skill_id: str = "review.skill", scope=None, trigger="review changes") -> Path:
    import yaml

    root.mkdir(parents=True, exist_ok=True)
    path = root / "review.yaml"
    path.write_text(yaml.safe_dump({
        "schema": "hirda-skill-v1",
        "id": skill_id,
        "name": "Review Skill",
        "version": "1",
        "description": "Procedural review guidance",
        "scope": scope or {"kind": "global"},
        "triggers": [trigger],
        "required_capabilities": ["repo.read"],
        "required_tools": ["git diff"],
        "policy_hints": ["preserve user changes"],
        "workflow_steps": ["inspect diff"],
        "verification_checks": ["run focused tests"],
    }), encoding="utf-8")
    return path


def _authority_keys(value):
    forbidden = {
        "model", "runtime", "machine", "agent", "provider", "router", "routing",
        "permission", "permissions", "authority",
    }
    if isinstance(value, dict):
        for key, nested in value.items():
            assert key.casefold() not in forbidden
            _authority_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            _authority_keys(nested)


def test_disabled_runtime_is_a_payload_no_op(tmp_path):
    runtime = SkillRuntime(enabled=False, path="missing", base_dir=tmp_path)
    payload = {"label": "Review", "workspace": "repo", "metadata": {"skill_resolution": {"caller": True}}}
    result = runtime.enrich_work_payload(payload)
    assert runtime.status == "disabled"
    assert result == payload
    assert result is not payload
    assert result["metadata"] is not payload["metadata"]
    assert payload["metadata"]["skill_resolution"] == {"caller": True}


def test_skill_registry_settings_defaults_and_yaml_config(tmp_path):
    defaults = StudioConfig()
    assert defaults.skill_registry_enabled is False
    assert defaults.skill_registry_path == "./skills"
    assert defaults.skill_registry_max_manifest_bytes == 256 * 1024
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "studio:\n  skill_registry_enabled: true\n  skill_registry_path: ./custom-skills\n"
        "  skill_registry_max_manifest_bytes: 1024\nservers:\n"
        "  - id: server\n    name: test\n    url: http://127.0.0.1:8001/mcp\n",
        encoding="utf-8",
    )
    settings = load_settings(config_path)
    assert settings.studio.skill_registry_enabled is True
    assert settings.studio.skill_registry_path == "./custom-skills"
    assert settings.studio.skill_registry_max_manifest_bytes == 1024


def test_enabled_runtime_resolves_scope_precedence_and_unresolved_explicit(tmp_path):
    registry = SkillRegistry()
    registry.register(_definition(scope=SkillScope("global"), source="global.yaml"))
    registry.register(_definition(scope=SkillScope("domain", "engineering"), source="domain.yaml"))
    registry.register(_definition(scope=SkillScope("workspace", "repo-a"), source="workspace.yaml"))
    runtime = SkillRuntime(enabled=True, path=tmp_path, base_dir=tmp_path)
    runtime.registry = registry
    runtime.loaded_skill_count = len(registry.snapshot())
    runtime.status = "ready"
    payload = {
        "label": "Unrelated title",
        "instruction": "  review changes carefully  ",
        "workspace": "repo-a",
        "metadata": {"domain": "engineering", "explicit_skill_ids": ["review.skill", "missing.skill"]},
    }
    result = runtime.enrich_work_payload(payload)
    resolution = result["metadata"]["skill_resolution"]
    assert resolution["schema"] == "hirda-skill-resolution-v1"
    assert resolution["registry_status"] == "ready"
    assert resolution["loaded_skill_count"] == 3
    assert resolution["unresolved_explicit_ids"] == ["missing.skill"]
    assert resolution["selected"] == [{
        "id": "review.skill", "name": "review.skill skill", "version": "1.0",
        "scope": {"kind": "workspace", "value": "repo-a"}, "source": "workspace.yaml",
        "explicit": True, "advisory": False, "trigger_matches": ["review changes"],
    }]
    assert resolution["required_tools"] == ["git diff"]
    assert payload["metadata"]["domain"] == "engineering"


def test_advisory_ranks_only_applicable_skills(tmp_path):
    registry = SkillRegistry()
    registry.register(_definition("ordinary.skill"))
    registry.register(_definition("advised.skill"))
    registry.register(_definition(
        "other.workspace", scope=SkillScope("workspace", "repo-b"), triggers=(), source="other.yaml",
    ))
    runtime = SkillRuntime(enabled=True, path=tmp_path, base_dir=tmp_path)
    runtime.registry = registry
    runtime.status = "ready"
    result = runtime.enrich_work_payload({
        "label": "Review changes", "workspace": "repo-a",
        "metadata": {"advisory_skill_ids": ["advised.skill", "other.workspace"]},
    })["metadata"]["skill_resolution"]
    assert [item["id"] for item in result["selected"]] == ["advised.skill", "ordinary.skill"]
    assert result["selected"][0]["advisory"] is True


def test_metadata_is_preserved_and_result_has_no_authority_fields(tmp_path):
    registry = SkillRegistry()
    registry.register(_definition())
    runtime = SkillRuntime(enabled=True, path=tmp_path, base_dir=tmp_path)
    runtime.registry = registry
    runtime.status = "ready"
    payload = {
        "label": "Review changes", "workspace": "repo", "priority": 99,
        "metadata": {"ticket": "ABC-1", "domain": "engineering", "skill_resolution": {"caller": True}},
    }
    result = runtime.enrich_work_payload(payload)
    resolution = result["metadata"]["skill_resolution"]
    assert result["metadata"]["ticket"] == "ABC-1"
    assert result["metadata"]["domain"] == "engineering"
    assert payload["metadata"]["skill_resolution"] == {"caller": True}
    assert result["priority"] == payload["priority"]
    assert json.loads(json.dumps(resolution)) == resolution
    _authority_keys(resolution)
    serialized = json.dumps(resolution)
    assert "internal" not in serialized
    assert "model" not in serialized.casefold()


def test_enabled_missing_directory_fails_closed_clearly(tmp_path):
    runtime = SkillRuntime(enabled=True, path="missing-skills", base_dir=tmp_path)
    result = runtime.enrich_work_payload({
        "label": "Review changes", "workspace": "repo",
        "metadata": {"explicit_skill_ids": ["requested.skill"]},
    })["metadata"]["skill_resolution"]
    assert runtime.status == "missing"
    assert runtime.registry.snapshot() == ()
    assert result["registry_status"] == "missing"
    assert result["error_code"] == "skill_registry_directory_missing"
    assert result["selected"] == []
    assert result["unresolved_explicit_ids"] == ["requested.skill"]


def test_runtime_can_reload_and_drops_partial_registry_on_failure(tmp_path):
    root = tmp_path / "skills"
    path = _manifest(root)
    runtime = SkillRuntime(enabled=True, path=root, base_dir=tmp_path)
    assert runtime.status == "ready"
    assert runtime.loaded_skill_count == 1
    path.write_text("id: invalid", encoding="utf-8")
    assert runtime.reload() == "error"
    assert runtime.loaded_skill_count == 0
    assert runtime.registry.snapshot() == ()


@pytest.mark.parametrize("metadata", [
    {"domain": "  "},
    {"explicit_skill_ids": [""]},
    {"explicit_skill_ids": ["dup", " dup "]},
    {"advisory_skill_ids": "skill.id"},
])
def test_invalid_skill_selection_fails_closed(tmp_path, metadata):
    runtime = SkillRuntime(enabled=True, path=tmp_path, base_dir=tmp_path)
    with pytest.raises(SkillRequestError, match="skill_request_invalid"):
        runtime.enrich_work_payload({"label": "Review", "workspace": "repo", "metadata": metadata})


class _FakeDatabase:
    def __init__(self, events):
        self.events = events
        self.item = None

    async def work_summary(self):
        return {"queue": 0}

    async def create_work(self, payload, default_server_id):
        self.events.append("create_work")
        self.item = payload
        return {
            "id": "work-test", "label": payload["label"], "server_id": default_server_id,
            "workspace": payload["workspace"], "priority": payload["priority"],
            "dispatch_mode": payload["dispatch_mode"],
        }

    async def add_event(self, *args, **kwargs):
        return None

    async def add_audit(self, *args, **kwargs):
        return None


@pytest.mark.asyncio
async def test_submit_work_persists_resolution_before_scheduler_kick(monkeypatch, tmp_path):
    root = tmp_path / "skills"
    _manifest(root)
    runtime = SkillRuntime(enabled=True, path=root, base_dir=tmp_path)
    events = []
    database = _FakeDatabase(events)
    monkeypatch.setattr(main, "skill_runtime", runtime)
    monkeypatch.setattr(main, "db", database)
    monkeypatch.setattr(main.scheduler, "kick", lambda: events.append("scheduler_kick"))
    monkeypatch.setattr(main.execution, "kick", lambda: events.append("execution_kick"))
    payload = WorkSubmit(
        label="Fallback label", instruction="review changes", workspace="repo",
        server_id=main.settings.studio.worker_server_id, dispatch_mode="manual",
        metadata={"explicit_skill_ids": ["review.skill"], "project": "alpha"},
    )
    await main.submit_work(payload)
    assert events.index("create_work") < events.index("scheduler_kick")
    assert database.item["metadata"]["skill_resolution"]["selected"][0]["id"] == "review.skill"
    assert database.item["metadata"]["project"] == "alpha"
    assert "skill_resolution" not in payload.metadata


@pytest.mark.asyncio
async def test_submit_work_maps_invalid_selection_to_http_400(monkeypatch):
    events = []
    monkeypatch.setattr(main, "skill_runtime", SkillRuntime(enabled=True, path="missing"))
    monkeypatch.setattr(main, "db", _FakeDatabase(events))
    payload = WorkSubmit(
        label="Review", workspace="repo", server_id=main.settings.studio.worker_server_id,
        metadata={"explicit_skill_ids": [""]},
    )
    with pytest.raises(HTTPException) as exc:
        await main.submit_work(payload)
    assert exc.value.status_code == 400
    assert exc.value.detail == "skill_request_invalid"
    assert events == []
