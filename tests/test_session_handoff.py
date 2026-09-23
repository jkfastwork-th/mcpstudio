from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcp_studio.db import Database
from mcp_studio.gateway import GatewaySessionManager
from mcp_studio.managed_sessions import ManagedSessionError
from mcp_studio.settings import ServerConfig, Settings, StudioConfig
from mcp_studio.tool_permissions import classify_tool


def settings_for(tmp_path: Path) -> Settings:
    project_root = tmp_path / "projects"
    project_root.mkdir()
    return Settings(
        studio=StudioConfig(
            database=str(tmp_path / "studio.sqlite3"),
            gateway_enabled=True,
            gateway_allowed_servers=["serena-8001"],
            worker_server_id="serena-8001",
            managed_session_enabled=True,
            managed_session_workspace_roots=[str(project_root)],
            managed_session_port_start=43110,
            managed_session_port_end=43120,
        ),
        servers=[
            ServerConfig(
                id="serena-8001",
                name="Serena",
                url="http://127.0.0.1:8001/mcp",
            )
        ],
        tunnels=[],
        config_path=tmp_path / "config.yaml",
    )


class Pool:
    enabled = True

    def __init__(self, db: Database, managed_session_id: str):
        self.db = db
        self.managed_session_id = managed_session_id

    async def ensure_running(self, session_id: str):
        if session_id != self.managed_session_id:
            raise KeyError(session_id)
        return await self.db.get_managed_session(session_id)

    async def get_session(self, session_id: str):
        return await self.ensure_running(session_id)


async def setup_pair(tmp_path: Path):
    settings = settings_for(tmp_path)
    db = Database(settings.studio.database)
    await db.init()

    project = tmp_path / "projects" / "alpha"
    project.mkdir()
    await db.upsert_managed_workspace(
        key="alpha",
        name="Alpha",
        project_path=str(project.resolve()),
    )
    managed = await db.create_managed_session(
        name="Alpha",
        workspace_key="alpha",
        project_path=str(project.resolve()),
        server_id="serena-8001",
        port=43110,
    )
    managed = await db.update_managed_session_runtime(
        managed["id"],
        status="ready",
        pid=123,
        endpoint="http://127.0.0.1:43110/mcp",
    )

    source_studio = await db.create_session(
        {"client_id": "conversation-source", "client_type": "chatgpt", "server_id": "serena-8001"}
    )
    target_studio = await db.create_session(
        {"client_id": "conversation-target", "client_type": "chatgpt", "server_id": "serena-8001"}
    )
    source_gateway = await db.create_gateway_session(
        studio_session_id=source_studio["id"],
        client_id="conversation-source",
        client_type="chatgpt",
        server_id="serena-8001",
        upstream_session_id="source-managed-up",
        protocol_version="2025-11-25",
        init_payload={},
        upstream_url="http://127.0.0.1:43110/mcp",
        managed_session_id=managed["id"],
    )
    await db.bind_gateway_managed_session(
        source_gateway["id"],
        managed_session_id=managed["id"],
        upstream_url="http://127.0.0.1:43110/mcp",
        upstream_session_id="source-managed-up",
    )
    target_gateway = await db.create_gateway_session(
        studio_session_id=target_studio["id"],
        client_id="conversation-target",
        client_type="chatgpt",
        server_id="serena-8001",
        upstream_session_id="target-base-up",
        protocol_version="2025-11-25",
        init_payload={},
        upstream_url="http://127.0.0.1:8001/mcp",
    )
    manager = GatewaySessionManager(
        settings,
        db,
        managed_sessions=Pool(db, managed["id"]),
    )
    return settings, db, manager, managed, source_gateway, target_gateway, source_studio, target_studio


def test_session_handoff_tools_are_exposed_and_classified(tmp_path: Path):
    settings = settings_for(tmp_path)
    manager = GatewaySessionManager(
        settings,
        Database(settings.studio.database),
        managed_sessions=type("Pool", (), {"enabled": True})(),
    )
    names = {tool["name"] for tool in manager._management_tools()}
    assert "mcpstudio_handoff_session" in names
    assert "mcpstudio_accept_handoff" in names
    assert "mcpstudio_context_status" in names
    assert "mcpstudio_report_context_usage" in names
    assert classify_tool("mcpstudio_handoff_session", {}) == "execute"
    assert classify_tool("mcpstudio_accept_handoff", {}) == "execute"
    assert classify_tool("mcpstudio_context_status", {}) == "read"
    assert classify_tool("mcpstudio_report_context_usage", {}) == "execute"


