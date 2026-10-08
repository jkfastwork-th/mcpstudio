"""Owner-governed, allowlisted disk maintenance for the *local* HIRDA node.

Never accepts shell commands or arbitrary filesystem paths. Plans are volatile,
content-bound and single use; enabling execution requires a separate machine
maintenance capability, not blanket destructive tool permission.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import secrets
import shutil
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_RELEASE = re.compile(r"^(\d+)\.(\d+)\.(\d+)-x86_64-unknown-linux-musl$")
_VALID_TARGETS = frozenset({"codex.old_releases", "npm.cacache", "bun.download_cache"})


class DiskMaintenanceError(ValueError):
    pass


@dataclass(frozen=True)
class _Candidate:
    key: str
    path: Path
    bytes: int
    fingerprint: str

    def public(self) -> dict[str, Any]:
        return {"id": self.key, "path": str(self.path), "bytes": self.bytes}


def _signature(path: Path) -> tuple[str, int]:
    """Hash every entry's type/identity/mtime/size without following symlinks."""
    if not path.is_dir() or path.is_symlink():
        raise DiskMaintenanceError("candidate is not an ordinary directory")
    digest = hashlib.sha256()
    total = 0
    for base, dirs, files in os.walk(path, topdown=True, followlinks=False):
        base_path = Path(base)
        for name in sorted(dirs + files):
            entry = base_path / name
            st = entry.lstat()
            rel = str(entry.relative_to(path))
            if stat.S_ISLNK(st.st_mode):
                meta = os.readlink(entry)
            elif stat.S_ISREG(st.st_mode):
                total += st.st_size
                meta = ""
            elif stat.S_ISDIR(st.st_mode):
                meta = ""
            else:
                raise DiskMaintenanceError("special filesystem entry in cleanup target")
            digest.update(
                f"{rel}\x00{st.st_dev}:{st.st_ino}:{st.st_mode}:{st.st_size}:{st.st_mtime_ns}:{meta}\n".encode()
            )
        dirs[:] = [name for name in dirs if not (base_path / name).is_symlink()]
    root = path.lstat()
    digest.update(f"ROOT:{root.st_dev}:{root.st_ino}:{root.st_mtime_ns}".encode())
    return digest.hexdigest(), total


