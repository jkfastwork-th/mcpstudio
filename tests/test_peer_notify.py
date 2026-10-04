"""Focused tests for Peer Notify Layer.

Covers: online delivery, wrong-scope isolation, offline inbox persists,
reconnect drain, and notification does not claim message.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest

from mcp_studio.db import Database
from mcp_studio.gateway import GatewaySessionManager


async def _make_gateway(tmp_path, monkeypatch, *, principal: str, scope: str):
    db = Database(str(tmp_path / "peer.sqlite3"))
    await db.init()

    gateway = GatewaySessionManager(
        cast(Any, SimpleNamespace()),
        db,
        managed_sessions=None,
    )

    async def principal_for_gateway(_gw_id):
        return principal

    async def peer_scope_for_gateway(_gw_id):
        return scope

    monkeypatch.setattr(gateway, "connector_principal_for_gateway", principal_for_gateway)
    monkeypatch.setattr(gateway, "connector_peer_scope_for_gateway", peer_scope_for_gateway)
    return db, gateway


@pytest.mark.asyncio
async def test_online_delivery_enqueues_notification_to_recipient(tmp_path, monkeypatch):
    """hirda_peer_send enqueues a notification to the recipient's gateway queue."""
    db, gateway = await _make_gateway(tmp_path, monkeypatch, principal="oauth-a", scope="scope-main")

    # Register a transport for the recipient (oauth-b)
    recipient_queue = await gateway._connect_peer_notify_transport("oauth-b", "scope-main")

    # Send a message from oauth-a to oauth-b
    message = await db.create_peer_message(
        peer_scope_id="scope-main",
        sender_principal_id="oauth-a",
        recipient_principal_id="oauth-b",
        text="hello online",
        correlation_id="corr-online",
    )

    # Notify (simulating what hirda_peer_send does)
    await gateway._notify_peer_message(message)

    # The recipient's queue should have the notification
    assert not recipient_queue.empty()
    notification = recipient_queue.get_nowait()
    assert notification["method"] == "notifications/message"
    assert notification["params"]["level"] == "info"
    assert notification["params"]["logger"] == "hirda.peer"
    data = notification["params"]["data"]
    assert data["message_id"] == message["message_id"]
    assert data["sender_principal_id"] == "oauth-a"
    assert data["recipient_principal_id"] == "oauth-b"
    assert data["thread_id"] == message["thread_id"]
    assert data["correlation_id"] == "corr-online"
    assert data["text"] == "hello online"

    # Cleanup
    gateway._disconnect_peer_notify_transport("oauth-b", "scope-main", recipient_queue)


@pytest.mark.asyncio
async def test_wrong_scope_isolation_no_cross_scope_delivery(tmp_path, monkeypatch):
    """Notifications are NOT delivered to gateway sessions in a different scope."""
    db, gateway = await _make_gateway(tmp_path, monkeypatch, principal="oauth-a", scope="scope-one")

    # Register transports in different scopes
    queue_scope_one = await gateway._connect_peer_notify_transport("oauth-b", "scope-one")
    queue_scope_two = await gateway._connect_peer_notify_transport("oauth-b", "scope-two")

    # Create a message in scope-one
    message = await db.create_peer_message(
        peer_scope_id="scope-one",
        sender_principal_id="oauth-a",
        recipient_principal_id="oauth-b",
        text="private to scope-one",
        correlation_id="corr-scope",
    )

    # Notify for scope-one recipient
    await gateway._notify_peer_message(message)

    # scope-one queue should have the notification
    assert not queue_scope_one.empty()
    notification = queue_scope_one.get_nowait()
    assert notification["params"]["data"]["recipient_principal_id"] == "oauth-b"

    # scope-two queue should be empty (no cross-scope delivery)
    assert queue_scope_two.empty()

    # Cleanup
    gateway._disconnect_peer_notify_transport("oauth-b", "scope-one", queue_scope_one)
    gateway._disconnect_peer_notify_transport("oauth-b", "scope-two", queue_scope_two)


