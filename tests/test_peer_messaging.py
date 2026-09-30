from types import SimpleNamespace
from typing import Any, cast

import pytest

from mcp_studio.db import Database
from mcp_studio.gateway import GatewaySessionManager
from mcp_studio.tool_permissions import classify_tool


@pytest.mark.asyncio
async def test_peer_db_round_trip_is_principal_scoped(tmp_path):
    db = Database(str(tmp_path / "peer.sqlite3"))
    await db.init()

    sent = await db.create_peer_message(
        peer_scope_id="scope-main",
        sender_principal_id="oauth-fern",
        recipient_principal_id="oauth-somchai",
        text="hello",
        correlation_id="cert-001",
    )
    assert sent["sender_principal_id"] == "oauth-fern"
    assert sent["recipient_principal_id"] == "oauth-somchai"
    assert sent["correlation_id"] == "cert-001"
    assert sent["claimed_at"] is None

    inbox = await db.list_peer_inbox(
        "oauth-somchai", peer_scope_id="scope-main", unclaimed_only=True
    )
    assert [item["message_id"] for item in inbox] == [sent["message_id"]]

    with pytest.raises(PermissionError):
        await db.claim_peer_message(
            sent["message_id"],
            principal_id="oauth-other",
            peer_scope_id="scope-main",
        )

    claimed = await db.claim_peer_message(
        sent["message_id"],
        principal_id="oauth-somchai",
        peer_scope_id="scope-main",
    )
    assert claimed["claimed_by_principal_id"] == "oauth-somchai"

    reply = await db.reply_peer_message(
        sent["message_id"],
        sender_principal_id="oauth-somchai",
        peer_scope_id="scope-main",
        text="hello back",
    )
    assert reply["reply_to"] == sent["message_id"]
    assert reply["thread_id"] == sent["thread_id"]
    assert reply["correlation_id"] == sent["correlation_id"]
    assert reply["sender_principal_id"] == "oauth-somchai"
    assert reply["recipient_principal_id"] == "oauth-fern"

    fern_inbox = await db.list_peer_inbox(
        "oauth-fern", peer_scope_id="scope-main"
    )
    assert [item["message_id"] for item in fern_inbox] == [reply["message_id"]]

    threads = await db.list_peer_threads(
        "oauth-fern",
        peer_scope_id="scope-main",
        correlation_id="cert-001",
    )
    assert len(threads) == 1
    assert [item["message_id"] for item in threads[0]["messages"]] == [
        sent["message_id"],
        reply["message_id"],
    ]


@pytest.mark.asyncio
async def test_peer_thread_cannot_be_hijacked_or_recorrelated(tmp_path):
    db = Database(str(tmp_path / "peer.sqlite3"))
    await db.init()
    sent = await db.create_peer_message(
        peer_scope_id="scope-main",
        sender_principal_id="oauth-a",
        recipient_principal_id="oauth-b",
        text="one",
        correlation_id="corr-one",
    )

    with pytest.raises(PermissionError):
        await db.create_peer_message(
            peer_scope_id="scope-main",
            sender_principal_id="oauth-c",
            recipient_principal_id="oauth-a",
            text="hijack",
            thread_id=sent["thread_id"],
        )

    with pytest.raises(ValueError):
        await db.create_peer_message(
            peer_scope_id="scope-main",
            sender_principal_id="oauth-a",
            recipient_principal_id="oauth-b",
            text="re-correlate",
            thread_id=sent["thread_id"],
            correlation_id="corr-two",
        )


@pytest.mark.asyncio
async def test_peer_scope_blocks_cross_connector_visibility_and_claims(tmp_path):
    db = Database(str(tmp_path / "peer.sqlite3"))
    await db.init()
    sent = await db.create_peer_message(
        peer_scope_id="scope-one",
        sender_principal_id="oauth-a",
        recipient_principal_id="oauth-b",
        text="private",
        correlation_id="scope-cert",
    )

    assert await db.list_peer_inbox(
        "oauth-b", peer_scope_id="scope-two"
    ) == []

    with pytest.raises(PermissionError):
        await db.claim_peer_message(
            sent["message_id"],
            principal_id="oauth-b",
            peer_scope_id="scope-two",
        )

    with pytest.raises(PermissionError):
        await db.reply_peer_message(
            sent["message_id"],
            sender_principal_id="oauth-b",
            peer_scope_id="scope-two",
            text="should not cross scope",
        )

    assert await db.list_peer_threads(
        "oauth-a",
        peer_scope_id="scope-two",
        correlation_id="scope-cert",
    ) == []


