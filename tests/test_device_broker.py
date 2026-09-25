from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mcp_studio.device_broker import DeviceBroker, DeviceBrokerError
from mcp_studio.device_pairing import DevicePairingManager


def _studio(tmp_path: Path, **overrides):
    values = {
        "device_pairing_enabled": True,
        "device_pairing_state_path": str(tmp_path / "pairing.json"),
        "device_pairing_secret_path": str(tmp_path / "pairing.key"),
        "device_pairing_base_url": "https://hirda.example/pair",
        "device_pairing_ttl_seconds": 300,
        "device_pairing_max_attempts": 5,
        "device_broker_enabled": True,
        "device_broker_challenge_ttl_seconds": 30,
        "device_broker_session_ttl_seconds": 900,
        "device_broker_heartbeat_timeout_seconds": 45,
        "device_broker_max_message_bytes": 262144,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _pair(tmp_path: Path):
    studio = _studio(tmp_path)
    pairing = DevicePairingManager(studio, base_dir=tmp_path)
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes_raw()
    public_text = "ed25519:" + base64.urlsafe_b64encode(public).decode("ascii").rstrip("=")
    started = pairing.start(
        name="JKFASTDEV",
        platform="windows",
        architecture="x86_64",
        public_key=public_text,
        capabilities=["filesystem", "process", "computer_use"],
        runtime_version="1.0.0",
    )
    nonce = started["pairing_url"].split("?nonce=", 1)[1]
    pairing.verify(started["session_id"], code=started["display_code"], nonce=nonce)
    device = pairing.approve(started["session_id"], actor="owner")
    return studio, pairing, private, device


class FakeSocket:
    def __init__(self):
        self.closed = []

    async def close(self, *, code: int, reason: str):
        self.closed.append((code, reason))


def _sig(private: Ed25519PrivateKey, text: str) -> str:
    raw = private.sign(text.encode("utf-8"))
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def test_trusted_device_can_authenticate_fresh_challenge(tmp_path):
    studio, pairing, private, device = _pair(tmp_path)
    broker = DeviceBroker(studio, pairing)

    challenge = broker.issue_challenge(device["device_id"])
    verified = broker.verify_challenge(
        challenge["challenge_id"],
        _sig(private, challenge["signing_input"]),
    )
    assert verified["device_id"] == device["device_id"]


def test_challenge_is_single_use_even_after_success(tmp_path):
    studio, pairing, private, device = _pair(tmp_path)
    broker = DeviceBroker(studio, pairing)
    challenge = broker.issue_challenge(device["device_id"])
    signature = _sig(private, challenge["signing_input"])

    broker.verify_challenge(challenge["challenge_id"], signature)
    with pytest.raises(DeviceBrokerError, match="challenge_unknown_or_consumed"):
        broker.verify_challenge(challenge["challenge_id"], signature)


def test_bad_signature_consumes_challenge(tmp_path):
    studio, pairing, _, device = _pair(tmp_path)
    broker = DeviceBroker(studio, pairing)
    challenge = broker.issue_challenge(device["device_id"])
    attacker = Ed25519PrivateKey.generate()

    with pytest.raises(DeviceBrokerError, match="device_signature_invalid"):
        broker.verify_challenge(
            challenge["challenge_id"],
            _sig(attacker, challenge["signing_input"]),
        )
    with pytest.raises(DeviceBrokerError, match="challenge_unknown_or_consumed"):
        broker.verify_challenge(challenge["challenge_id"], "x")


def test_revoked_device_cannot_receive_new_challenge(tmp_path):
    studio, pairing, _, device = _pair(tmp_path)
    broker = DeviceBroker(studio, pairing)
    pairing.revoke(device["device_id"], actor="owner")

    with pytest.raises(DeviceBrokerError, match="device_not_trusted"):
        broker.issue_challenge(device["device_id"])


@pytest.mark.asyncio
async def test_new_connection_replaces_old_connection_for_same_device(tmp_path):
    studio, pairing, _, device = _pair(tmp_path)
    broker = DeviceBroker(studio, pairing)
    first = FakeSocket()
    second = FakeSocket()

    one = await broker.register_connection(device["device_id"], first, remote="first")
    two = await broker.register_connection(device["device_id"], second, remote="second")

    assert one["connection_id"] != two["connection_id"]
    assert first.closed == [(4410, "Device connection replaced")]
    snapshot = await broker.snapshot()
    assert snapshot["online_count"] == 1
    assert snapshot["connections"][0]["remote"] == "second"


@pytest.mark.asyncio
async def test_connection_unregister_is_generation_safe(tmp_path):
    studio, pairing, _, device = _pair(tmp_path)
    broker = DeviceBroker(studio, pairing)
    first = FakeSocket()
    second = FakeSocket()

    one = await broker.register_connection(device["device_id"], first)
    two = await broker.register_connection(device["device_id"], second)
    await broker.unregister_connection(device["device_id"], one["connection_id"])
    assert (await broker.snapshot())["online_count"] == 1

    await broker.unregister_connection(device["device_id"], two["connection_id"])
    assert (await broker.snapshot())["online_count"] == 0


def test_broker_disabled_fails_closed(tmp_path):
    studio, pairing, _, device = _pair(tmp_path)
    studio.device_broker_enabled = False
    broker = DeviceBroker(studio, pairing)
    with pytest.raises(DeviceBrokerError, match="device_broker_disabled"):
        broker.issue_challenge(device["device_id"])
