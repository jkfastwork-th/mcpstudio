import asyncio
import asyncio
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import websockets


class HermesGroupError(RuntimeError):
    pass


@dataclass(slots=True)
class HermesGroupResult:
    room_id: str
    output: str
    events: list[dict[str, Any]]
    latest_seq: int
    driver_status: dict[str, Any]
    member_outputs: dict[str, str]
    recovery_rounds: int
    lifecycle: dict[str, Any]


class HermesGroupClient:
    """Minimal headless client for Hermes Bot Mode Group Chat JSON-RPC."""

    def __init__(self, studio: Any) -> None:
        self.studio = studio
        self._scope_locks: dict[str, asyncio.Lock] = {}
        self._lifecycle_status: dict[str, Any] = {}

    def _token(self) -> tuple[str | None, str | None]:
        token_env = str(
            getattr(
                self.studio,
                "cognitive_hermes_group_token_env",
                "HIRDA_HERMES_GROUP_TOKEN",
            )
            or ""
        ).strip()
        if token_env:
            value = os.getenv(token_env)
            if value:
                return value.strip(), "env"

        token_file = str(
            getattr(
                self.studio,
                "cognitive_hermes_group_token_file",
                "",
            )
            or ""
        ).strip()
        if not token_file:
            return None, None
        path = Path(token_file).expanduser()
        try:
            mode = path.stat().st_mode & 0o777
            if mode & 0o077:
                raise HermesGroupError("hermes_group_token_file_permissions")
            text = path.read_text(encoding="utf-8")
        except HermesGroupError:
            raise
        except FileNotFoundError:
            return None, None
        except OSError as exc:
            raise HermesGroupError("hermes_group_token_file_unreadable") from exc
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() == "HERMES_DASHBOARD_SESSION_TOKEN":
                token = value.strip().strip('"').strip("\'")
                if token:
                    return token, "file"
        return None, None

    def status(self) -> dict[str, Any]:
        url = str(
            getattr(self.studio, "cognitive_hermes_group_ws_url", "") or ""
        ).strip()
        token_env = str(
            getattr(
                self.studio,
                "cognitive_hermes_group_token_env",
                "HIRDA_HERMES_GROUP_TOKEN",
            )
            or ""
        ).strip()
        token_file = str(
            getattr(
                self.studio,
                "cognitive_hermes_group_token_file",
                "",
            )
            or ""
        ).strip()
        try:
            token, token_source = self._token()
        except HermesGroupError:
            token, token_source = None, "invalid_file"
        return {
            "schema": "hirda-hermes-group-runtime-v2",
            "configured": bool(url),
            "ws_url": url or None,
            "token_env": token_env or None,
            "token_file": token_file or None,
            "token_available": bool(token),
            "token_source": token_source,
            "room_id": str(
                getattr(
                    self.studio,
                    "cognitive_hermes_group_room_id",
                    "hirda-cognitive-team",
                )
                or "hirda-cognitive-team"
            ),
            "lifecycle": {
                "scope_mode": str(
                    getattr(
                        self.studio,
                        "cognitive_hermes_group_scope_mode",
                        "workspace",
                    )
                    or "workspace"
                ),
                "rotation_enabled": bool(
                    getattr(
                        self.studio,
                        "cognitive_hermes_group_rotation_enabled",
                        True,
                    )
                ),
                "max_events": int(
                    getattr(
                        self.studio,
                        "cognitive_hermes_group_max_events",
                        96,
                    )
                    or 0
                ),
                "max_age_seconds": float(
                    getattr(
                        self.studio,
                        "cognitive_hermes_group_max_age_seconds",
                        21600.0,
                    )
                    or 0.0
                ),
                "last": dict(self._lifecycle_status),
            },
        }

    def _ws_url(self) -> str:
        url = str(
            getattr(self.studio, "cognitive_hermes_group_ws_url", "") or ""
        ).strip()
        if not url:
            raise HermesGroupError("hermes_group_ws_url_missing")
        token, _token_source = self._token()
        if not token:
            return url
        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query.setdefault("token", token)
        return urlunsplit(
            (
                parts.scheme,
                parts.netloc,
                parts.path,
                urlencode(query),
                parts.fragment,
            )
        )

    async def rpc(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout_seconds: float = 10.0,
    ) -> dict[str, Any]:
        request_id = f"hirda-{uuid4().hex}"
        frame = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": dict(params or {}),
        }
        try:
            async with websockets.connect(
                self._ws_url(),
                open_timeout=timeout_seconds,
                close_timeout=2.0,
            ) as ws:
                await ws.send(json.dumps(frame, ensure_ascii=False) + "\n")
                while True:
                    raw = await asyncio.wait_for(
                        ws.recv(),
                        timeout=timeout_seconds,
                    )
                    text = (
                        raw.decode("utf-8", errors="replace")
                        if isinstance(raw, bytes)
                        else str(raw)
                    )
                    for line in text.splitlines():
                        if not line.strip():
                            continue
                        payload = json.loads(line)
                        if payload.get("id") != request_id:
                            continue
                        if payload.get("error"):
                            error = payload["error"]
                            raise HermesGroupError(
                                str(error.get("message") or error)
                            )
                        result = payload.get("result")
                        return dict(result) if isinstance(result, dict) else {
                            "value": result
                        }
        except HermesGroupError:
            raise
        except Exception as exc:
            raise HermesGroupError(
                f"hermes_group_rpc_{type(exc).__name__}"
            ) from exc


    @staticmethod
    def _scope_digest(scope_key: str | None) -> str:
        raw = str(scope_key or "shared").strip() or "shared"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]

    def _room_family(self, scope_key: str | None) -> tuple[str, str, str]:
        base_id = str(
            getattr(
                self.studio,
                "cognitive_hermes_group_room_id",
                "hirda-cognitive-team",
            )
            or "hirda-cognitive-team"
        ).strip()
        base_name = str(
            getattr(
                self.studio,
                "cognitive_hermes_group_room_name",
                "HIRDA Cognitive Team",
            )
            or "HIRDA Cognitive Team"
        ).strip()
        scope_digest = self._scope_digest(scope_key)
        raw_scope = str(scope_key or "shared").strip() or "shared"
        if raw_scope == "shared":
            return base_id, base_name, scope_digest
        family_id = f"{base_id}-s{scope_digest}"
        family_name = f"{base_name} [{scope_digest}]"
        return family_id, family_name, scope_digest

    @staticmethod
    def _room_generation(room_id: str, family_id: str) -> int | None:
        if room_id == family_id:
            return 0
        match = re.fullmatch(
            re.escape(family_id) + r"-r(\d{4,})",
            room_id,
        )
        if match is None:
            return None
        return int(match.group(1))

    @staticmethod
    def _room_roster_matches(
        room: dict[str, Any],
        members: list[dict[str, Any]],
    ) -> bool:
        def normalized(rows: Any) -> list[tuple[str, str, str]]:
            result: list[tuple[str, str, str]] = []
            for row in rows or []:
                if not isinstance(row, dict):
                    continue
                result.append(
                    (
                        str(row.get("member_id") or ""),
                        str(row.get("profile") or ""),
                        str(row.get("handle") or ""),
                    )
                )
            return sorted(result)

        return normalized(room.get("members")) == normalized(members)

    async def _room_rotation_metrics(
        self,
        room: dict[str, Any],
        members: list[dict[str, Any]],
    ) -> dict[str, Any]:
        latest_seq = int(room.get("latest_seq") or 0)
        created_at = float(room.get("created_at") or 0.0)
        age_seconds = (
            max(0.0, time.time() - created_at)
            if created_at > 0
            else 0.0
        )
        max_events = int(
            getattr(
                self.studio,
                "cognitive_hermes_group_max_events",
                96,
            )
            or 0
        )
        max_age_seconds = float(
            getattr(
                self.studio,
                "cognitive_hermes_group_max_age_seconds",
                21600.0,
            )
            or 0.0
        )
        reasons: list[str] = []
        if max_events > 0 and latest_seq >= max_events:
            reasons.append("event_budget_exceeded")
        if max_age_seconds > 0 and age_seconds >= max_age_seconds:
            reasons.append("age_budget_exceeded")
        if not self._room_roster_matches(room, members):
            reasons.append("roster_changed")
        return {
            "latest_seq": latest_seq,
            "age_seconds": round(age_seconds, 3),
            "rotation_reasons": reasons,
            "rotation_due": bool(reasons),
        }

    async def _safe_disband_room(self, room_id: str, *, reason: str) -> bool:
        try:
            state = await self.rpc(
                "groups.state",
                {"room_id": room_id, "include_disbanded": False},
            )
            driver_status = dict(state.get("driver_status") or {})
            counts = dict(driver_status.get("counts") or {})
            active = int(counts.get("queued") or 0) + int(
                counts.get("running") or 0
            )
            if (
                bool(driver_status.get("working"))
                or bool(driver_status.get("blocked"))
                or active > 0
            ):
                return False
            await self.rpc(
                "groups.disband",
                {
                    "room_id": room_id,
                    "cancel_id": f"hirda-lifecycle-{reason}-{uuid4().hex[:12]}",
                },
            )
            return True
        except Exception:
            return False

    def _room_spec(
        self,
        scope_key: str | None = None,
        *,
        generation: int = 0,
    ) -> tuple[str, str, list[dict[str, Any]], str, str]:
        family_id, family_name, scope_digest = self._room_family(scope_key)
        room_id = (
            family_id
            if generation <= 0
            else f"{family_id}-r{generation:04d}"
        )
        room_name = (
            family_name
            if generation <= 0
            else f"{family_name} · r{generation:04d}"
        )
        profiles = dict(
            getattr(
                self.studio,
                "cognitive_hermes_team_profiles",
                {},
            )
            or {}
        )
        members = [
            {
                "member_id": role,
                "profile": str(profiles.get(role) or role),
                "handle": str(profiles.get(role) or role),
            }
            for role in ("coder", "tester", "reviewer")
        ]
        return room_id, room_name, members, family_id, scope_digest

    async def ensure_room(
        self,
        scope_key: str | None = None,
    ) -> dict[str, Any]:
        (
            base_room_id,
            base_room_name,
            members,
            family_id,
            scope_digest,
        ) = self._room_spec(scope_key)
        capabilities = await self.rpc("groups.capabilities")
        if not capabilities.get("driver"):
            raise HermesGroupError("hermes_group_driver_unavailable")

        listed = await self.rpc(
            "groups.list",
            {"include_disbanded": True, "limit": 500, "offset": 0},
        )
        family_rows: list[tuple[int, dict[str, Any]]] = []
        for raw in listed.get("rooms") or []:
            if not isinstance(raw, dict):
                continue
            room = dict(raw)
            room_id = str(room.get("room_id") or "")
            generation = self._room_generation(room_id, family_id)
            if generation is None:
                continue
            family_rows.append((generation, room))

        active_rows = [
            (generation, room)
            for generation, room in family_rows
            if room.get("disbanded_at") is None
        ]
        max_seen_generation = max(
            (generation for generation, _room in family_rows),
            default=-1,
        )

        room_created = False
        rotated = False
        rotated_from: str | None = None
        rotation_deferred = False
        stale_rooms_cleaned: list[str] = []
        lifecycle_error: str | None = None

        if active_rows:
            generation, room = max(active_rows, key=lambda item: item[0])
            room = dict(room)
        else:
            generation = max(0, max_seen_generation + 1)
            room_id, room_name, _members, _family, _scope = self._room_spec(
                scope_key,
                generation=generation,
            )
            created = await self.rpc(
                "groups.create",
                {
                    "room_id": room_id,
                    "name": room_name,
                    "members": members,
                },
            )
            raw_room = created.get("room")
            if not isinstance(raw_room, dict):
                raise HermesGroupError("hermes_group_create_invalid_result")
            room = dict(raw_room)
            room_created = True

        for stale_generation, stale_room in sorted(active_rows):
            stale_id = str(stale_room.get("room_id") or "")
            if (
                stale_generation < generation
                and stale_id
                and await self._safe_disband_room(
                    stale_id,
                    reason="stale-active-room",
                )
            ):
                stale_rooms_cleaned.append(stale_id)

        metrics = await self._room_rotation_metrics(room, members)
        rotation_trigger_reasons = list(metrics["rotation_reasons"])
        rotation_enabled = bool(
            getattr(
                self.studio,
                "cognitive_hermes_group_rotation_enabled",
                True,
            )
        )
        if rotation_enabled and metrics["rotation_due"]:
            state = await self.rpc(
                "groups.state",
                {
                    "room_id": str(room.get("room_id") or ""),
                    "include_disbanded": False,
                },
            )
            driver_status = dict(state.get("driver_status") or {})
            counts = dict(driver_status.get("counts") or {})
            active = int(counts.get("queued") or 0) + int(
                counts.get("running") or 0
            )
            if (
                bool(driver_status.get("working"))
                or bool(driver_status.get("blocked"))
                or active > 0
            ):
                rotation_deferred = True
            else:
                next_generation = max(
                    generation + 1,
                    max_seen_generation + 1,
                )
                next_room_id, next_room_name, _members, _family, _scope = (
                    self._room_spec(
                        scope_key,
                        generation=next_generation,
                    )
                )
                created = await self.rpc(
                    "groups.create",
                    {
                        "room_id": next_room_id,
                        "name": next_room_name,
                        "members": members,
                    },
                )
                raw_room = created.get("room")
                if not isinstance(raw_room, dict):
                    raise HermesGroupError(
                        "hermes_group_rotation_create_invalid_result"
                    )
                rotated_from = str(room.get("room_id") or "") or None
                room = dict(raw_room)
                generation = next_generation
                room_created = True
                rotated = True
                if rotated_from:
                    cleaned = await self._safe_disband_room(
                        rotated_from,
                        reason="rotation",
                    )
                    if cleaned:
                        stale_rooms_cleaned.append(rotated_from)
                    else:
                        lifecycle_error = "previous_room_cleanup_deferred"
                metrics = await self._room_rotation_metrics(room, members)

        lifecycle = {
            "schema": "hirda-hermes-group-lifecycle-v1",
            "scope_digest": scope_digest,
            "family_id": family_id,
            "room_id": str(room.get("room_id") or base_room_id),
            "generation": generation,
            "room_created": room_created,
            "rotated": rotated,
            "rotated_from": rotated_from,
            "rotation_enabled": rotation_enabled,
            "rotation_due": bool(metrics["rotation_due"]),
            "rotation_deferred": rotation_deferred,
            "rotation_reasons": (
                rotation_trigger_reasons
                if rotated or rotation_deferred
                else list(metrics["rotation_reasons"])
            ),
            "latest_seq": int(metrics["latest_seq"]),
            "age_seconds": float(metrics["age_seconds"]),
            "fresh_session_boundary": bool(room_created or rotated),
            "stale_rooms_cleaned": stale_rooms_cleaned,
            "error": lifecycle_error,
        }
        self._lifecycle_status = dict(lifecycle)
        room["_hirda_lifecycle"] = lifecycle
        return room

    @staticmethod
    def _event_text(event: dict[str, Any]) -> str:
        payload = event.get("payload")
        if not isinstance(payload, dict):
            return ""
        for key in ("text", "message", "content", "output"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""


    async def run_mission(
        self,
        mission: str,
        *,
        request_id: str,
        timeout_seconds: float,
        scope_key: str | None = None,
    ) -> HermesGroupResult:
        scope_digest = self._scope_digest(scope_key)
        lock = self._scope_locks.setdefault(scope_digest, asyncio.Lock())
        async with lock:
            return await self._run_mission_locked(
                mission,
                request_id=request_id,
                timeout_seconds=timeout_seconds,
                scope_key=scope_key,
            )

    async def _run_mission_locked(
        self,
        mission: str,
        *,
        request_id: str,
        timeout_seconds: float,
        scope_key: str | None,
    ) -> HermesGroupResult:
        room = await self.ensure_room(scope_key=scope_key)
        lifecycle = dict(room.get("_hirda_lifecycle") or {})
        room_id = str(room.get("room_id") or "")
        thread_id = f"hirda:{request_id}"
        required_roles = ("coder", "tester", "reviewer")
        required_role_set = set(required_roles)
        before = await self.rpc(
            "groups.log",
            {"room_id": room_id, "since_seq": 0, "limit": 1},
        )
        latest_seq = int(
            before.get("latest_seq") or before.get("cursor") or 0
        )
        deadline = asyncio.get_running_loop().time() + max(
            1.0, timeout_seconds
        )
        events: list[dict[str, Any]] = []
        driver_status: dict[str, Any] = {}

        async def collect_round(
            start_seq: int,
            discussion_event_id: str | None,
            terminal_roles_required: set[str],
        ) -> tuple[list[dict[str, Any]], int, dict[str, Any]]:
            seq = start_seq
            round_events: list[dict[str, Any]] = []
            terminal_roles: set[str] = set()
            terminal_kinds = {
                "turn.settled",
                "turn.failed",
                "turn.cancelled",
                "turn.deferred",
            }
            status: dict[str, Any] = {}
            stop_requested = False
            while asyncio.get_running_loop().time() < deadline:
                remaining = max(
                    1.0,
                    deadline - asyncio.get_running_loop().time(),
                )
                log = await self.rpc(
                    "groups.log",
                    {
                        "room_id": room_id,
                        "since_seq": seq,
                        "limit": 500,
                    },
                    timeout_seconds=min(10.0, remaining),
                )
                batch = [
                    dict(item)
                    for item in (log.get("events") or [])
                    if isinstance(item, dict)
                ]
                if batch:
                    round_events.extend(batch)
                    seq = int(
                        log.get("latest_seq")
                        or log.get("cursor")
                        or seq
                    )
                    for item in batch:
                        if str(item.get("kind") or "") not in terminal_kinds:
                            continue
                        payload = dict(item.get("payload") or {})
                        if (
                            discussion_event_id
                            and str(payload.get("discussion_event_id") or "")
                            != discussion_event_id
                        ):
                            continue
                        role = str(
                            payload.get("member_id") or ""
                        ).strip().casefold()
                        if role in required_role_set:
                            terminal_roles.add(role)

                state = await self.rpc(
                    "groups.state",
                    {"room_id": room_id},
                    timeout_seconds=min(10.0, remaining),
                )
                status = dict(state.get("driver_status") or {})
                counts = dict(status.get("counts") or {})
                working = bool(status.get("working"))
                active = int(counts.get("queued") or 0) + int(
                    counts.get("running") or 0
                )
                roles_complete = terminal_roles_required.issubset(terminal_roles)
                if roles_complete and (working or active > 0) and not stop_requested:
                    try:
                        await self.rpc(
                            "groups.stop",
                            {
                                "room_id": room_id,
                                "cancel_id": (
                                    f"hirda-round-complete-{request_id}-"
                                    f"{discussion_event_id or seq}"
                                ),
                            },
                            timeout_seconds=min(5.0, remaining),
                        )
                    except Exception:
                        pass
                    stop_requested = True
                if roles_complete and not working and active == 0:
                    return round_events, seq, status
                await asyncio.sleep(0.35)
            try:
                await self.rpc(
                    "groups.stop",
                    {
                        "room_id": room_id,
                        "cancel_id": f"hirda-timeout-{request_id}",
                    },
                    timeout_seconds=5.0,
                )
            except Exception:
                pass
            raise HermesGroupError("hermes_group_mission_timeout")

        async def send_round(
            *,
            event_id: str,
            text: str,
            terminal_roles_required: set[str] | None = None,
        ) -> list[dict[str, Any]]:
            nonlocal latest_seq, driver_status
            start_seq = latest_seq
            sent = await self.rpc(
                "groups.send",
                {
                    "room_id": room_id,
                    "event_id": event_id,
                    "payload": {
                        "text": text,
                        "thread_id": thread_id,
                    },
                },
            )
            event = sent.get("event")
            discussion_event_id = (
                str(event.get("event_id") or "")
                if isinstance(event, dict)
                else ""
            ) or None
            batch, latest_seq, driver_status = await collect_round(
                start_seq,
                discussion_event_id,
                terminal_roles_required or required_role_set,
            )
            events.extend(batch)
            return batch

        def collect_member_outputs(
            source_events: list[dict[str, Any]],
        ) -> dict[str, str]:
            outputs: dict[str, str] = {}
            for event in source_events:
                if str(event.get("kind") or "") != "message.member":
                    continue
                actor = dict(event.get("actor") or {})
                payload = dict(event.get("payload") or {})
                role = str(
                    actor.get("id")
                    or payload.get("member_id")
                    or ""
                ).strip().casefold()
                text = self._event_text(event)
                if role in required_role_set and text:
                    outputs[role] = text
            return outputs

        await send_round(
            event_id=f"hirda-{request_id}",
            text=mission,
        )

        finalization = (
            "HIRDA FINALIZATION ROUND. Every configured role MUST emit one "
            "substantive final role report in this round. Do not PASS and do not "
            "defer the answer to another member. If blocked, state the blocker "
            "explicitly in your own message. "
            "@coder provide the final implementation/inspection summary. "
            "@tester provide the final verification and evidence report. "
            "@reviewer provide the independent architecture review and PASS/BLOCKED "
            "recommendation. Keep each report concise and grounded in the room context."
        )
        final_events = await send_round(
            event_id=f"hirda-{request_id}-final",
            text=finalization,
        )
        member_outputs = collect_member_outputs(final_events)

        recovery_rounds = 0
        missing_roles = [
            role for role in required_roles if role not in member_outputs
        ]
        for role in tuple(missing_roles):
            targeted = (
                f"@{role} REQUIRED HIRDA ROLE COMPLETION. You did not emit a final "
                "member message in the previous finalization round. Do not PASS. "
                f"Produce your substantive final {role} report now using the existing "
                "mission and room context. If blocked, state the blocker explicitly."
            )
            retry_events = await send_round(
                event_id=f"hirda-{request_id}-retry-{role}",
                text=targeted,
                terminal_roles_required={role},
            )
            recovery_rounds += 1
            retry_outputs = collect_member_outputs(retry_events)
            for found_role, text in retry_outputs.items():
                member_outputs[found_role] = text

        missing_roles = [
            role for role in required_roles if role not in member_outputs
        ]
        if missing_roles:
            raise HermesGroupError(
                "hermes_group_missing_role_outputs:"
                + ",".join(missing_roles)
            )

        output = "\n\n".join(
            f"[{role}]\n{member_outputs[role]}"
            for role in required_roles
        ).strip()
        return HermesGroupResult(
            room_id=room_id,
            output=output,
            events=events,
            latest_seq=latest_seq,
            driver_status=driver_status,
            member_outputs=member_outputs,
            recovery_rounds=recovery_rounds,
            lifecycle=lifecycle,
        )
