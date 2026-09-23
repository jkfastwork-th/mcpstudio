from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .db import Database
from .context_fit import ContextProfile, ModelCapability, evaluate_context_fit
from .execution import build_tool_arguments, classify_failure
from .herdr import HerdrManager, _decode_text_content

AGENTS = {"claude", "codex", "hermes"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CapsuleNotFound(KeyError):
    pass


class CapsuleDeliveryError(RuntimeError):
    pass


class CapsuleService:
    """Append-only capsule projection backed by the existing Studio event ledger."""

    def __init__(
        self,
        db: Database,
        herdr: HerdrManager | None = None,
        *,
        local_api_base: str = "http://127.0.0.1:8100",
        handoff_ack_timeout_seconds: float = 45.0,
        handoff_ack_poll_seconds: float = 1.0,
    ):
        self.db = db
        self.herdr = herdr
        self.local_api_base = local_api_base.rstrip("/")
        self.handoff_ack_timeout_seconds = max(1.0, float(handoff_ack_timeout_seconds))
        self.handoff_ack_poll_seconds = max(0.1, float(handoff_ack_poll_seconds))
        self._handoff_lock = asyncio.Lock()

    async def _ledger(self, limit: int = 500) -> list[dict[str, Any]]:
        events = await self.db.recent_events(max(1, min(limit, 500)))
        return [e for e in reversed(events) if str(e.get("kind", "")).startswith("capsule.")]

    async def _projections(self, limit: int = 100) -> list[dict[str, Any]]:
        by_id: dict[str, dict[str, Any]] = {}
        for event in await self._ledger(500):
            data = dict(event.get("data") or {})
            capsule_id = str(data.get("capsule_id") or "")
            if not capsule_id:
                continue
            current = by_id.setdefault(
                capsule_id,
                {
                    "capsule_id": capsule_id,
                    "title": data.get("title") or capsule_id,
                    "workspace": data.get("workspace"),
                    "source_pane": data.get("source_pane"),
                    "current_agent": data.get("agent") or "claude",
                    "current_stage": data.get("stage") or "ingress",
                    "status": "active",
                    "created_at": event.get("created_at"),
                    "updated_at": event.get("created_at"),
                    "last_handoff": None,
                    "pending_handoff": None,
                    "handoffs": [],
                    "blocked_handoffs": [],
                    "failed_handoffs": [],
                    "last_context_fit": None,
                    "context_profile": None,
                    "capsule_type": "unknown",
                    "a2a_context_id": capsule_id,
                    "events": [],
                    "metadata": dict(data.get("metadata") or {}),
                },
            )
            kind = str(event.get("kind") or "")
            current["updated_at"] = event.get("created_at")
            current["events"].append(
                {
                    "id": event.get("id"),
                    "kind": kind,
                    "created_at": event.get("created_at"),
                    "message": event.get("message"),
                    "data": data,
                }
            )
            if kind == "capsule.created":
                current.update(
                    {
                        "title": data.get("title") or current["title"],
                        "workspace": data.get("workspace"),
                        "source_pane": data.get("source_pane"),
                        "current_agent": data.get("agent") or current["current_agent"],
                        "current_stage": data.get("stage") or "ingress",
                        "status": "active",
                        "metadata": dict(data.get("metadata") or {}),
                    }
                )
                profile_data = current["metadata"].get("context_profile")
                if isinstance(profile_data, dict):
                    current["context_profile"] = dict(profile_data)
                    current["capsule_type"] = str(profile_data.get("capsule_type") or "unknown")
            elif kind == "capsule.stage":
                current["current_stage"] = data.get("stage") or current["current_stage"]
                if data.get("agent") in AGENTS:
                    current["current_agent"] = data["agent"]
                current["metadata"].update(data.get("metadata") or {})
            elif kind == "capsule.handoff_requested":
                current["pending_handoff"] = {
                    "handoff_id": data.get("handoff_id"),
                    "connector_id": data.get("connector_id") or capsule_id,
                    "from_agent": data.get("from_agent"),
                    "to_agent": data.get("to_agent"),
                    "from_stage": data.get("from_stage") or "agent_runtime",
                    "to_stage": data.get("to_stage") or "agent_runtime",
                    "reason": data.get("reason") or "manual",
                    "state": "requested",
                    "target_pane": data.get("target_pane"),
                    "a2a_task_id": data.get("a2a_task_id"),
                    "context_fit": dict(data.get("context_fit") or {}),
                    "created_at": event.get("created_at"),
                    "metadata": dict(data.get("metadata") or {}),
                }
                current["last_context_fit"] = current["pending_handoff"].get("context_fit") or None
            elif kind == "capsule.handoff_dispatched":
                pending = current.get("pending_handoff")
                if isinstance(pending, dict) and pending.get("handoff_id") == data.get("handoff_id"):
                    pending["state"] = "dispatched"
                    pending["target_pane"] = data.get("target_pane") or pending.get("target_pane")
                    pending["a2a"] = dict(data.get("a2a") or {})
                    pending["dispatched_at"] = event.get("created_at")
            elif kind == "capsule.handoff_dispatch_uncertain":
                pending = current.get("pending_handoff")
                if isinstance(pending, dict) and pending.get("handoff_id") == data.get("handoff_id"):
                    pending["state"] = "dispatch_uncertain"
                    pending["error"] = data.get("error")
            elif kind == "capsule.handoff_acknowledged":
                pending = current.get("pending_handoff")
                if isinstance(pending, dict) and pending.get("handoff_id") == data.get("handoff_id"):
                    pending["state"] = "acknowledged"
                    pending["acknowledged_at"] = event.get("created_at")
                    pending["receipt"] = dict(data.get("receipt") or {})
            elif kind == "capsule.handoff_committed":
                pending = current.get("pending_handoff")
                handoff = {
                    "handoff_id": data.get("handoff_id"),
                    "connector_id": data.get("connector_id") or capsule_id,
                    "from_agent": data.get("from_agent"),
                    "to_agent": data.get("to_agent"),
                    "from_stage": data.get("from_stage") or "agent_runtime",
                    "to_stage": data.get("to_stage") or "agent_runtime",
                    "reason": data.get("reason") or "manual",
                    "state": "committed",
                    "target_pane": data.get("target_pane"),
                    "a2a": dict(data.get("a2a") or {}),
                    "context_fit": dict(data.get("context_fit") or {}),
                    "created_at": event.get("created_at"),
                    "metadata": dict(data.get("metadata") or {}),
                    "receipt": dict(data.get("receipt") or {}),
                }
                if isinstance(pending, dict):
                    handoff["requested_at"] = pending.get("created_at")
                    handoff["dispatched_at"] = pending.get("dispatched_at")
                    handoff["acknowledged_at"] = pending.get("acknowledged_at")
                current["handoffs"].append(handoff)
                current["last_handoff"] = handoff
                current["pending_handoff"] = None
                current["last_context_fit"] = handoff.get("context_fit") or None
                if handoff["to_agent"] in AGENTS:
                    current["current_agent"] = handoff["to_agent"]
                current["current_stage"] = handoff["to_stage"]
            elif kind == "capsule.handoff_failed":
                failed = {
                    "handoff_id": data.get("handoff_id"),
                    "from_agent": data.get("from_agent"),
                    "to_agent": data.get("to_agent"),
                    "reason": data.get("reason") or "delivery_failed",
                    "error": data.get("error"),
                    "created_at": event.get("created_at"),
                }
                current["failed_handoffs"].append(failed)
                pending = current.get("pending_handoff")
                if isinstance(pending, dict) and pending.get("handoff_id") == data.get("handoff_id"):
                    current["pending_handoff"] = None
            elif kind == "capsule.handoff":
                handoff = {
                    "handoff_id": data.get("handoff_id"),
                    "connector_id": data.get("connector_id") or capsule_id,
                    "from_agent": data.get("from_agent"),
                    "to_agent": data.get("to_agent"),
                    "from_stage": data.get("from_stage") or "agent_runtime",
                    "to_stage": data.get("to_stage") or "agent_runtime",
                    "reason": data.get("reason") or "manual",
                    "a2a": dict(data.get("a2a") or {}),
                    "context_fit": dict(data.get("context_fit") or {}),
                    "created_at": event.get("created_at"),
                    "metadata": dict(data.get("metadata") or {}),
                }
                current["handoffs"].append(handoff)
                current["last_handoff"] = handoff
                current["last_context_fit"] = handoff.get("context_fit") or None
                if handoff["to_agent"] in AGENTS:
                    current["current_agent"] = handoff["to_agent"]
                current["current_stage"] = handoff["to_stage"]
            elif kind == "capsule.handoff_blocked":
                blocked = {
                    "handoff_id": data.get("handoff_id"),
                    "connector_id": data.get("connector_id") or capsule_id,
                    "from_agent": data.get("from_agent"),
                    "to_agent": data.get("to_agent"),
                    "reason": data.get("reason") or "context_fit_blocked",
                    "context_fit": dict(data.get("context_fit") or {}),
                    "a2a": dict(data.get("a2a") or {}),
                    "created_at": event.get("created_at"),
                }
                current["blocked_handoffs"].append(blocked)
                current["last_context_fit"] = blocked.get("context_fit") or None
            elif kind == "capsule.completed":
                current["status"] = "completed"
                current["current_stage"] = data.get("stage") or "result"
                if data.get("agent") in AGENTS:
                    current["current_agent"] = data["agent"]
                current["metadata"].update(data.get("metadata") or {})
        items = sorted(by_id.values(), key=lambda x: str(x.get("updated_at") or ""), reverse=True)
        return items[: max(1, min(limit, 100))]

    async def overview(self, limit: int = 100) -> dict[str, Any]:
        capsules = await self._projections(limit)
        active = [c for c in capsules if c.get("status") == "active"]
        handoffs = sum(len(c.get("handoffs") or []) for c in capsules)
        return {
            "capsules": capsules,
            "summary": {
                "active": len(active),
                "total": len(capsules),
                "handoffs": handoffs,
            },
        }

    async def get(self, capsule_id: str) -> dict[str, Any]:
        for item in await self._projections(100):
            if item.get("capsule_id") == capsule_id:
                return item
        raise CapsuleNotFound(capsule_id)

    async def create(
        self,
        *,
        title: str,
        workspace: str | None = None,
        source_pane: str | None = None,
        agent: str = "claude",
        metadata: dict[str, Any] | None = None,
        capsule_id: str | None = None,
    ) -> dict[str, Any]:
        if agent not in AGENTS:
            raise ValueError(f"unsupported agent: {agent}")
        capsule_id = capsule_id or f"C-{uuid4().hex[:6].upper()}"
        metadata = dict(metadata or {})
        profile_data = metadata.get("context_profile")
        if profile_data is not None:
            if not isinstance(profile_data, dict):
                raise ValueError("context_profile must be an object")
            profile = ContextProfile(
                source_tokens=int(profile_data.get("source_tokens") or 0),
                retained_tokens=int(profile_data.get("retained_tokens") or 0),
            )
            metadata["context_profile"] = profile.as_dict()
        payload = {
            "capsule_id": capsule_id,
            "title": title,
            "workspace": workspace,
            "source_pane": source_pane,
            "agent": agent,
            "stage": "ingress",
            "metadata": metadata,
        }
        await self.db.add_event("capsule.created", f"{capsule_id} created", data=payload)
        return await self.get(capsule_id)

    async def set_stage(
        self,
        capsule_id: str,
        *,
        stage: str,
        agent: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        current = await self.get(capsule_id)
        if current.get("pending_handoff"):
            raise ValueError("capsule_handoff_in_progress")
        if agent is not None and agent not in AGENTS:
            raise ValueError(f"unsupported agent: {agent}")
        if agent is not None and agent != current.get("current_agent"):
            raise ValueError("stage update cannot transfer capsule ownership; use handoff")
        await self.db.add_event(
            "capsule.stage",
            f"{capsule_id} entered {stage}",
            data={
                "capsule_id": capsule_id,
                "stage": stage,
                "agent": agent,
                "metadata": metadata or {},
            },
        )
        return await self.get(capsule_id)

    async def handoff(
        self,
        capsule_id: str,
        *,
        from_agent: str,
        to_agent: str,
        reason: str = "manual",
        from_stage: str = "agent_runtime",
        to_stage: str = "agent_runtime",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if from_agent not in AGENTS or to_agent not in AGENTS:
            raise ValueError("unsupported handoff agent")
        if from_agent == to_agent:
            raise ValueError("handoff target must differ from current agent")

        metadata = dict(metadata or {})
        async with self._handoff_lock:
            current = await self.get(capsule_id)
            if current.get("status") != "active":
                raise ValueError("capsule is not active")
            if current.get("current_agent") != from_agent:
                raise ValueError(
                    f"handoff owner mismatch: current_agent={current.get('current_agent')!r}"
                )
            if current.get("pending_handoff"):
                raise ValueError("capsule_handoff_in_progress")

            handoff_id = f"H-{uuid4().hex[:8].upper()}"
            a2a_task_id = f"A2A-{uuid4().hex[:8].upper()}"
            delivery_token = secrets.token_urlsafe(32)
            delivery_token_sha256 = hashlib.sha256(delivery_token.encode("utf-8")).hexdigest()

            profile_data = current.get("context_profile") or (current.get("metadata") or {}).get("context_profile")
            capability_data = metadata.get("model_capability")
            blocked_reason = None
            fit: dict[str, Any] = {}
            try:
                if not isinstance(profile_data, dict):
                    blocked_reason = "capsule_context_profile_required"
                    raise ValueError(blocked_reason)
                if not isinstance(capability_data, dict):
                    blocked_reason = "model_capability_required"
                    raise ValueError(blocked_reason)

                profile = ContextProfile(
                    source_tokens=int(profile_data.get("source_tokens") or 0),
                    retained_tokens=int(profile_data.get("retained_tokens") or 0),
                )
                capability = ModelCapability(
                    provider=str(capability_data.get("provider") or ""),
                    model_id=str(capability_data.get("model_id") or ""),
                    context_window=int(capability_data.get("context_window") or 0),
                    max_output_tokens=int(capability_data.get("max_output_tokens") or 0),
                    system_prompt_tokens=int(capability_data.get("system_prompt_tokens") or 0),
                    tool_schema_tokens=int(capability_data.get("tool_schema_tokens") or 0),
                    safety_reserve_tokens=int(capability_data.get("safety_reserve_tokens") or 4096),
                    last_verified_at=capability_data.get("last_verified_at"),
                )
                fit = evaluate_context_fit(profile, capability)
                if not fit["fit"]:
                    blocked_reason = "capsule_context_too_large"
                    raise ValueError(blocked_reason)
            except (TypeError, ValueError) as exc:
                if blocked_reason is None:
                    blocked_reason = f"context_fit_invalid: {exc}"
                a2a_blocked = {
                    "protocol": "a2a",
                    "task_id": a2a_task_id,
                    "context_id": capsule_id,
                    "task": {
                        "id": a2a_task_id,
                        "contextId": capsule_id,
                        "kind": "task",
                        "status": {"state": "rejected"},
                    },
                    "from_agent": from_agent,
                    "to_agent": to_agent,
                }
                await self.db.add_event(
                    "capsule.handoff_blocked",
                    f"{capsule_id} handoff blocked: {blocked_reason}",
                    severity="warning",
                    data={
                        "capsule_id": capsule_id,
                        "handoff_id": handoff_id,
                        "connector_id": capsule_id,
                        "from_agent": from_agent,
                        "to_agent": to_agent,
                        "reason": blocked_reason,
                        "context_fit": fit,
                        "a2a_task_id": a2a_task_id,
                        "a2a": a2a_blocked,
                        "metadata": metadata,
                    },
                )
                raise ValueError(blocked_reason) from exc

            await self.db.add_event(
                "capsule.handoff_requested",
                f"{capsule_id} handoff requested {from_agent} -> {to_agent}",
                data={
                    "capsule_id": capsule_id,
                    "handoff_id": handoff_id,
                    "connector_id": capsule_id,
                    "from_agent": from_agent,
                    "to_agent": to_agent,
                    "from_stage": from_stage,
                    "to_stage": to_stage,
                    "reason": reason,
                    "context_fit": fit,
                    "a2a_task_id": a2a_task_id,
                    "delivery_token_sha256": delivery_token_sha256,
                    "metadata": metadata,
                },
            )

        async def fail_delivery(
            error: str,
            *,
            uncertain: bool = False,
            reason_code: str = "delivery_failed",
        ) -> None:
            kind = "capsule.handoff_dispatch_uncertain" if uncertain else "capsule.handoff_failed"
            await self.db.add_event(
                kind,
                f"{capsule_id} handoff delivery {'uncertain' if uncertain else 'failed'}: {error}",
                severity="warning",
                data={
                    "capsule_id": capsule_id,
                    "handoff_id": handoff_id,
                    "from_agent": from_agent,
                    "to_agent": to_agent,
                    "reason": "delivery_failed",
                    "error": error,
                },
            )

        if self.herdr is None:
            await fail_delivery("herdr_not_configured")
            raise CapsuleDeliveryError("herdr_not_configured")

        snapshot = await self.herdr.refresh()
        if snapshot.get("status") == "down":
            error = f"herdr_unavailable: {snapshot.get('error') or 'unknown'}"
            await fail_delivery(error)
            raise CapsuleDeliveryError(error)

        requested_pane = str(metadata.get("target_pane") or "").strip()
        if requested_pane:
            pane = self.herdr.find_pane(pane_id=requested_pane)
            if not pane or pane.get("pane_id") != requested_pane:
                pane = None
            elif pane.get("agent") and pane.get("agent") != to_agent:
                pane = None
        else:
            pane = self.herdr.find_pane(agent=to_agent)
        if not pane:
            await fail_delivery("target_pane_not_found")
            raise CapsuleDeliveryError("target_pane_not_found")

        tool = self.herdr.tool("herdr_prompt_agent")
        if not tool:
            await fail_delivery("herdr_prompt_agent_missing")
            raise CapsuleDeliveryError("herdr_prompt_agent_missing")

        ack_url = (
            f"{self.local_api_base}/api/capsules/{capsule_id}"
            f"/handoff/{handoff_id}/ack"
        )
        ack_body = json.dumps(
            {
                "agent": to_agent,
                "delivery_token": delivery_token,
                "receipt": {
                    "transport": "herdr",
                    "pane_id": pane.get("pane_id"),
                    "a2a_task_id": a2a_task_id,
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        capsule_payload = json.dumps(
            {
                "capsule_id": capsule_id,
                "title": current.get("title"),
                "workspace": current.get("workspace"),
                "from_agent": from_agent,
                "to_agent": to_agent,
                "from_stage": from_stage,
                "to_stage": to_stage,
                "reason": reason,
                "a2a_task_id": a2a_task_id,
                "context_profile": current.get("context_profile"),
                "metadata": current.get("metadata") or {},
            },
            ensure_ascii=False,
            indent=2,
        )
        prompt = (
            "HIRDA CAPSULE DELIVERY — physical handoff\n\n"
            f"You are the target agent for capsule {capsule_id}.\n"
            "Before doing any capsule work, acknowledge physical receipt by running EXACTLY this local command:\n\n"
            f"curl -fsS -X POST '{ack_url}' -H 'Content-Type: application/json' --data '{ack_body}'\n\n"
            "The handoff is NOT committed until that ACK succeeds. "
            "If ACK fails, do not claim ownership.\n\n"
            "Capsule payload:\n"
            f"{capsule_payload}\n"
        )

        try:
            args = build_tool_arguments(
                tool,
                pane=pane,
                pane_id=pane.get("pane_id"),
                agent=to_agent,
                prompt=prompt,
                timeout_seconds=5,
            )
            input_schema = tool.get("inputSchema") or tool.get("input_schema") or {}
            properties = input_schema.get("properties") if isinstance(input_schema, dict) else {}
            if isinstance(properties, dict) and "wait" in properties:
                args["wait"] = False
            await self.herdr.client().call_tool(
                "herdr_prompt_agent",
                args,
                client_name=f"hirda-capsule-{handoff_id.lower()}",
            )
        except Exception as exc:
            failure = classify_failure(exc)
            uncertain = failure == "dispatch_uncertain"
            await fail_delivery(str(exc), uncertain=uncertain)
            raise CapsuleDeliveryError(
                "handoff_dispatch_uncertain" if uncertain else f"handoff_dispatch_failed: {exc}"
            ) from exc

        a2a = {
            "protocol": "a2a",
            "task_id": a2a_task_id,
            "context_id": capsule_id,
            "task": {
                "id": a2a_task_id,
                "contextId": capsule_id,
                "kind": "task",
                "status": {"state": "submitted"},
            },
            "from_agent": from_agent,
            "to_agent": to_agent,
        }
        await self.db.add_event(
            "capsule.handoff_dispatched",
            f"{capsule_id} dispatched to {to_agent} on {pane.get('pane_id')}",
            data={
                "capsule_id": capsule_id,
                "handoff_id": handoff_id,
                "from_agent": from_agent,
                "to_agent": to_agent,
                "target_pane": pane.get("pane_id"),
                "a2a": a2a,
            },
        )
        return await self.get(capsule_id)

    async def acknowledge_handoff(
        self,
        capsule_id: str,
        handoff_id: str,
        *,
        agent: str,
        delivery_token: str,
        receipt: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if agent not in AGENTS:
            raise ValueError("unsupported handoff agent")
        if not delivery_token:
            raise ValueError("delivery_token is required")

        async with self._handoff_lock:
            current = await self.get(capsule_id)
            requested = None
            already_committed = None
            terminal_failure = None
            for event in current.get("events") or []:
                data = event.get("data") or {}
                if data.get("handoff_id") != handoff_id:
                    continue
                if event.get("kind") == "capsule.handoff_requested":
                    requested = data
                elif event.get("kind") == "capsule.handoff_committed":
                    already_committed = data
                elif event.get("kind") == "capsule.handoff_failed":
                    terminal_failure = data

            if requested is None:
                raise ValueError("handoff request not found")
            if agent != requested.get("to_agent"):
                raise ValueError("handoff ACK agent mismatch")

            expected_hash = str(requested.get("delivery_token_sha256") or "")
            actual_hash = hashlib.sha256(delivery_token.encode("utf-8")).hexdigest()
            if not expected_hash or not secrets.compare_digest(expected_hash, actual_hash):
                raise ValueError("invalid handoff delivery token")

            if already_committed is not None:
                return current
            if terminal_failure is not None:
                raise ValueError("handoff already failed")
            if current.get("current_agent") != requested.get("from_agent"):
                raise ValueError("handoff source no longer owns capsule")

            receipt = dict(receipt or {})
            common = {
                "capsule_id": capsule_id,
                "handoff_id": handoff_id,
                "connector_id": capsule_id,
                "from_agent": requested.get("from_agent"),
                "to_agent": requested.get("to_agent"),
                "from_stage": requested.get("from_stage"),
                "to_stage": requested.get("to_stage"),
                "reason": requested.get("reason"),
                "context_fit": dict(requested.get("context_fit") or {}),
                "metadata": dict(requested.get("metadata") or {}),
                "target_pane": receipt.get("pane_id"),
                "receipt": receipt,
            }
            await self.db.add_event(
                "capsule.handoff_acknowledged",
                f"{capsule_id} receipt acknowledged by {agent}",
                data=common,
            )
            a2a = {
                "protocol": "a2a",
                "task_id": requested.get("a2a_task_id"),
                "context_id": capsule_id,
                "task": {
                    "id": requested.get("a2a_task_id"),
                    "contextId": capsule_id,
                    "kind": "task",
                    "status": {"state": "working"},
                },
                "from_agent": requested.get("from_agent"),
                "to_agent": requested.get("to_agent"),
            }
            await self.db.add_event(
                "capsule.handoff_committed",
                f"{capsule_id} ownership committed to {agent}",
                data={**common, "a2a": a2a},
            )
            return await self.get(capsule_id)

    async def complete(
        self,
        capsule_id: str,
        *,
        agent: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        current = await self.get(capsule_id)
        if current.get("pending_handoff"):
            raise ValueError("capsule_handoff_in_progress")
        if agent is not None and agent not in AGENTS:
            raise ValueError(f"unsupported agent: {agent}")
        if agent is not None and agent != current.get("current_agent"):
            raise ValueError("completion agent does not own capsule")
        await self.db.add_event(
            "capsule.completed",
            f"{capsule_id} completed",
            data={
                "capsule_id": capsule_id,
                "agent": agent,
                "stage": "result",
                "metadata": metadata or {},
            },
        )
        return await self.get(capsule_id)