@pytest.mark.asyncio
async def test_offline_inbox_persists_zero_data_loss(tmp_path, monkeypatch):
    """Messages sent while recipient is offline persist in the inbox."""
    db, gateway = await _make_gateway(tmp_path, monkeypatch, principal="oauth-a", scope="main")

    # No transports registered (recipient offline)
    # Send multiple messages
    msg1 = await db.create_peer_message(
        peer_scope_id="scope-main",
        sender_principal_id="oauth-a",
        recipient_principal_id="oauth-b",
        text="offline message 1",
        correlation_id="corr-offline-1",
    )
    msg2 = await db.create_peer_message(
        peer_scope_id="scope-main",
        sender_principal_id="oauth-a",
        recipient_principal_id="oauth-b",
        text="offline message 2",
        correlation_id="corr-offline-2",
    )

    # Notify (no active transports, so it falls back to inbox)
    await gateway._notify_peer_message(msg1)
    await gateway._notify_peer_message(msg2)

    # Verify messages persist in the inbox
    inbox = await db.list_peer_inbox(
        "oauth-b", peer_scope_id="scope-main", unclaimed_only=True
    )
    assert len(inbox) == 2
    assert {m["message_id"] for m in inbox} == {msg1["message_id"], msg2["message_id"]}
    # Messages are NOT claimed
    assert all(m["claimed_at"] is None for m in inbox)


@pytest.mark.asyncio
async def test_reconnect_drain_emits_notifications_without_claiming(tmp_path, monkeypatch):
    """On reconnect, unclaimed inbox items are drained as notifications without claiming."""
    db, gateway = await _make_gateway(tmp_path, monkeypatch, principal="oauth-b", scope="main")

    # Create messages while oauth-b was offline
    msg1 = await db.create_peer_message(
        peer_scope_id="scope-main",
        sender_principal_id="oauth-a",
        recipient_principal_id="oauth-b",
        text="drain me 1",
        correlation_id="corr-drain-1",
    )
    msg2 = await db.create_peer_message(
        peer_scope_id="scope-main",
        sender_principal_id="oauth-a",
        recipient_principal_id="oauth-b",
        text="drain me 2",
        correlation_id="corr-drain-2",
    )

    # Simulate reconnect: connect transport (drains inbox)
    queue = await gateway._connect_peer_notify_transport("oauth-b", "scope-main")

    # Queue should have 2 notifications
    assert queue.qsize() == 2

    # Verify notifications are well-formed
    notifications = []
    while not queue.empty():
        notifications.append(queue.get_nowait())
    assert len(notifications) == 2
    for notification in notifications:
        assert notification["method"] == "notifications/message"
        assert notification["params"]["logger"] == "hirda.peer"
        data = notification["params"]["data"]
        assert data["recipient_principal_id"] == "oauth-b"
        assert data["sender_principal_id"] == "oauth-a"

    # CRITICAL: messages are NOT claimed
    inbox = await db.list_peer_inbox(
        "oauth-b", peer_scope_id="scope-main", unclaimed_only=True
    )
    assert len(inbox) == 2
    assert all(m["claimed_at"] is None for m in inbox)

    # Cleanup
    gateway._disconnect_peer_notify_transport("oauth-b", "scope-main", queue)


@pytest.mark.asyncio
async def test_notification_does_not_claim_message(tmp_path, monkeypatch):
    """Sending a peer message does NOT auto-claim it for the recipient."""
    db, gateway = await _make_gateway(tmp_path, monkeypatch, principal="oauth-a", scope="main")

    # Register a transport for the recipient
    recipient_queue = await gateway._connect_peer_notify_transport("oauth-b", "scope-main")

    # Send a message
    message = await db.create_peer_message(
        peer_scope_id="scope-main",
        sender_principal_id="oauth-a",
        recipient_principal_id="oauth-b",
        text="do not claim me",
        correlation_id="corr-no-claim",
    )

    # Notify
    await gateway._notify_peer_message(message)

    # Verify the message is still unclaimed
    inbox = await db.list_peer_inbox(
        "oauth-b", peer_scope_id="scope-main", unclaimed_only=True
    )
    assert len(inbox) == 1
    assert inbox[0]["message_id"] == message["message_id"]
    assert inbox[0]["claimed_at"] is None
    assert inbox[0]["claimed_by_principal_id"] is None

    # Cleanup
    gateway._disconnect_peer_notify_transport("oauth-b", "scope-main", recipient_queue)


