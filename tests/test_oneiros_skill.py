from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
from fastapi import HTTPException

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = REPO_ROOT / "skills"
SKILL_ID = "oneiros.motion.shot-regeneration"

_APP_CONFIG_DIR = tempfile.TemporaryDirectory(prefix="oneiros-skill-submit-")
_APP_CONFIG = Path(_APP_CONFIG_DIR.name) / "config.yaml"
_APP_CONFIG.write_text(
    "studio:\n  database: " + str(Path(_APP_CONFIG_DIR.name) / "db.sqlite3") +
    "\n  worker_server_id: oneiros-test-server\nservers:\n  - id: oneiros-test-server\n"
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
from mcp_studio.skill_runtime import SkillRuntime
from mcp_studio.skills import SkillRegistry


@pytest.fixture(scope="module")
def registry() -> SkillRegistry:
    loaded = SkillRegistry()
    skills = loaded.load_directory(SKILLS_ROOT)
    oneiros = [skill for skill in skills if skill.id == SKILL_ID]
    assert len(oneiros) == 1
    return loaded


def _selected(resolution):
    return [match for match in resolution.matches if match.skill.id == SKILL_ID]


def test_real_skills_directory_contains_exactly_one_oneiros_skill(registry):
    skills = [skill for skill in registry.snapshot() if skill.id == SKILL_ID]
    assert len(skills) == 1
    assert skills[0].version == "0.1.0"


def test_english_regenerate_from_here_resolves_expected_triggers(registry):
    resolution = registry.resolve(
        "Please regenerate from here and regenerate only selected shot.",
        workspace="nova-oracle-desktop",
    )
    selected, = _selected(resolution)
    assert "regenerate from here" in selected.trigger_matches
    assert "regenerate only selected shot" in selected.trigger_matches


def test_thai_regeneration_and_anchor_change_intent_resolves(registry):
    resolution = registry.resolve(
        "ช่วย: สร้างช็อตนี้ใหม่; เปลี่ยนจุดอ้างอิงช็อต",
        workspace="nova-oracle-desktop",
    )
    selected, = _selected(resolution)
    assert "สร้างช็อตนี้ใหม่" in selected.trigger_matches
    assert "เปลี่ยนจุดอ้างอิงช็อต" in selected.trigger_matches


def test_skill_does_not_apply_outside_its_workspace(registry):
    resolution = registry.resolve(
        "Regenerate from here", workspace="another-workspace",
    )
    assert _selected(resolution) == []


def test_explanatory_request_does_not_select_regeneration_skill(registry):
    resolution = registry.resolve(
        "Explain how to regenerate shot in Oneiros",
        workspace="nova-oracle-desktop",
    )
    assert _selected(resolution) == []


def test_explicit_skill_in_wrong_workspace_is_unresolved(registry):
    resolution = registry.resolve(
        "Regenerate from here",
        workspace="another-workspace",
        explicit_skill_ids=[SKILL_ID],
    )
    assert _selected(resolution) == []
    assert resolution.unresolved_explicit_ids == (SKILL_ID,)


def test_skill_runtime_serializes_expected_advisory_context(registry):
    runtime = SkillRuntime(enabled=True, path=SKILLS_ROOT, base_dir=REPO_ROOT)
    payload = {
        "label": "Regenerate from here",
        "instruction": "Regenerate only the selected shot",
        "workspace": "nova-oracle-desktop",
        "metadata": {"explicit_skill_ids": [SKILL_ID], "existing": "preserved"},
    }
    enriched = runtime.enrich_work_payload(payload)
    resolution = enriched["metadata"]["skill_resolution"]
    assert runtime.status == "ready"
    assert resolution["selected"][0]["id"] == SKILL_ID
    assert resolution["selected"][0]["scope"] == {"kind": "workspace", "value": "nova-oracle-desktop"}
    assert "oneiros.shot_context.read" in resolution["required_capabilities"]
    assert resolution["workflow_steps"] == [skill_step for skill_step in registry.snapshot()[0].workflow_steps]
    assert resolution["verification_checks"] == list(registry.snapshot()[0].verification_checks)
    assert enriched["metadata"]["existing"] == "preserved"


def _assert_no_forbidden_keys(value):
    forbidden = {
        "model", "models", "runtime", "runtimes", "machine", "machines", "agent", "agents",
        "permission", "permissions", "provider", "router", "routing", "code", "executable",
        "adapter", "command", "commands", "secret", "secrets", "token", "password",
        "credential", "credentials", "authorization", "api_key", "private_key",
    }
    if isinstance(value, dict):
        for key, nested in value.items():
            assert key.casefold() not in forbidden
            _assert_no_forbidden_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            _assert_no_forbidden_keys(nested)


def test_serialized_resolution_contains_no_authority_executable_or_secret_keys():
    runtime = SkillRuntime(enabled=True, path=SKILLS_ROOT, base_dir=REPO_ROOT)
    resolution = runtime.enrich_work_payload({
        "label": "Regenerate from here",
        "workspace": "nova-oracle-desktop",
        "metadata": {},
    })["metadata"]["skill_resolution"]
    _assert_no_forbidden_keys(resolution)


class _FakeDatabase:
    def __init__(self):
        self.created_payload = None

    async def work_summary(self):
        return {"queue": 0}

    async def create_work(self, payload, default_server_id):
        self.created_payload = payload
        return {
            "id": "work-oneiros-test", "label": payload["label"],
            "server_id": default_server_id, "workspace": payload["workspace"],
            "priority": payload["priority"], "dispatch_mode": payload["dispatch_mode"],
        }

    async def add_event(self, *args, **kwargs):
        return None

    async def add_audit(self, *args, **kwargs):
        return None


@pytest.mark.asyncio
async def test_submit_work_resolves_real_skill_before_persistence_without_mutating_payload(monkeypatch):
    runtime = SkillRuntime(enabled=True, path=SKILLS_ROOT, base_dir=REPO_ROOT)
    database = _FakeDatabase()
    monkeypatch.setattr(main, "skill_runtime", runtime)
    monkeypatch.setattr(main, "db", database)
    monkeypatch.setattr(main.scheduler, "kick", lambda: None)
    monkeypatch.setattr(main.execution, "kick", lambda: None)
    original_metadata = {"explicit_skill_ids": [SKILL_ID], "project": "oneiros"}
    payload = WorkSubmit(
        label="Regenerate shot", instruction="Regenerate from here",
        workspace="nova-oracle-desktop",
        server_id=main.settings.studio.worker_server_id,
        dispatch_mode="manual", metadata=original_metadata,
    )

    await main.submit_work(payload)

    assert database.created_payload is not None
    assert database.created_payload["metadata"]["skill_resolution"]["selected"][0]["id"] == SKILL_ID
    assert database.created_payload["metadata"]["project"] == "oneiros"
    assert payload.metadata == original_metadata
    assert "skill_resolution" not in payload.metadata