class DiskMaintenance:
    def __init__(self, home: Path, *, ttl_seconds: int = 600) -> None:
        self.home = home.expanduser().resolve(strict=True)
        if not self.home.is_dir():
            raise DiskMaintenanceError("home directory missing")
        self.ttl_seconds = ttl_seconds
        self._plans: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    def _protected_path(self, path: Path) -> bool:
        """All candidates must be within a fixed safe base without symlink parents."""
        if not path.is_relative_to(self.home):
            return False
        current = self.home
        for item in path.relative_to(self.home).parts:
            current = current / item
            if current.is_symlink():
                return False
        return path.is_dir() and not path.is_symlink()

    @staticmethod
    def _process_commands() -> list[str]:
        proc = Path("/proc")
        if not proc.is_dir():
            raise DiskMaintenanceError("cannot check process references")
        result = []
        for item in proc.iterdir():
            if not item.name.isdigit():
                continue
            try:
                command = (item / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
            except (ProcessLookupError, FileNotFoundError):
                continue
            except PermissionError as exc:
                raise DiskMaintenanceError("cannot inspect all process references") from exc
            if command:
                result.append(command)
        return result

    def _candidates(self, targets: frozenset[str]) -> tuple[list[_Candidate], list[dict[str, str]]]:
        active_commands = self._process_commands()
        active_text = "\n".join(active_commands)
        selected: list[_Candidate] = []
        skipped: list[dict[str, str]] = []

        if "codex.old_releases" in targets:
            for kind in ("standalone", "app-server-daemon"):
                parent = self.home / ".codex" / "packages" / kind / "releases"
                if not parent.exists():
                    continue
                if not self._protected_path(parent):
                    raise DiskMaintenanceError("unsafe Codex release root")
                versions = sorted(
                    (p for p in parent.iterdir() if p.is_dir() and _RELEASE.fullmatch(p.name)),
                    key=lambda p: tuple(int(v) for v in _RELEASE.fullmatch(p.name).groups()),
                    reverse=True,
                )
                retained = set(versions[:3])
                # Preserve currently-selected CLI binary even if it is not a top-3 version.
                current_bin = (self.home / ".local" / "bin" / "codex").resolve(strict=False)
                for item in versions:
                    key = f"codex:{kind}:{item.name}"
                    if item in retained or current_bin.is_relative_to(item):
                        skipped.append({"id": key, "reason": "retained_release"})
                        continue
                    if str(item) in active_text:
                        skipped.append({"id": key, "reason": "in_use"})
                        continue
                    if not self._protected_path(item):
                        raise DiskMaintenanceError("unsafe Codex release candidate")
                    fingerprint, size = _signature(item)
                    selected.append(_Candidate(key, item, size, fingerprint))

        for target, rel, blocked_tokens in (
            ("npm.cacache", (".npm", "_cacache"), ("npm ", "npm\x00", "npx ", "npm exec", "npm install", "npm ci")),
            ("bun.download_cache", (".bun", "install", "cache"), ("bunx ", "bun install", "bun add", "bun update")),
        ):
            if target not in targets:
                continue
            path = self.home.joinpath(*rel)
            if not path.exists():
                continue
            if not self._protected_path(path):
                raise DiskMaintenanceError(f"unsafe {target} root")
            if any(token in active_text for token in blocked_tokens):
                skipped.append({"id": target, "reason": "package_manager_active"})
                continue
            fingerprint, size = _signature(path)
            selected.append(_Candidate(target, path, size, fingerprint))

        selected.sort(key=lambda c: c.key)
        return selected, skipped

    @staticmethod
    def _validate_targets(targets: list[str]) -> frozenset[str]:
        if not isinstance(targets, list) or not targets or len(targets) > len(_VALID_TARGETS):
            raise DiskMaintenanceError("choose one or more maintenance target IDs")
        if any(not isinstance(t, str) or t not in _VALID_TARGETS for t in targets):
            raise DiskMaintenanceError("unknown maintenance target")
        return frozenset(targets)

    async def plan(self, targets: list[str]) -> dict[str, Any]:
        chosen = self._validate_targets(targets)
        async with self._lock:
            candidates, skipped = self._candidates(chosen)
            manifest = "\n".join(
                f"{c.key}\x00{c.fingerprint}\x00{c.bytes}" for c in candidates
            )
            digest = hashlib.sha256(manifest.encode()).hexdigest()
            plan_id = secrets.token_urlsafe(24)
            self._plans[plan_id] = {
                "targets": chosen, "candidates": candidates, "digest": digest,
                "expires": time.monotonic() + self.ttl_seconds, "approved": None,
            }
            return {
                "plan_id": plan_id, "fingerprint": digest, "dry_run": True,
                "candidates": [c.public() for c in candidates],
                "skipped": skipped, "estimated_bytes": sum(c.bytes for c in candidates),
                "expires_in_seconds": self.ttl_seconds,
            }

    async def approve(self, plan_id: str, fingerprint: str) -> dict[str, Any]:
        async with self._lock:
            plan = self._plans.get(plan_id)
            if not plan or time.monotonic() >= plan["expires"]:
                raise DiskMaintenanceError("unknown or expired plan")
            if plan["approved"] is not None:
                raise DiskMaintenanceError("plan already approved")
            if not secrets.compare_digest(plan["digest"], fingerprint):
                raise DiskMaintenanceError("plan fingerprint mismatch")
            if not plan["candidates"]:
                raise DiskMaintenanceError("nothing to approve")
            token = secrets.token_urlsafe(32)
            plan["approved"] = hashlib.sha256(token.encode()).hexdigest()
            return {"plan_id": plan_id, "approval_token": token, "single_use": True}

    async def apply(
        self,
        plan_id: str,
        approval_token: str,
        *,
        enabled: bool,
        machine_maintenance_permission: bool,
    ) -> dict[str, Any]:
        if not enabled or not machine_maintenance_permission:
            raise DiskMaintenanceError("maintenance not enabled by operator and local machine policy")
        if not shutil.rmtree.avoids_symlink_attacks:
            raise DiskMaintenanceError("safe fd-based directory removal unavailable")
        async with self._lock:
            plan = self._plans.pop(plan_id, None)  # Single-use, even on validation failure.
            if not plan or time.monotonic() >= plan["expires"] or not plan["approved"]:
                raise DiskMaintenanceError("plan is not approved or expired")
            expected = hashlib.sha256(approval_token.encode()).hexdigest()
            if not secrets.compare_digest(expected, plan["approved"]):
                raise DiskMaintenanceError("invalid approval token")
            current, _ = self._candidates(plan["targets"])
            expected_items = [(c.key, c.path, c.fingerprint) for c in plan["candidates"]]
            current_items = [(c.key, c.path, c.fingerprint) for c in current]
            if expected_items != current_items:
                raise DiskMaintenanceError("filesystem or process changed; new plan required")

            freed_estimate = 0
            completed: list[str] = []
            for candidate in plan["candidates"]:
                if not self._protected_path(candidate.path):
                    raise DiskMaintenanceError("cleanup path changed")
                fingerprint, _ = _signature(candidate.path)
                if fingerprint != candidate.fingerprint:
                    raise DiskMaintenanceError("cleanup target changed; operation halted")
                shutil.rmtree(candidate.path)
                completed.append(candidate.key)
                freed_estimate += candidate.bytes
            return {"completed": completed, "estimated_bytes_removed": freed_estimate}