@pytest.mark.asyncio
async def test_dedupe_prevents_double_delivery(tmp_path, monkeypatch):
    """Same message is not delivered twice to the same transport."""
    db, gateway = await _make_gateway(tmp_path, monkeypatch, principal="oauth-a", scope="main")

    # Register a transport
    queue = await gateway._connect_peer_notify_transport("oauth-b", "scope-main")

    # Create a message
    message = await db.create_peer_message(
        peer_scope_id="scope-main",
        sender_principal_id="oauth-a",
        recipient_principal_id="oauth-b",
        text="dedupe test",
    )

    # Notify twice
    await gateway._notify_peer_message(message)
    await gateway._notify_peer_message(message)

    # Should only be delivered once
    assert queue.qsize() == 1

    # Cleanup
    gateway._disconnect_peer_notify_transport("oauth-b", "scope-main", queue)


@pytest.mark.asyncio
async def test_disconnect_removes_transport(tmp_path, monkeypatch):
    """Disconnecting removes the transport from the registry."""
    db, gateway = await _make_gateway(tmp_path, monkeypatch, principal="oauth-a", scope="scope-main")

    # Register a transport
    queue = await gateway._connect_peer_notify_transport("oauth-b", "scope-main")
    assert ("oauth-b", "scope-main") in gateway._peer_notify_transports

    # Disconnect
    gateway._disconnect_peer_notify_transport("oauth-b", "scope-main", queue)
    assert ("oauth-b", "scope-main") not in gateway._peer_notify_transports



@pytest.mark.asyncio
async def test_tool_catalog_change_notification_is_enqueued_once(tmp_path, monkeypatch):
    _, gateway = await _make_gateway(
        tmp_path,
        monkeypatch,
        principal="oauth-a",
        scope="scope-main",
    )
    queue: asyncio.Queue[dict] = asyncio.Queue()
    session = {
        "metadata": {
            "initialize_response": {
                "tool_catalog_hash": "stale-catalog-hash",
            }
        }
    }

    assert await gateway._queue_tool_catalog_refresh_if_needed(
        "gws-test",
        session,
        queue,
    ) is True
    notice = queue.get_nowait()
    assert notice == {
        "jsonrpc": "2.0",
        "method": "notifications/tools/list_changed",
    }

    assert await gateway._queue_tool_catalog_refresh_if_needed(
        "gws-test",
        session,
        queue,
    ) is False
    assert queue.empty()


def test_initialize_augmentation_advertises_logging_capability():
    """Initialize response advertises logging capability alongside tools.listChanged."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "capabilities": {
                "tools": {},
            },
        },
    }
    result = GatewaySessionManager._augment_initialize_payload(payload)
    caps = result["result"]["capabilities"]
    assert isinstance(caps["logging"], dict)
    assert caps["tools"]["listChanged"] is True


def test_initialize_augmentation_preserves_existing_logging_capability():
    """Existing logging capability dict is preserved, not overwritten."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "capabilities": {
                "tools": {},
                "logging": {"level": "debug"},
            },
        },
    }
    result = GatewaySessionManager._augment_initialize_payload(payload)
    caps = result["result"]["capabilities"]
    assert caps["logging"] == {"level": "debug"}
    assert caps["tools"]["listChanged"] is True


def test_initialize_augmentation_normalizes_non_dict_logging():
    """Non-dict logging capability (None, string, list) is normalized to {}."""
    for bad_value in (None, "enabled", [1, 2], 42):
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "capabilities": {
                    "tools": {},
                    "logging": bad_value,
                },
            },
        }
        result = GatewaySessionManager._augment_initialize_payload(payload)
        caps = result["result"]["capabilities"]
        assert caps["logging"] == {}
        assert caps["tools"]["listChanged"] is True