def test_peer_tools_are_control_plane_and_sender_is_not_user_supplied(tmp_path):
    gateway = GatewaySessionManager(
        cast(Any, SimpleNamespace()),
        Database(str(tmp_path / "peer.sqlite3")),
        managed_sessions=None,
    )
    tools = {tool["name"]: tool for tool in gateway._management_tools()}
    expected = {
        "hirda_peer_list",
        "hirda_peer_send",
        "hirda_peer_inbox",
        "hirda_peer_claim",
        "hirda_peer_reply",
        "hirda_peer_threads",
    }
    assert expected <= set(tools)
    send_schema = tools["hirda_peer_send"]["inputSchema"]
    assert "sender" not in send_schema["properties"]
    assert "from" not in send_schema["properties"]

    assert classify_tool("hirda_peer_list", {}) == "read"
    assert classify_tool("hirda_peer_inbox", {}) == "read"
    assert classify_tool("hirda_peer_threads", {}) == "read"
    assert classify_tool("hirda_peer_send", {}) == "execute"
    assert classify_tool("hirda_peer_claim", {}) == "execute"
    assert classify_tool("hirda_peer_reply", {}) == "execute"


@pytest.mark.asyncio
async def test_peer_gateway_round_trip_requires_no_managed_session(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "peer.sqlite3"))
    await db.init()
    gateway = GatewaySessionManager(
        cast(Any, SimpleNamespace()),
        db,
        managed_sessions=None,
    )

    current = {"principal": "oauth-fern"}

    async def principal_for_gateway(_gateway_session_id):
        return current["principal"]

    async def peer_scope_for_gateway(_gateway_session_id):
        return "scope-main"

    monkeypatch.setattr(
        gateway,
        "connector_principal_for_gateway",
        principal_for_gateway,
    )
    monkeypatch.setattr(
        gateway,
        "connector_peer_scope_for_gateway",
        peer_scope_for_gateway,
    )

    sent_result = await gateway._handle_management_tool(
        "gws-any",
        "hirda_peer_send",
        {
            "recipient": "oauth-somchai",
            "text": "cert ping",
            "correlation_id": "cert-round-trip",
        },
    )
    sent = sent_result["message"]
    assert sent["sender_principal_id"] == "oauth-fern"

    current["principal"] = "oauth-somchai"
    inbox = await gateway._handle_management_tool(
        "gws-any",
        "hirda_peer_inbox",
        {"unclaimed_only": True},
    )
    assert inbox["messages"][0]["message_id"] == sent["message_id"]

    claimed = await gateway._handle_management_tool(
        "gws-any",
        "hirda_peer_claim",
        {"message_id": sent["message_id"]},
    )
    assert claimed["message"]["claimed_by_principal_id"] == "oauth-somchai"

    replied = await gateway._handle_management_tool(
        "gws-any",
        "hirda_peer_reply",
        {"message_id": sent["message_id"], "text": "cert pong"},
    )
    reply = replied["message"]
    assert reply["correlation_id"] == sent["correlation_id"]

    current["principal"] = "oauth-fern"
    threads = await gateway._handle_management_tool(
        "gws-any",
        "hirda_peer_threads",
        {"correlation_id": "cert-round-trip"},
    )
    assert len(threads["threads"]) == 1
    assert [item["text"] for item in threads["threads"][0]["messages"]] == [
        "cert ping",
        "cert pong",
    ]


@pytest.mark.asyncio
async def test_peer_list_collapses_conversations_to_principals(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "peer.sqlite3"))
    await db.init()

    async def fake_sessions(limit=1000, tunnel_id=None):
        assert limit == 1000
        return [
            {
                "client_id": "conversation-a1",
                "client_type": "openai-chatgpt",
                "status": "connected",
                "last_seen_at": "2026-09-30T10:00:00+00:00",
                "metadata": {
                    "connector_principal_id": "oauth-a",
                    "connector_peer_scope_id": "scope-main",
                },
            },
            {
                "client_id": "conversation-a2",
                "client_type": "openai-chatgpt",
                "status": "connected",
                "last_seen_at": "2026-09-30T10:01:00+00:00",
                "metadata": {
                    "connector_principal_id": "oauth-a",
                    "connector_peer_scope_id": "scope-main",
                },
            },
            {
                "client_id": "conversation-b1",
                "client_type": "openai-chatgpt",
                "status": "closed",
                "last_seen_at": "2026-09-30T09:59:00+00:00",
                "metadata": {
                    "connector_principal_id": "oauth-b",
                    "connector_peer_scope_id": "scope-main",
                },
            },
        ]

    monkeypatch.setattr(db, "list_gateway_sessions", fake_sessions)
    peers = await db.list_peer_principals(
        current_principal_id="oauth-a",
        peer_scope_id="scope-main",
    )
    assert [peer["principal_id"] for peer in peers] == ["oauth-a", "oauth-b"]
    assert peers[0]["online"] is True
    assert peers[0]["conversation_count"] == 2
    assert peers[0]["self"] is True
    assert peers[1]["online"] is False