@pytest.mark.asyncio
async def test_context_usage_reporting_opens_escalates_and_resolves_rollover_alert(tmp_path: Path):
    _, db, manager, managed, source_gateway, _, _, _ = await setup_pair(tmp_path)

    low = await manager.report_context_usage(
        source_gateway["id"],
        context_usage_percent=79,
        source="product_surface",
    )
    assert low["telemetry_available"] is True
    assert low["rollover"]["urgency"] == "optional"
    assert low["alert_open"] is False
    assert await db.list_alerts(status="open") == []

    warning = await manager.report_context_usage(
        source_gateway["id"],
        context_usage_percent=85,
        source="product_surface",
    )
    assert warning["rollover"]["urgency"] == "recommended"
    assert warning["next_action"] == "prepare-rollover"
    alerts = await db.list_alerts(status="open")
    assert len(alerts) == 1
    assert alerts[0]["kind"] == "managed.session.context_near_full"
    assert alerts[0]["data"]["managed_session_id"] == managed["id"]
    assert alerts[0]["data"]["context_usage_percent"] == 85

    critical = await manager.report_context_usage(
        source_gateway["id"],
        context_usage_percent=93,
        source="product_surface",
    )
    assert critical["rollover"]["urgency"] == "critical"
    assert critical["next_action"] == "rollover-now"
    alerts = await db.list_alerts(status="open")
    assert len(alerts) == 1
    assert alerts[0]["kind"] == "managed.session.context_critical"
    assert alerts[0]["severity"] == "critical"

    rows = await db.managed_session_overview()
    row = next(item for item in rows if item["id"] == managed["id"])
    assert row["context_usage"]["context_usage_percent"] == 93
    assert row["context_usage"]["urgency"] == "critical"

    recovered = await manager.report_context_usage(
        source_gateway["id"],
        context_usage_percent=60,
        source="product_surface",
    )
    assert recovered["rollover"]["urgency"] == "optional"
    assert await db.list_alerts(status="open") == []


@pytest.mark.asyncio
async def test_handoff_reuses_latest_reported_context_when_percent_is_omitted(tmp_path: Path):
    _, _, manager, _, source_gateway, _, _, _ = await setup_pair(tmp_path)

    await manager.report_context_usage(
        source_gateway["id"],
        context_usage_percent=91,
        source="product_surface",
    )
    prepared = await manager.prepare_session_handoff(
        source_gateway["id"],
        summary="Continue in a fresh conversation with the same managed session.",
    )

    assert prepared["handoff"]["context_usage_percent"] == 91
    assert prepared["rollover"]["urgency"] == "critical"
    assert prepared["rollover"]["recommended"] is True


@pytest.mark.asyncio
async def test_prepare_handoff_does_not_transfer_ownership_before_claim(tmp_path: Path):
    _, db, manager, managed, source_gateway, _, source_studio, _ = await setup_pair(tmp_path)

    prepared = await manager.prepare_session_handoff(
        source_gateway["id"],
        summary="Continue the HIRDA session handoff implementation.",
        reason="chat context rollover",
        context_usage_percent=85,
    )

    assert prepared["ownership_transferred"] is False
    assert prepared["source_remains_attached_until_claim"] is True
    assert prepared["claim_token"].startswith("msh_")
    assert prepared["rollover"]["recommended"] is True
    assert prepared["rollover"]["urgency"] == "recommended"

    source_after = await db.get_gateway_session(source_gateway["id"])
    logical_after = await db.get_session(source_studio["id"])
    assert source_after["status"] == "connected"
    assert source_after["managed_session_id"] == managed["id"]
    assert logical_after["managed_session_id"] == managed["id"]

    handoffs = await db.list_session_handoffs(managed["id"])
    assert len(handoffs) == 1
    assert handoffs[0]["state"] == "pending"
    assert "token_hash" not in handoffs[0]

    internal = await db.get_session_handoff(handoffs[0]["id"])
    assert prepared["claim_token"] not in json.dumps(internal)


