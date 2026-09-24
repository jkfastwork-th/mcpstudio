from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mcp_studio.capsules import CapsuleService
from mcp_studio.db import Database, EVENT_REDACTED


@pytest.mark.asyncio
async def test_h3_event_stream_is_ordered_hashed_and_redacted(tmp_path: Path):
    db = Database(str(tmp_path / "studio.sqlite3"))
    await db.init()

    await db.add_event(
        "capsule.handoff_requested",
        "Bearer top-secret-token",
        data={
            "capsule_id": "C-H3",
            "delivery_token": "raw-delivery-token",
            "delivery_token_sha256": "safe-hash",
            "authorization": "Bearer another-secret",
            "correlation": {"handoff_id": "H-H3", "turn_id": "T-H3"},
        },
    )
    await db.add_event(
        "capsule.handoff_dispatched",
        "dispatched",
        data={"capsule_id": "C-H3", "handoff_id": "H-H3"},
    )

    events = await db.list_events(stream_id="capsule:C-H3", ascending=True)
    assert [event["stream_seq"] for event in events] == [1, 2]
    assert events[0]["event_id"].startswith("EV-")
    assert events[1]["prev_event_hash"] == events[0]["event_hash"]
    assert events[0]["correlation_id"] == "H-H3"
    assert events[0]["data"]["delivery_token"] == EVENT_REDACTED
    assert events[0]["data"]["authorization"] == EVENT_REDACTED
    assert events[0]["data"]["delivery_token_sha256"] == "safe-hash"
    assert "top-secret-token" not in events[0]["message"]

    tail = await db.list_events(
        stream_id="capsule:C-H3",
        after_sequence=1,
        ascending=True,
    )
    assert [event["stream_seq"] for event in tail] == [2]

    verification = await db.verify_event_ledger("capsule:C-H3")
    assert verification == {
        "ok": True,
        "checked": 2,
        "streams": 1,
        "errors": [],
    }


@pytest.mark.asyncio
async def test_h3_events_table_rejects_update_and_delete(tmp_path: Path):
    db = Database(str(tmp_path / "studio.sqlite3"))
    await db.init()
    await db.add_event("capsule.created", "created", data={"capsule_id": "C-LOCK"})

    def mutate(sql: str):
        with db._connect() as conn:
            conn.execute(sql)

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        await db._run(lambda: mutate("UPDATE events SET message='changed' WHERE id=1"))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        await db._run(lambda: mutate("DELETE FROM events WHERE id=1"))


@pytest.mark.asyncio
async def test_h3_legacy_events_are_backfilled_into_canonical_stream(tmp_path: Path):
    path = tmp_path / "studio.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute(
            """CREATE TABLE events (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   created_at TEXT NOT NULL,
                   kind TEXT NOT NULL,
                   severity TEXT NOT NULL,
                   server_id TEXT,
                   message TEXT NOT NULL,
                   data_json TEXT NOT NULL DEFAULT '{}'
               )"""
        )
        conn.execute(
            """CREATE TABLE schema_migrations (
                   version INTEGER PRIMARY KEY,
                   name TEXT NOT NULL,
                   applied_at TEXT NOT NULL
               )"""
        )
        conn.execute(
            """INSERT INTO events(
                   created_at, kind, severity, server_id, message, data_json
               ) VALUES(?,?,?,?,?,?)""",
            (
                "2026-09-24T00:00:00+00:00",
                "capsule.created",
                "info",
                None,
                "legacy",
                '{"capsule_id":"C-LEGACY","agent":"claude"}',
            ),
        )
        conn.execute(
            "INSERT INTO schema_migrations(version, name, applied_at) "
            "VALUES(9, 'hirda-lane-state-and-capsule-contract', '2026-09-24T00:00:00+00:00')"
        )

    db = Database(str(path))
    await db.init()

    schema = await db.schema_status()
    assert schema["current_version"] == 10
    assert schema["expected_version"] == 10

    events = await db.list_events(stream_id="capsule:C-LEGACY", ascending=True)
    assert len(events) == 1
    assert events[0]["event_id"] == "EV-LEGACY-0000000000000001"
    assert events[0]["stream_seq"] == 1
    assert events[0]["payload_sha256"]
    assert events[0]["event_hash"]

    verification = await db.verify_event_ledger("capsule:C-LEGACY")
    assert verification["ok"] is True


@pytest.mark.asyncio
async def test_h3_capsule_projection_survives_more_than_500_global_events(tmp_path: Path):
    db = Database(str(tmp_path / "studio.sqlite3"))
    await db.init()
    service = CapsuleService(db)

    await service.create(
        title="Long lived capsule",
        capsule_id="C-OLD",
        agent="claude",
        metadata={
            "context_profile": {
                "source_tokens": 100,
                "retained_tokens": 100,
            }
        },
    )

    for index in range(520):
        await db.add_event(
            "server.noise",
            f"noise-{index}",
            server_id="serena-8001",
            data={"index": index},
        )

    state = await service.get("C-OLD")
    assert state["capsule_id"] == "C-OLD"
    assert state["current_agent"] == "claude"
    assert state["events"][0]["stream_id"] == "capsule:C-OLD"
    assert state["events"][0]["stream_seq"] == 1


@pytest.mark.asyncio
async def test_h3_managed_session_events_use_session_stream(tmp_path: Path):
    db = Database(str(tmp_path / "studio.sqlite3"))
    await db.init()

    await db.add_event(
        "managed.session.ready",
        "ready",
        data={"managed_session_id": "ms-h3", "workspace_key": "alpha"},
    )
    await db.add_event(
        "managed.session.context.report",
        "context",
        data={"managed_session_id": "ms-h3", "context_usage_percent": 42.0},
    )

    events = await db.list_events(
        stream_id="managed-session:ms-h3",
        ascending=True,
    )
    assert [event["stream_seq"] for event in events] == [1, 2]
    assert [event["kind"] for event in events] == [
        "managed.session.ready",
        "managed.session.context.report",
    ]
    assert (await db.verify_event_ledger("managed-session:ms-h3"))["ok"] is True
