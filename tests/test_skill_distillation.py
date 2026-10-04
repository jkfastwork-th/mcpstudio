import json
from types import SimpleNamespace

import pytest
import yaml

from mcp_studio.db import Database
from mcp_studio.skill_distillation import SkillDistillationManager
from mcp_studio.skill_runtime import SkillRuntime


class _Resolver:
    def resolve_skill_metadata(self, metadata):
        return {
            "schema": "hirda-capability-resolution-v1",
            "resolved": True,
            "status": "ready",
            "unresolved_capabilities": [],
            "unresolved_tools": [],
        }


class _HermesRouter:
    def __init__(self, *, runtime="hermes"):
        self.runtime = runtime
        self.requests = []

    async def execute(self, payload):
        self.requests.append(payload)
        output = {
            "id": "social.publish",
            "name": "Social Publish",
            "description": "Publish through governed Social Hub task ingress.",
            "scope": {"kind": "global"},
            "triggers": ["publish this on facebook"],
            "intents": ["publish_social_post"],
            "required_capabilities": ["social_hub_task_ingress"],
            "required_tools": ["social_task_create"],
            "runtime_constraints": ["tool_use"],
            "permission_requirements": ["social.publish"],
            "policy_hints": ["preserve owner identity"],
            "workflow_steps": ["validate identity", "create task", "verify"],
            "verification_checks": ["task provenance recorded"],
            "metadata": {"domain": "social"},
        }
        return SimpleNamespace(
            schema="hirda-cognitive-result-v1",
            request_id="distill-1",
            status="success",
            runtime=self.runtime,
            model="test-hermes-model",
            lane="deep",
            fallback_used=False,
            output=json.dumps(output),
            error=None,
        )


class _HermesControlCharRouter(_HermesRouter):
    async def execute(self, payload):
        result = await super().execute(payload)
        result.output = result.output.replace(
            "Publish through governed Social Hub task ingress.",
            "Publish through governed Social Hub task ingress.\nKeep review before external publish.",
        )
        return result


class _HermesDescriptiveDependencyRouter(_HermesRouter):
    async def execute(self, payload):
        result = await super().execute(payload)
        decoded = json.loads(result.output)
        decoded["required_capabilities"] = [
            "Create a social publishing task through governed ingress"
        ]
        decoded["required_tools"] = ["Social task ingress"]
        result.output = json.dumps(decoded)
        return result


def _seed_manifest():
    return {
        "schema": "hirda-skill-v2",
        "id": "social.publish",
        "name": "Social Publish",
        "version": "0.2.0",
        "description": "Seed social publishing skill.",
        "scope": {"kind": "global"},
        "triggers": ["publish this on facebook"],
        "intents": ["publish_social_post"],
        "required_capabilities": ["social_hub_task_ingress"],
        "required_tools": ["social_task_create"],
        "runtime_constraints": ["tool_use"],
        "permission_requirements": ["social.publish"],
        "policy_hints": ["review before publish"],
        "workflow_steps": ["create task"],
        "verification_checks": ["task created"],
        "provenance": {
            "origin": "seed",
            "distillation_status": "certified_seed",
        },
        "metadata": {"domain": "social"},
    }


async def _record_experience(db, index, *, score=0.95):
    return await db.record_skill_experience(
        {
            "invocation_id": f"inv-{index}",
            "work_id": f"work-{index}",
            "pstack_task_id": f"TASK-{index}",
            "skill_ids": ["social.publish"],
            "domain": "social",
            "intent": "publish_social_post",
            "input": {
                "channel": "facebook",
                "account_id": f"private-account-{index}",
            },
            "identity": {"account_id": f"private-account-{index}"},
            "channel": "facebook",
            "outcome": "quality_pass",
            "execution_certification": {
                "passed": True,
                "outcome": "PASS",
            },
            "quality_certification": {
                "passed": True,
                "outcome": "PASS",
                "score": score,
            },
            "trace": {
                "workflow_steps": ["validate identity", "create task", "verify"],
                "verification_checks": ["task provenance recorded"],
                "selected_provider_ids": ["social-hub-task"],
            },
        }
    )


