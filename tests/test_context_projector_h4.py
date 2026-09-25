from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcp_studio.capsules import CapsuleService
from mcp_studio.context_projector import ContextProjector
from mcp_studio.db import (
    Database,
    EVENT_PRIVATE_REASONING_OMITTED,
    EVENT_REDACTED,
)


def contract() -> dict:
    return {
        "objective": "Continue the implementation without semantic drift.",
        "locked_decisions": ["Keep the public API stable."],
        "artifacts": ["mcp_studio/capsules.py"],
        "current_state": {"tests": "green"},
        "checkpoint": {"git": "clean"},
        "next_actions": ["Continue H4."],
        "acceptance_criteria": ["Projection remains deterministic."],
        "handoff_checks": ["baseline tests pass"],
        "constraints": ["Do not persist private reasoning."],
        "portability": "safe",
        "target_capabilities": {},
    }


async def prepared(tmp_path: Path, capsule_id: str = "C-H4"):
    db = Database(str(tmp_path / "studio.sqlite3"))
    await db.init()
    service = CapsuleService(db)
    await service.create(
        title="H4 projector",
        capsule_id=capsule_id,
        agent="claude",
        metadata={
            "contract": contract(),
            "context_profile": {
                "source_tokens": 1000,
                "retained_tokens": 900,
            },
        },
    )
    return db, service, ContextProjector(db)


@pytest.mark.asyncio
async def test_h4_replay_is_deterministic(tmp_path: Path):
    db, service, projector = await prepared(tmp_path)
    await service.set_stage("C-H4", stage="implementation", agent="claude")

    first = await projector.project_capsule("C-H4", use_checkpoint=False)
    second = await projector.project_capsule("C-H4", use_checkpoint=False)

    assert first["projection_sha256"] == second["projection_sha256"]
    assert first["context"] == second["context"]
    assert first["recent_events"] == second["recent_events"]
    assert first["context"]["objective"] == contract()["objective"]
    assert first["context"]["locked_decisions"] == ["Keep the public API stable."]
    assert first["context"]["runtime"]["current_stage"] == "implementation"



@pytest.mark.asyncio
async def test_h4_compact_resume_matches_full_replay(tmp_path: Path):
    db, service, projector = await prepared(tmp_path)
    await service.set_stage("C-H4", stage="implementation", agent="claude")

    compacted = await projector.compact_capsule("C-H4")
    assert compacted["checkpoint"]["used"] is True

    full = await projector.project_capsule("C-H4", use_checkpoint=False)
    resumed = await projector.project_capsule("C-H4", use_checkpoint=True)

    assert full["projection_sha256"] == resumed["projection_sha256"]
    assert full["context"] == resumed["context"]
    assert full["recent_events"] == resumed["recent_events"]

    await service.set_stage("C-H4", stage="review", agent="claude")
    full_after = await projector.project_capsule("C-H4", use_checkpoint=False)
    resumed_after = await projector.project_capsule("C-H4", use_checkpoint=True)

    assert full_after["projection_sha256"] == resumed_after["projection_sha256"]
    assert resumed_after["context"]["locked_decisions"] == [
        "Keep the public API stable."
    ]
    assert resumed_after["context"]["runtime"]["current_stage"] == "review"



@pytest.mark.asyncio
async def test_h4_capsule_streams_do_not_mix(tmp_path: Path):
    db = Database(str(tmp_path / "studio.sqlite3"))
    await db.init()
    service = CapsuleService(db)

    await service.create(title="A", capsule_id="C-A", agent="claude")
    await service.create(title="B", capsule_id="C-B", agent="hermes")
    await service.set_stage("C-A", stage="alpha", agent="claude")
    await service.set_stage("C-B", stage="beta", agent="hermes")

    projector = ContextProjector(db)
    a = await projector.project_capsule("C-A")
    b = await projector.project_capsule("C-B")

    assert a["context"]["runtime"]["current_stage"] == "alpha"
    assert b["context"]["runtime"]["current_stage"] == "beta"
    assert all(event["data"]["capsule_id"] == "C-A" for event in a["recent_events"])
    assert all(event["data"]["capsule_id"] == "C-B" for event in b["recent_events"])


@pytest.mark.asyncio
async def test_h4_preserves_handoff_and_completion_correlation(tmp_path: Path):
    db, _, projector = await prepared(tmp_path)
    correlation = {
        "schema": "hirda-handoff-correlation-v1",
        "handoff_id": "H-H4",
        "capsule_id": "C-H4",
        "turn_id": "T-H4",
        "source_session": "pane-claude",
        "target_session": "pane-hermes",
        "generation": "G-H4",
        "from_agent": "claude",
        "to_agent": "hermes",
    }
    await db.add_event(
        "capsule.handoff_requested",
        "requested",
        data={
            "capsule_id": "C-H4",
            "handoff_id": "H-H4",
            "from_agent": "claude",
            "to_agent": "hermes",
            "from_stage": "implementation",
            "to_stage": "implementation",
            "correlation": correlation,
        },
    )
    await db.add_event(
        "capsule.handoff_committed",
        "committed",
        data={
            "capsule_id": "C-H4",
            "handoff_id": "H-H4",
            "from_agent": "claude",
            "to_agent": "hermes",
            "from_stage": "implementation",
            "to_stage": "implementation",
            "correlation": correlation,
        },
    )
    await db.add_event(
        "capsule.handoff_completed",
        "completed",
        data={
            "capsule_id": "C-H4",
            "handoff_id": "H-H4",
            "from_agent": "claude",
            "to_agent": "hermes",
            "correlation": correlation,
            "sentinel_verified": True,
        },
    )

    projection = await projector.project_capsule("C-H4")
    assert projection["context"]["handoff"]["state"] == "committed"
    assert projection["context"]["handoff"]["correlation"] == correlation
    assert projection["context"]["completion"] == {
        "handoff_id": "H-H4",
        "state": "completed",
        "sentinel_verified": True,
        "correlation": correlation,
    }


@pytest.mark.asyncio
async def test_h4_private_reasoning_never_reaches_projection(tmp_path: Path):
    db = Database(str(tmp_path / "studio.sqlite3"))
    await db.init()
    await db.add_event(
        "capsule.created",
        "created",
        data={
            "capsule_id": "C-PRIVATE",
            "agent": "claude",
            "metadata": {
                "hidden_reasoning": "never persist this thought",
                "scratchpad": "private scratch",
                "api_key": "sk-super-secret-value",
                "safe_note": "observable note",
            },
        },
    )

    events = await db.list_events(stream_id="capsule:C-PRIVATE", ascending=True)
    metadata = events[0]["data"]["metadata"]
    assert metadata["hidden_reasoning"] == EVENT_PRIVATE_REASONING_OMITTED
    assert metadata["scratchpad"] == EVENT_PRIVATE_REASONING_OMITTED
    assert metadata["api_key"] == EVENT_REDACTED
    assert metadata["safe_note"] == "observable note"

    projection = await ContextProjector(db).project_capsule("C-PRIVATE")
    encoded = json.dumps(projection, ensure_ascii=False)
    assert "never persist this thought" not in encoded
    assert "private scratch" not in encoded
    assert "sk-super-secret-value" not in encoded
    assert EVENT_PRIVATE_REASONING_OMITTED in encoded
    assert EVENT_REDACTED in encoded
