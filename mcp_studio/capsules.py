from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .db import Database
from .context_fit import ContextProfile, ModelCapability, evaluate_context_fit

AGENTS = {"claude", "codex", "hermes"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CapsuleNotFound(KeyError):
    pass


class CapsuleService:
    """Append-only capsule projection backed by the existing Studio event ledger."""

    def __init__(self, db: Database):
        self.db = db

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
                    "handoffs": [],
                    "blocked_handoffs": [],
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
        await self.get(capsule_id)
        if agent is not None and agent not in AGENTS:
            raise ValueError(f"unsupported agent: {agent}")
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
        current = await self.get(capsule_id)
        if from_agent not in AGENTS or to_agent not in AGENTS:
            raise ValueError("unsupported handoff agent")

        metadata = dict(metadata or {})
        handoff_id = f"H-{uuid4().hex[:8].upper()}"
        a2a_task_id = f"A2A-{uuid4().hex[:8].upper()}"
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
            a2a_blocked = {**a2a, "task": {**a2a["task"], "status": {"state": "rejected"}}}
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
                    "a2a": a2a_blocked,
                    "metadata": metadata,
                },
            )
            raise ValueError(blocked_reason) from exc

        await self.db.add_event(
            "capsule.handoff",
            f"{capsule_id} {from_agent} -> {to_agent}",
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
                "a2a": a2a,
                "metadata": metadata,
            },
        )
        return await self.get(capsule_id)

    async def complete(
        self,
        capsule_id: str,
        *,
        agent: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        await self.get(capsule_id)
        if agent is not None and agent not in AGENTS:
            raise ValueError(f"unsupported agent: {agent}")
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