@pytest.mark.asyncio
async def test_hermes_distills_certifies_and_promotes_skill_evolution(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    (root / "social.publish.yaml").write_text(
        yaml.safe_dump(_seed_manifest(), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    db = Database(str(tmp_path / "studio.sqlite3"))
    await db.init()
    for index in range(3):
        await _record_experience(db, index)

    runtime = SkillRuntime(enabled=True, path=root, base_dir=tmp_path)
    router = _HermesRouter()
    manager = SkillDistillationManager(
        db,
        runtime,
        _Resolver(),
        router,
        min_experiences=3,
        quality_threshold=0.8,
    )

    result = await manager.distill(
        domain="social",
        intent="publish_social_post",
    )
    candidate = result["candidate"]

    assert result["status"] == "candidate_created"
    assert candidate["operation"] == "update"
    assert candidate["skill_id"] == "social.publish"
    assert candidate["manifest"]["version"] == "0.2.1"
    assert candidate["distiller_runtime"] == "hermes"
    request = router.requests[0]
    assert request["preferred_runtime"] == "hermes"
    assert request["principal_id"] == "hirda-skill-distiller"
    assert request["allow_fallback"] is False
    assert "private-account-" not in request["prompt"]
    assert "social_hub_task_ingress" in request["prompt"]
    assert "social_task_create" in request["prompt"]

    certified = await manager.certify_candidate(candidate["id"])
    assert certified["status"] == "certified"
    assert certified["certification"]["outcome"] == "PASS"

    promoted = await manager.promote_candidate(candidate["id"])
    assert promoted["status"] == "promoted"
    manifest = yaml.safe_load(
        (root / "social.publish.yaml").read_text(encoding="utf-8")
    )
    assert manifest["version"] == "0.2.1"
    assert manifest["provenance"]["origin"] == "hermes_distillation"
    assert any(
        skill.id == "social.publish" and skill.version == "0.2.1"
        for skill in runtime.registry.snapshot()
    )


@pytest.mark.asyncio
async def test_distillation_recovers_raw_control_characters_in_hermes_json(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    (root / "social.publish.yaml").write_text(
        yaml.safe_dump(_seed_manifest(), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    db = Database(str(tmp_path / "studio.sqlite3"))
    await db.init()
    for index in range(3):
        await _record_experience(db, index)

    runtime = SkillRuntime(enabled=True, path=root, base_dir=tmp_path)
    manager = SkillDistillationManager(
        db,
        runtime,
        _Resolver(),
        _HermesControlCharRouter(),
        min_experiences=3,
        quality_threshold=0.8,
    )

    result = await manager.distill(
        domain="social",
        intent="publish_social_post",
    )

    assert result["status"] == "candidate_created"
    assert result["candidate"]["manifest"]["description"].endswith(
        "Keep review before external publish."
    )


@pytest.mark.asyncio
async def test_skill_evolution_preserves_existing_canonical_dependency_ids(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    (root / "social.publish.yaml").write_text(
        yaml.safe_dump(_seed_manifest(), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    db = Database(str(tmp_path / "studio.sqlite3"))
    await db.init()
    for index in range(3):
        await _record_experience(db, index)

    runtime = SkillRuntime(enabled=True, path=root, base_dir=tmp_path)
    router = _HermesDescriptiveDependencyRouter()
    manager = SkillDistillationManager(
        db,
        runtime,
        _Resolver(),
        router,
        min_experiences=3,
        quality_threshold=0.8,
    )

    result = await manager.distill(
        domain="social",
        intent="publish_social_post",
    )
    manifest = result["candidate"]["manifest"]

    assert manifest["required_capabilities"] == ["social_hub_task_ingress"]
    assert manifest["required_tools"] == ["social_task_create"]
    assert "social_hub_task_ingress" in router.requests[0]["prompt"]
    assert "social_task_create" in router.requests[0]["prompt"]


@pytest.mark.asyncio
async def test_rejected_candidate_does_not_block_distillation_retry(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    (root / "social.publish.yaml").write_text(
        yaml.safe_dump(_seed_manifest(), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    db = Database(str(tmp_path / "studio.sqlite3"))
    await db.init()
    for index in range(3):
        await _record_experience(db, index)

    runtime = SkillRuntime(enabled=True, path=root, base_dir=tmp_path)
    manager = SkillDistillationManager(
        db,
        runtime,
        _Resolver(),
        _HermesRouter(),
        min_experiences=3,
        quality_threshold=0.8,
    )

    first = await manager.distill(domain="social", intent="publish_social_post")
    await db.update_skill_candidate(first["candidate"]["id"], status="rejected")

    second = await manager.distill(domain="social", intent="publish_social_post")

    assert second["status"] == "candidate_created"
    assert second["candidate"]["id"] != first["candidate"]["id"]


@pytest.mark.asyncio
async def test_distillation_requires_enough_high_quality_experience(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    (root / "social.publish.yaml").write_text(
        yaml.safe_dump(_seed_manifest(), sort_keys=False),
        encoding="utf-8",
    )
    db = Database(str(tmp_path / "studio.sqlite3"))
    await db.init()
    await _record_experience(db, 1, score=0.95)
    await _record_experience(db, 2, score=0.50)
    manager = SkillDistillationManager(
        db,
        SkillRuntime(enabled=True, path=root, base_dir=tmp_path),
        _Resolver(),
        _HermesRouter(),
        min_experiences=3,
        quality_threshold=0.8,
    )

    result = await manager.distill(
        domain="social",
        intent="publish_social_post",
    )

    assert result["status"] == "not_enough_experience"
    assert result["eligible"] == 1
    assert await db.list_skill_candidates() == []


@pytest.mark.asyncio
async def test_distillation_fails_closed_if_non_hermes_runtime_answers(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    (root / "social.publish.yaml").write_text(
        yaml.safe_dump(_seed_manifest(), sort_keys=False),
        encoding="utf-8",
    )
    db = Database(str(tmp_path / "studio.sqlite3"))
    await db.init()
    for index in range(3):
        await _record_experience(db, index)
    manager = SkillDistillationManager(
        db,
        SkillRuntime(enabled=True, path=root, base_dir=tmp_path),
        _Resolver(),
        _HermesRouter(runtime="claude"),
        min_experiences=3,
    )

    with pytest.raises(RuntimeError, match="runtime_must_be_hermes"):
        await manager.distill(
            domain="social",
            intent="publish_social_post",
        )
