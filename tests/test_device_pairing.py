from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from mcp_studio.device_pairing import DevicePairingError, DevicePairingManager


def _public_key(seed: int = 1) -> str:
    raw = bytes([seed]) * 32
    return "ed25519:" + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _studio(tmp_path: Path, **overrides):
    values = {
        "device_pairing_enabled": True,
        "device_pairing_state_path": str(tmp_path / "pairing.json"),
        "device_pairing_secret_path": str(tmp_path / "pairing.key"),
        "device_pairing_base_url": "https://hirda.example/pair",
        "device_pairing_ttl_seconds": 300,
        "device_pairing_max_attempts": 5,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _manager(tmp_path: Path, **overrides) -> DevicePairingManager:
    return DevicePairingManager(_studio(tmp_path, **overrides), base_dir=tmp_path)


def _start(manager: DevicePairingManager, *, seed: int = 1):
    return manager.start(
        name="JKFASTDEV",
        platform="windows",
        architecture="x86_64",
        public_key=_public_key(seed),
        capabilities=["filesystem", "process", "computer_use"],
        runtime_version="1.0.0",
    )


def _nonce(started: dict) -> str:
    return started["pairing_url"].split("?nonce=", 1)[1]


def test_pairing_start_returns_qr_payload_and_8_digit_code_without_persisting_code(tmp_path):
    manager = _manager(tmp_path)
    started = _start(manager)

    assert started["session_id"].startswith("pair_")
    assert started["device_id"].startswith("dev_")
    assert started["display_code"].isdigit()
    assert len(started["display_code"]) == 8
    assert started["pairing_url"].startswith(
        f"https://hirda.example/pair/{started['session_id']}?nonce="
    )
    assert started["state"] == "pairing"

    persisted = (tmp_path / "pairing.json").read_text(encoding="utf-8")
    assert started["display_code"] not in persisted
    assert _nonce(started) not in persisted
    assert json.loads(persisted)["sessions"][started["session_id"]]["code_digest"]


def test_device_id_is_stable_for_same_public_key(tmp_path):
    first = _start(_manager(tmp_path / "a"), seed=7)
    second = _start(_manager(tmp_path / "b"), seed=7)
    assert first["device_id"] == second["device_id"]
    assert first["fingerprint"] == second["fingerprint"]


def test_verify_requires_both_qr_nonce_and_8_digit_code(tmp_path):
    manager = _manager(tmp_path)
    started = _start(manager)

    with pytest.raises(DevicePairingError, match="pairing_verification_failed"):
        manager.verify(
            started["session_id"],
            code=started["display_code"],
            nonce="wrong-nonce",
        )
    assert manager.status(started["session_id"])["attempts"] == 1

    verified = manager.verify(
        started["session_id"],
        code=started["display_code"],
        nonce=_nonce(started),
    )
    assert verified["state"] == "pending_owner"
    assert "display_code" not in verified


def test_pairing_locks_after_bounded_failed_attempts(tmp_path):
    manager = _manager(tmp_path, device_pairing_max_attempts=3)
    started = _start(manager)

    for _ in range(3):
        with pytest.raises(DevicePairingError, match="pairing_verification_failed"):
            manager.verify(
                started["session_id"],
                code="00000000",
                nonce=_nonce(started),
            )

    assert manager.status(started["session_id"])["state"] == "locked"
    with pytest.raises(DevicePairingError, match="pairing_session_not_verifiable:locked"):
        manager.verify(
            started["session_id"],
            code=started["display_code"],
            nonce=_nonce(started),
        )


def test_owner_approval_is_separate_from_human_verification(tmp_path):
    manager = _manager(tmp_path)
    started = _start(manager)

    with pytest.raises(DevicePairingError, match="pairing_session_not_approvable:pairing"):
        manager.approve(started["session_id"], actor="owner")

    manager.verify(
        started["session_id"],
        code=started["display_code"],
        nonce=_nonce(started),
    )
    device = manager.approve(
        started["session_id"],
        actor="owner",
        policy={"read": True, "write": False},
    )

    assert device["trust_state"] == "trusted"
    assert device["approved_by"] == "owner"
    assert device["platform"] == "windows"
    assert device["policy"] == {"read": True, "write": False}
    assert manager.status(started["session_id"])["state"] == "trusted"


def test_same_contract_accepts_linux_and_macos_metadata(tmp_path):
    manager = _manager(tmp_path)
    linux = manager.start(
        name="openclaw",
        platform="linux",
        architecture="x86_64",
        public_key=_public_key(2),
        capabilities=["filesystem", "process"],
    )
    mac = manager.start(
        name="Jack-MacBook",
        platform="macos",
        architecture="arm64",
        public_key=_public_key(3),
        capabilities=["filesystem", "process", "computer_use"],
    )
    assert linux["platform"] == "linux"
    assert mac["platform"] == "macos"


def test_revoke_changes_trust_state_without_deleting_identity(tmp_path):
    manager = _manager(tmp_path)
    started = _start(manager)
    manager.verify(
        started["session_id"],
        code=started["display_code"],
        nonce=_nonce(started),
    )
    device = manager.approve(started["session_id"], actor="owner")
    revoked = manager.revoke(device["device_id"], actor="owner", reason="lost-device")

    assert revoked["device_id"] == device["device_id"]
    assert revoked["trust_state"] == "revoked"
    assert manager.list_devices()["device_count"] == 1


def test_pairing_disabled_fails_closed(tmp_path):
    manager = _manager(tmp_path, device_pairing_enabled=False)
    with pytest.raises(DevicePairingError, match="device_pairing_disabled"):
        _start(manager)
