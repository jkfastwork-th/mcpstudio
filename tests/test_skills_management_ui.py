from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from mcp_studio import main
from mcp_studio.skill_runtime import SkillRuntime


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
JS = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
CSS = (ROOT / "static" / "styles.css").read_text(encoding="utf-8")
MAIN = (ROOT / "mcp_studio" / "main.py").read_text(encoding="utf-8")

FORBIDDEN_AUTHORITY_KEYS = {
    "model", "models", "runtime", "runtimes", "machine", "machines",
    "agent", "agents", "permission", "permissions", "provider",
    "router", "routing", "code", "command", "commands", "executable",
    "adapter", "secret", "secrets", "token", "tokens", "password",
}


def _write_skill(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "demo.skill.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema": "hirda-skill-v1",
                "id": "demo.review",
                "name": "Demo Review",
                "version": "1.2.3",
                "description": "Review a demo change.",
                "scope": {"kind": "workspace", "value": "demo-workspace"},
                "triggers": ["review demo"],
                "required_capabilities": ["repository.read"],
                "required_tools": [],
                "policy_hints": ["Preserve accepted work."],
                "workflow_steps": ["Inspect the change.", "Review the change."],
                "verification_checks": ["Focused tests pass."],
                "metadata": {"team": "demo"},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def _walk_keys(value):
    if isinstance(value, dict):
        for key, nested in value.items():
            yield str(key).casefold()
            yield from _walk_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk_keys(nested)


@pytest.mark.asyncio
async def test_skills_api_serializes_read_only_registry_without_authority_or_source_path(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    manifest = _write_skill(root)
    runtime = SkillRuntime(enabled=True, path=root, base_dir=tmp_path)
    monkeypatch.setattr(main, "skill_runtime", runtime)

    payload = await main.skills_registry_status()

    assert payload["schema"] == "hirda-skill-registry-status-v1"
    assert payload["enabled"] is True
    assert payload["registry_status"] == "ready"
    assert payload["loaded_skill_count"] == 1
    assert payload["configured_path"] == "skills"
    assert len(payload["skills"]) == 1
    skill = payload["skills"][0]
    assert skill["id"] == "demo.review"
    assert skill["scope"] == {"kind": "workspace", "value": "demo-workspace"}
    assert skill["activation_mode"] == "triggered"
    assert skill["source"] == "manifest"
    serialized = json.dumps(payload)
    assert str(manifest) not in serialized
    assert str(root) not in serialized
    assert not (set(_walk_keys(payload)) & FORBIDDEN_AUTHORITY_KEYS)


@pytest.mark.asyncio
async def test_skills_api_disabled_and_error_states_are_non_throwing(tmp_path, monkeypatch):
    disabled = SkillRuntime(enabled=False, path=tmp_path / "missing", base_dir=tmp_path)
    monkeypatch.setattr(main, "skill_runtime", disabled)
    payload = await main.skills_registry_status()
    assert payload["registry_status"] == "disabled"
    assert payload["skills"] == []
    assert payload["loaded_skill_count"] == 0

    root = tmp_path / "invalid"
    root.mkdir()
    (root / "broken.yaml").write_text("schema: wrong\nid: broken\n", encoding="utf-8")
    broken = SkillRuntime(enabled=True, path=root, base_dir=tmp_path)
    monkeypatch.setattr(main, "skill_runtime", broken)
    payload = await main.skills_registry_status()
    assert payload["registry_status"] == "error"
    assert payload["skills"] == []
    assert payload["error_code"]


def test_skills_is_first_class_read_only_navigation_page():
    assert HTML.count('data-view="skills" href="#skills"') == 2
    assert 'data-page="skills"' in HTML
    for control in ("skillsRefreshBtn", "skillsSearch", "skillsScopeFilter", "skillsList", "skillsDetailPanel"):
        assert f'id="{control}"' in HTML
    assert "Read-only · no execution authority" in HTML
    assert "Nothing on this page installs or runs a skill." in HTML
    assert "install skill" not in HTML.casefold()
    assert "delete skill" not in HTML.casefold()


def test_skills_ui_uses_lazy_read_only_api_and_global_search_route():
    assert "skills:['Skills','Skills'" in JS
    assert "if(view==='skills') setTimeout(refreshSkillsRegistry,0);" in JS
    assert "const data=await getJson('/api/skills');" in JS
    assert JS.count("getJson('/api/skills')") == 1
    assert "[/(skill|skills|registry|manifest)/,'skills']" in JS
    assert "sendJson('/api/skills" not in JS
    assert "fetch('/api/skills" not in JS
    assert '@app.get("/api/skills")' in MAIN
    assert '@app.post("/api/skills")' not in MAIN
    assert '@app.patch("/api/skills")' not in MAIN
    assert '@app.delete("/api/skills")' not in MAIN


def test_skills_page_has_isolated_responsive_styles_and_thai_copy():
    for selector in (
        ".skills-summary-grid", ".skills-browser", ".skills-list-item",
        ".skills-detail-facts", ".skills-authority-notice",
    ):
        assert selector in CSS
    assert "@media(max-width:980px)" in CSS
    assert "@media(max-width:620px)" in CSS
    assert "'Skills':'ทักษะ'" in JS
    assert "'Registry status':'สถานะทะเบียน'" in JS
    assert "'SKILL DETAILS':'รายละเอียดทักษะ'" in JS
    assert "'Search':'ค้นหา'" in JS