@pytest.mark.asyncio
async def test_new_handoff_supersedes_old_pending_token_and_audit_never_stores_raw_token(tmp_path: Path):
    _, db, manager, managed, source_gateway, target_gateway, _, _ = await setup_pair(tmp_path)

    first = await manager.prepare_session_handoff(
        source_gateway["id"],
        summary="First rollover packet.",
    )
    second = await manager.prepare_session_handoff(
        source_gateway["id"],
        summary="Replacement rollover packet.",
    )

    rows = await db.list_session_handoffs(managed["id"])
    states = {row["id"]: row["state"] for row in rows}
    assert states[first["handoff"]["id"]] == "superseded"
    assert states[second["handoff"]["id"]] == "pending"

    with pytest.raises(ManagedSessionError, match="session handoff is superseded"):
        await manager.accept_session_handoff(target_gateway["id"], token=first["claim_token"])

    def audit_dump() -> str:
        with db._connect() as conn:
            rows = conn.execute(
                "SELECT data_json FROM audit_log WHERE action='managed.session.handoff.prepare'"
            ).fetchall()
            return "\n".join(str(row[0]) for row in rows)

    dumped = await db._run(audit_dump)
    assert first["claim_token"] not in dumped
    assert second["claim_token"] not in dumped


@pytest.mark.asyncio
async def test_claim_handoff_binds_target_closes_source_and_consumes_token(tmp_path: Path, monkeypatch):
    _, db, manager, managed, source_gateway, target_gateway, source_studio, target_studio = await setup_pair(tmp_path)

    opened: list[tuple[str, str]] = []
    closed: list[tuple[str | None, str | None]] = []

    async def fake_open(gateway, url):
        opened.append((gateway["id"], url))
        return "target-managed-up"

    async def fake_close(url, upstream_session_id):
        closed.append((url, upstream_session_id))

    monkeypatch.setattr(manager, "_open_upstream_session", fake_open)
    monkeypatch.setattr(manager, "_close_upstream_session", fake_close)

    prepared = await manager.prepare_session_handoff(
        source_gateway["id"],
        summary="Resume from the session handoff checkpoint and continue the next implementation step.",
        ttl_seconds=1800,
        context_usage_percent=93,
    )
    token = prepared["claim_token"]

    claimed = await manager.accept_session_handoff(target_gateway["id"], token=token)

    assert claimed["ownership_transferred"] is True
    assert claimed["claim_consumed"] is True
    assert claimed["source_gateway_closed"] is True
    assert claimed["context"]["summary"].startswith("Resume from the session handoff")
    assert claimed["managed_session"]["id"] == managed["id"]

    target_after = await db.get_gateway_session(target_gateway["id"])
    source_after = await db.get_gateway_session(source_gateway["id"])
    target_logical = await db.get_session(target_studio["id"])
    source_logical = await db.get_session(source_studio["id"])

    assert target_after["managed_session_id"] == managed["id"]
    assert target_after["upstream_session_id"] == "target-managed-up"
    assert target_logical["managed_session_id"] == managed["id"]
    assert source_after["status"] == "closed"
    assert source_logical["managed_session_id"] is None
    assert source_logical["status"] == "disconnected"
    assert opened == [(target_gateway["id"], "http://127.0.0.1:43110/mcp")]
    assert ("http://127.0.0.1:43110/mcp", "source-managed-up") in closed

    handoff = await db.get_session_handoff(claimed["handoff"]["id"])
    assert handoff["state"] == "claimed"
    assert handoff["target_gateway_session_id"] == target_gateway["id"]

    with pytest.raises(ManagedSessionError, match="session handoff is claimed"):
        await manager.accept_session_handoff(target_gateway["id"], token=token)


