from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


_DEVICE_NAME_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,120}$")
_PLATFORM_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,31}$")
_CAPABILITY_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_SAFE_DEVICE_POLICY = {
    "read": True,
    "write": False,
    "execute": False,
    "destructive": False,
    "fail_closed_unknown": True,
}


class DevicePairingError(RuntimeError):
    pass


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(value + padding)
    except Exception as exc:
        raise DevicePairingError("invalid_device_public_key") from exc


class DevicePairingManager:
    """Cross-platform HIRDA device identity and human-verification pairing core.

    P1 deliberately stops at trust establishment. The outbound authenticated
    device transport is introduced by P2, and dynamic Machine Registry
    materialization follows in P3.
    """

    SCHEMA = "hirda-device-pairing-v1"
    PUBLIC_KEY_PREFIX = "ed25519:"

    def __init__(self, studio: Any, *, base_dir: Path) -> None:
        self.studio = studio
        self.base_dir = Path(base_dir).resolve(strict=False)
        self.enabled = bool(getattr(studio, "device_pairing_enabled", False))
        raw_path = str(
            getattr(studio, "device_pairing_state_path", "data/device-pairing.json")
            or "data/device-pairing.json"
        )
        path = Path(raw_path).expanduser()
        self.state_path = path if path.is_absolute() else self.base_dir / path

        raw_secret = str(
            getattr(studio, "device_pairing_secret_path", "data/device-pairing.key")
            or "data/device-pairing.key"
        )
        secret_path = Path(raw_secret).expanduser()
        self.secret_path = secret_path if secret_path.is_absolute() else self.base_dir / secret_path

        self.base_url = str(
            getattr(studio, "device_pairing_base_url", "https://localhost/pair")
            or "https://localhost/pair"
        ).rstrip("/")
        self.ttl_seconds = max(
            60, min(900, int(getattr(studio, "device_pairing_ttl_seconds", 300) or 300))
        )
        self.owner_approval_ttl_seconds = max(
            300,
            min(
                86400,
                int(
                    getattr(studio, "device_pairing_owner_approval_ttl_seconds", 3600)
                    or 3600
                ),
            ),
        )
        self.max_attempts = max(
            1, min(10, int(getattr(studio, "device_pairing_max_attempts", 5) or 5))
        )
        self._secret = self._load_or_create_secret()
        self._state = self._load_state()

    def _empty_state(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "sessions": {},
            "devices": {},
            "updated_at": _iso(_utcnow()),
        }

    def _load_or_create_secret(self) -> bytes:
        try:
            secret = self.secret_path.read_bytes()
        except FileNotFoundError:
            self.secret_path.parent.mkdir(parents=True, exist_ok=True)
            secret = secrets.token_bytes(32)
            fd = os.open(
                self.secret_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            try:
                os.write(fd, secret)
            finally:
                os.close(fd)
        except OSError as exc:
            raise DevicePairingError(f"device pairing secret unavailable: {exc}") from exc
        if len(secret) < 32:
            raise DevicePairingError("device pairing secret must be at least 32 bytes")
        try:
            os.chmod(self.secret_path, 0o600)
        except OSError:
            pass
        return secret

    def _load_state(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return self._empty_state()
        except (OSError, json.JSONDecodeError) as exc:
            raise DevicePairingError(f"device pairing state unavailable: {exc}") from exc
        if not isinstance(raw, dict) or raw.get("schema") != self.SCHEMA:
            raise DevicePairingError("device pairing state schema mismatch")
        state = self._empty_state()
        for key in ("sessions", "devices"):
            if isinstance(raw.get(key), dict):
                state[key] = dict(raw[key])
        state["updated_at"] = raw.get("updated_at") or _iso(_utcnow())
        return state

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state["updated_at"] = _iso(_utcnow())
        tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(self._state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, self.state_path)

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise DevicePairingError("device_pairing_disabled")

    def _normalize_public_key(self, public_key: str) -> tuple[str, bytes]:
        value = str(public_key or "").strip()
        if not value.startswith(self.PUBLIC_KEY_PREFIX):
            raise DevicePairingError("device_public_key_must_use_ed25519")
        encoded = value[len(self.PUBLIC_KEY_PREFIX) :]
        raw = _b64url_decode(encoded)
        if len(raw) != 32:
            raise DevicePairingError("ed25519_public_key_must_be_32_bytes")
        canonical = self.PUBLIC_KEY_PREFIX + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        return canonical, raw

    def _normalize_capabilities(self, capabilities: list[str]) -> list[str]:
        if len(capabilities) > 32:
            raise DevicePairingError("too_many_device_capabilities")
        normalized = sorted({str(item or "").strip().lower() for item in capabilities})
        if any(not _CAPABILITY_RE.fullmatch(item) for item in normalized):
            raise DevicePairingError("invalid_device_capability")
        return normalized

    def _device_id(self, public_key_raw: bytes) -> str:
        return "dev_" + hashlib.sha256(public_key_raw).hexdigest()[:24]

    def _fingerprint(self, public_key_raw: bytes) -> str:
        digest = hashlib.sha256(public_key_raw).digest()
        return "sha256:" + base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

    def _code_digest(self, session_id: str, code: str) -> str:
        return hmac.new(
            self._secret,
            f"{session_id}:{code}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _nonce_digest(self, session_id: str, nonce: str) -> str:
        return hmac.new(
            self._secret,
            f"{session_id}:nonce:{nonce}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _session(self, session_id: str) -> dict[str, Any]:
        item = self._state["sessions"].get(str(session_id))
        if not isinstance(item, dict):
            raise KeyError(session_id)
        if item.get("state") not in {"trusted", "rejected", "expired", "locked"}:
            if _parse_iso(str(item["expires_at"])) <= _utcnow():
                item["state"] = "expired"
                item["expired_at"] = _iso(_utcnow())
                self._save_state()
        return item

    def _public_session(self, item: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in item.items()
            if key not in {"code_digest", "nonce_digest"}
        }

    def start(
        self,
        *,
        name: str,
        platform: str,
        architecture: str,
        public_key: str,
        capabilities: list[str],
        runtime_version: str | None = None,
    ) -> dict[str, Any]:
        self._require_enabled()
        name = str(name or "").strip()
        if not _DEVICE_NAME_RE.fullmatch(name):
            raise DevicePairingError("invalid_device_name")
        platform = str(platform or "").strip().lower()
        architecture = str(architecture or "").strip().lower()
        if not _PLATFORM_RE.fullmatch(platform):
            raise DevicePairingError("invalid_device_platform")
        if not _PLATFORM_RE.fullmatch(architecture):
            raise DevicePairingError("invalid_device_architecture")
        canonical_key, public_key_raw = self._normalize_public_key(public_key)
        normalized_capabilities = self._normalize_capabilities(capabilities)
        device_id = self._device_id(public_key_raw)

        existing = self._state["devices"].get(device_id)
        if isinstance(existing, dict) and existing.get("trust_state") == "trusted":
            raise DevicePairingError("device_already_trusted")

        now = _utcnow()
        session_id = "pair_" + secrets.token_urlsafe(18)
        nonce = secrets.token_urlsafe(24)
        code = f"{secrets.randbelow(100_000_000):08d}"
        item = {
            "session_id": session_id,
            "device_id": device_id,
            "name": name,
            "platform": platform,
            "architecture": architecture,
            "runtime_version": str(runtime_version or "").strip() or None,
            "public_key": canonical_key,
            "fingerprint": self._fingerprint(public_key_raw),
            "capabilities": normalized_capabilities,
            "state": "pairing",
            "attempts": 0,
            "max_attempts": self.max_attempts,
            "created_at": _iso(now),
            "expires_at": _iso(now + timedelta(seconds=self.ttl_seconds)),
            "code_digest": self._code_digest(session_id, code),
            "nonce_digest": self._nonce_digest(session_id, nonce),
        }
        self._state["sessions"][session_id] = item
        self._save_state()
        return {
            **self._public_session(item),
            "display_code": code,
            "pairing_url": f"{self.base_url}/{session_id}?nonce={nonce}",
        }

    def status(self, session_id: str) -> dict[str, Any]:
        self._require_enabled()
        return self._public_session(self._session(session_id))

    def verify(self, session_id: str, *, code: str, nonce: str) -> dict[str, Any]:
        self._require_enabled()
        item = self._session(session_id)
        if item.get("state") != "pairing":
            raise DevicePairingError(f"pairing_session_not_verifiable:{item.get('state')}")
        supplied_code = re.sub(r"\s+", "", str(code or ""))
        if not re.fullmatch(r"\d{8}", supplied_code):
            raise DevicePairingError("pairing_code_must_be_8_digits")
        supplied_nonce = str(nonce or "").strip()
        nonce_ok = hmac.compare_digest(
            str(item["nonce_digest"]),
            self._nonce_digest(session_id, supplied_nonce),
        )
        code_ok = hmac.compare_digest(
            str(item["code_digest"]),
            self._code_digest(session_id, supplied_code),
        )
        if not nonce_ok or not code_ok:
            item["attempts"] = int(item.get("attempts") or 0) + 1
            if item["attempts"] >= int(item.get("max_attempts") or self.max_attempts):
                item["state"] = "locked"
                item["locked_at"] = _iso(_utcnow())
            self._save_state()
            raise DevicePairingError("pairing_verification_failed")
        verified_at = _utcnow()
        item["state"] = "pending_owner"
        item["verified_at"] = _iso(verified_at)
        item["expires_at"] = _iso(
            verified_at + timedelta(seconds=self.owner_approval_ttl_seconds)
        )
        self._save_state()
        return self._public_session(item)

    def approve(
        self,
        session_id: str,
        *,
        actor: str,
        policy: dict[str, bool] | None = None,
    ) -> dict[str, Any]:
        self._require_enabled()
        item = self._session(session_id)
        if item.get("state") != "pending_owner":
            raise DevicePairingError(
                f"pairing_session_not_approvable:{item.get('state')}"
            )

        effective_policy = dict(_SAFE_DEVICE_POLICY)
        supplied = dict(policy or {})
        for key in _SAFE_DEVICE_POLICY:
            if key not in supplied:
                continue
            value = supplied[key]
            if not isinstance(value, bool):
                raise DevicePairingError(f"invalid_device_policy:{key}")
            effective_policy[key] = value

        now = _iso(_utcnow())
        device = {
            "device_id": item["device_id"],
            "name": item["name"],
            "platform": item["platform"],
            "architecture": item["architecture"],
            "runtime_version": item.get("runtime_version"),
            "public_key": item["public_key"],
            "fingerprint": item["fingerprint"],
            "capabilities": list(item.get("capabilities") or []),
            "trust_state": "trusted",
            "policy": effective_policy,
            "paired_at": now,
            "approved_by": str(actor or "unknown"),
            "last_seen_at": None,
            "transport": None,
        }
        self._state["devices"][device["device_id"]] = device
        item["state"] = "trusted"
        item["approved_at"] = now
        item["approved_by"] = device["approved_by"]
        self._save_state()
        return dict(device)

    def reject(self, session_id: str, *, actor: str, reason: str = "owner-rejected") -> dict[str, Any]:
        self._require_enabled()
        item = self._session(session_id)
        if item.get("state") in {"trusted", "expired", "locked"}:
            raise DevicePairingError(f"pairing_session_not_rejectable:{item.get('state')}")
        item["state"] = "rejected"
        item["rejected_at"] = _iso(_utcnow())
        item["rejected_by"] = str(actor or "unknown")
        item["reason"] = str(reason or "owner-rejected")[:500]
        self._save_state()
        return self._public_session(item)

    def get_device(self, device_id: str) -> dict[str, Any]:
        device = self._state["devices"].get(str(device_id))
        if not isinstance(device, dict):
            raise KeyError(device_id)
        return dict(device)

    def trusted_device(self, device_id: str) -> dict[str, Any]:
        device = self.get_device(device_id)
        if device.get("trust_state") != "trusted":
            raise DevicePairingError("device_not_trusted")
        return device

    def list_devices(self) -> dict[str, Any]:
        self._require_enabled()
        items = [
            dict(value)
            for _, value in sorted(self._state["devices"].items())
            if isinstance(value, dict)
        ]
        return {"schema": self.SCHEMA, "device_count": len(items), "devices": items}

    def revoke(self, device_id: str, *, actor: str, reason: str = "owner-revoked") -> dict[str, Any]:
        self._require_enabled()
        device = self._state["devices"].get(str(device_id))
        if not isinstance(device, dict):
            raise KeyError(device_id)
        device["trust_state"] = "revoked"
        device["revoked_at"] = _iso(_utcnow())
        device["revoked_by"] = str(actor or "unknown")
        device["revoke_reason"] = str(reason or "owner-revoked")[:500]
        self._save_state()
        return dict(device)