@pytest.mark.asyncio
async def test_expired_handoff_never_transfers_ownership(tmp_path: Path, monkeypatch):
    _, db, manager, managed, source_gateway, target_gateway, source_studio, target_studio = await setup_pair(tmp_path)

    prepared = await manager.prepare_session_handoff(
        source_gateway["id"],
        summary="Expired rollover test.",
        ttl_seconds=60,
    )

    def expire() -> None:
        with db._connect() as conn:
            conn.execute(
                "UPDATE session_handoffs SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
                (prepared["handoff"]["id"],),
            )

    await db._run(expire)

    async def must_not_open(*args, **kwargs):
        raise AssertionError("expired handoff must not open a target upstream session")

    monkeypatch.setattr(manager, "_open_upstream_session", must_not_open)

    with pytest.raises(ManagedSessionError, match="session handoff expired"):
        await manager.accept_session_handoff(target_gateway["id"], token=prepared["claim_token"])

    source_after = await db.get_gateway_session(source_gateway["id"])
    target_after = await db.get_gateway_session(target_gateway["id"])
    source_logical = await db.get_session(source_studio["id"])
    target_logical = await db.get_session(target_studio["id"])
    assert source_after["status"] == "connected"
    assert source_after["managed_session_id"] == managed["id"]
    assert source_logical["managed_session_id"] == managed["id"]
    assert target_after["managed_session_id"] is None
    assert target_logical["managed_session_id"] is None


@pytest.mark.asyncio
async def test_natural_language_resolver_prefers_named_workspace(tmp_path: Path):
    _, db, manager, managed, source_gateway, _, _, _ = await setup_pair(tmp_path)

    async def list_sessions():
        item = await db.get_managed_session(managed["id"])
        item["use_count"] = 3
        item["last_used_at"] = "2026-09-22T17:00:00+00:00"
        return [item]

    manager.managed_sessions.list_sessions = list_sessions
    resolved = await manager.resolve_session_natural(source_gateway["id"], "กลับไปทำ alpha ต่อ")

    assert resolved["session"]["id"] == managed["id"]
    assert resolved["confidence"] > 0.5
    assert "token:alpha" in resolved["reason"]


@pytest.mark.asyncio
async def test_observed_cross_chat_continuation_is_recorded_in_handoff_history(tmp_path: Path):
    _, db, manager, managed, source_gateway, target_gateway, _, _ = await setup_pair(tmp_path)
    await db.bind_gateway_managed_session(
        target_gateway["id"],
        managed_session_id=managed["id"],
        upstream_url="http://127.0.0.1:43110/mcp",
        upstream_session_id="target-managed-up",
    )

    event = await manager._record_observed_cross_chat_handoff(
        target_gateway["id"],
        managed,
        summary="ไปต่อจากแชตก่อน",
        reason="natural-language-session-continuation",
    )

    assert event is not None
    assert event["state"] == "observed"
    assert event["source_gateway_session_id"] == source_gateway["id"]
    assert event["target_gateway_session_id"] == target_gateway["id"]

    history = await db.managed_session_history(managed["id"])
    assert len(history["handoffs"]) == 1
    assert history["handoffs"][0]["state"] == "observed"
    assert history["handoffs"][0]["summary"] == "ไปต่อจากแชตก่อน"
    assert "token_hash" not in history["handoffs"][0]


@pytest.mark.asyncio
async def test_continuity_prefers_current_conversation_binding_over_global_recency(tmp_path: Path):
    _, db, manager, managed, source_gateway, _, _, _ = await setup_pair(tmp_path)

    other_project = tmp_path / "projects" / "beta"
    other_project.mkdir()
    await db.upsert_managed_workspace(
        key="beta",
        name="Beta",
        project_path=str(other_project.resolve()),
    )
    other = await db.create_managed_session(
        name="Beta",
        workspace_key="beta",
        project_path=str(other_project.resolve()),
        server_id="serena-8001",
        port=43111,
    )
    other = await db.update_managed_session_runtime(
        other["id"],
        status="ready",
        pid=456,
        endpoint="http://127.0.0.1:43111/mcp",
    )

    async def list_sessions():
        current = await db.get_managed_session(managed["id"])
        current["use_count"] = 1
        current["last_used_at"] = "2026-09-22T16:00:00+00:00"
        recent = await db.get_managed_session(other["id"])
        recent["use_count"] = 9999
        recent["last_used_at"] = "2026-09-22T18:00:00+00:00"
        return [recent, current]

    manager.managed_sessions.list_sessions = list_sessions
    resolved = await manager.resolve_session_natural(source_gateway["id"], "ต่อ session เดิม")

    assert resolved["session"]["id"] == managed["id"]
    assert resolved["reason"] == "current-conversation-binding"


@pytest.mark.asyncio
async def test_fresh_chat_continuity_with_multiple_sessions_fails_closed(tmp_path: Path):
    _, db, manager, managed, _, target_gateway, _, _ = await setup_pair(tmp_path)

    other_project = tmp_path / "projects" / "beta"
    other_project.mkdir()
    await db.upsert_managed_workspace(
        key="beta",
        name="Beta",
        project_path=str(other_project.resolve()),
    )
    other = await db.create_managed_session(
        name="Beta",
        workspace_key="beta",
        project_path=str(other_project.resolve()),
        server_id="serena-8001",
        port=43111,
    )
    other = await db.update_managed_session_runtime(
        other["id"],
        status="ready",
        pid=456,
        endpoint="http://127.0.0.1:43111/mcp",
    )

    async def list_sessions():
        first = await db.get_managed_session(managed["id"])
        first["use_count"] = 1
        first["last_used_at"] = "2026-09-22T16:00:00+00:00"
        second = await db.get_managed_session(other["id"])
        second["use_count"] = 9999
        second["last_used_at"] = "2026-09-22T18:00:00+00:00"
        return [second, first]

    manager.managed_sessions.list_sessions = list_sessions
    resolved = await manager.resolve_session_natural(target_gateway["id"], "ต่อ session เดิม")

    assert resolved["session"] is None
    assert resolved["reason"] == "ambiguous-continuity-no-lineage"
    assert {item["id"] for item in resolved["candidates"]} == {managed["id"], other["id"]}


@pytest.mark.asyncio
async def test_exact_workspace_with_multiple_sessions_is_not_silently_selected(tmp_path: Path):
    _, db, manager, managed, _, target_gateway, _, _ = await setup_pair(tmp_path)

    sibling = await db.create_managed_session(
        name="Alpha Secondary",
        workspace_key="alpha",
        project_path=managed["project_path"],
        server_id="serena-8001",
        port=43112,
    )
    sibling = await db.update_managed_session_runtime(
        sibling["id"],
        status="ready",
        pid=789,
        endpoint="http://127.0.0.1:43112/mcp",
    )

    async def list_sessions():
        primary = await db.get_managed_session(managed["id"])
        primary["name"] = "Primary"
        secondary = await db.get_managed_session(sibling["id"])
        secondary["name"] = "Secondary"
        return [primary, secondary]

    manager.managed_sessions.list_sessions = list_sessions
    resolved = await manager.resolve_session_natural(target_gateway["id"], "alpha")

    assert resolved["session"] is None
    assert resolved["reason"] == "ambiguous-workspace"
    assert {item["id"] for item in resolved["candidates"]} == {managed["id"], sibling["id"]}


@pytest.mark.asyncio
async def test_exact_workspace_with_current_binding_uses_that_bound_session(tmp_path: Path):
    _, db, manager, managed, source_gateway, _, _, _ = await setup_pair(tmp_path)

    sibling = await db.create_managed_session(
        name="Alpha Secondary",
        workspace_key="alpha",
        project_path=managed["project_path"],
        server_id="serena-8001",
        port=43112,
    )
    sibling = await db.update_managed_session_runtime(
        sibling["id"],
        status="ready",
        pid=789,
        endpoint="http://127.0.0.1:43112/mcp",
    )

    async def list_sessions():
        return [
            await db.get_managed_session(sibling["id"]),
            await db.get_managed_session(managed["id"]),
        ]

    manager.managed_sessions.list_sessions = list_sessions
    resolved = await manager.resolve_session_natural(source_gateway["id"], "กลับไป alpha ต่อ")

    assert resolved["session"]["id"] == managed["id"]
    assert "current-conversation-binding" in resolved["reason"]
