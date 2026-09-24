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
from .context_projector import ContextProjector
from .execution import build_tool_arguments, classify_failure
from .herdr import HerdrManager, _decode_text_content

AGENTS = {"claude", "codex", "hermes"}
LANE_STATES = {"normal", "draining", "disabled", "emergency"}
PORTABILITY_MODES = {"safe", "guarded", "pinned"}


def _clean_string_list(value: Any, *, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a list")
    result = [str(item).strip() for item in value if str(item).strip()]
    return result


def normalize_capsule_contract(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("capsule_contract must be an object")

    objective = str(value.get("objective") or "").strip()
    if not objective:
        raise ValueError("capsule_contract.objective is required")

    portability = str(value.get("portability") or "guarded").strip().casefold()
    if portability not in PORTABILITY_MODES:
        raise ValueError("capsule_contract.portability must be safe, guarded, or pinned")

    handoff_checks = _clean_string_list(value.get("handoff_checks"), field_name="capsule_contract.handoff_checks")
    acceptance = _clean_string_list(
        value.get("acceptance_criteria"),
        field_name="capsule_contract.acceptance_criteria",
    )
    if portability in {"safe", "guarded"} and not handoff_checks:
        raise ValueError("safe/guarded capsule_contract requires at least one handoff_check")

    target_capabilities = value.get("target_capabilities") or {}
    if not isinstance(target_capabilities, dict):
        raise ValueError("capsule_contract.target_capabilities must be an object")

    normalized_targets: dict[str, dict[str, Any]] = {}
    for agent, capability in target_capabilities.items():
        key = str(agent).strip().casefold()
        if key not in AGENTS:
            raise ValueError(f"unsupported capsule_contract target agent: {agent}")
        if not isinstance(capability, dict):
            raise ValueError(f"target capability for {key} must be an object")
        normalized_targets[key] = dict(capability)

    current_state = value.get("current_state") or {}
    checkpoint = value.get("checkpoint") or {}
    if not isinstance(current_state, dict):
        raise ValueError("capsule_contract.current_state must be an object")
    if not isinstance(checkpoint, dict):
        raise ValueError("capsule_contract.checkpoint must be an object")

    return {
        "schema": "hirda-capsule-contract-v1",
        "objective": objective,
        "locked_decisions": _clean_string_list(
            value.get("locked_decisions"),
            field_name="capsule_contract.locked_decisions",
        ),
        "artifacts": _clean_string_list(value.get("artifacts"), field_name="capsule_contract.artifacts"),
        "current_state": dict(current_state),
        "checkpoint": dict(checkpoint),
        "next_actions": _clean_string_list(
            value.get("next_actions"),
            field_name="capsule_contract.next_actions",
        ),
        "acceptance_criteria": acceptance,
        "handoff_checks": handoff_checks,
        "constraints": _clean_string_list(
            value.get("constraints"),
            field_name="capsule_contract.constraints",
        ),
        "portability": portability,
        "target_capabilities": normalized_targets,
        "updated_at": _now(),
    }


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
        self.context_projector = ContextProjector(db)
        self.local_api_base = local_api_base.rstrip("/")
        self.handoff_ack_timeout_seconds = max(1.0, float(handoff_ack_timeout_seconds))
        self.handoff_ack_poll_seconds = max(0.1, float(handoff_ack_poll_seconds))
        self._handoff_lock = asyncio.Lock()

    async def _ledger(
        self,
        limit: int = 500,
        *,
        capsule_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if hasattr(self.db, "list_events"):
            if capsule_id:
                return await self.db.list_events(
                    stream_id=f"capsule:{capsule_id}",
                    kind_prefix="capsule.",
                    limit=None,
                    ascending=True,
                )
            return await self.db.list_events(
                stream_prefix="capsule:",
                kind_prefix="capsule.",
                limit=None,
                ascending=True,
            )

        events = await self.db.recent_events(max(1, min(limit, 500)))
        ordered = [
            e
            for e in reversed(events)
            if str(e.get("kind", "")).startswith("capsule.")
        ]
        if capsule_id:
            ordered = [
                e
                for e in ordered
                if str((e.get("data") or {}).get("capsule_id") or "") == capsule_id
            ]
        return ordered

    async def _projections(
        self,
        limit: int = 100,
        *,
        capsule_id: str | None = None,
    ) -> list[dict[str, Any]]:
        by_id: dict[str, dict[str, Any]] = {}
        for event in await self._ledger(500, capsule_id=capsule_id):
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
                    "contract": None,
                    "portability": "pinned",
                    "handoff_review": None,
                },
            )
            kind = str(event.get("kind") or "")
            current["updated_at"] = event.get("created_at")
            current["events"].append(
                {
                    "id": event.get("id"),
                    "event_id": event.get("event_id"),
                    "stream_id": event.get("stream_id"),
                    "stream_seq": event.get("stream_seq"),
                    "correlation_id": event.get("correlation_id"),
                    "event_hash": event.get("event_hash"),
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
                contract_data = current["metadata"].get("contract")
                if isinstance(contract_data, dict):
                    current["contract"] = dict(contract_data)
                    current["portability"] = str(contract_data.get("portability") or "pinned")
            elif kind == "capsule.contract_updated":
                contract_data = data.get("contract")
                if isinstance(contract_data, dict):
                    current["contract"] = dict(contract_data)
                    current["portability"] = str(contract_data.get("portability") or "pinned")
                    current["metadata"]["contract"] = dict(contract_data)
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
                    "correlation": dict(data.get("correlation") or {}),
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
                    pending["correlation"] = dict(
                        data.get("correlation") or pending.get("correlation") or {}
                    )
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
            elif kind == "capsule.handoff_validated":
                pending = current.get("pending_handoff")
                if isinstance(pending, dict) and pending.get("handoff_id") == data.get("handoff_id"):
                    pending["validation"] = dict(data.get("validation") or {})
                    pending["validated_at"] = event.get("created_at")
                    metadata = pending.get("metadata") or {}
                    pending["state"] = "awaiting_approval" if metadata.get("approval_required") else "validated"
                    if metadata.get("approval_required"):
                        current["handoff_review"] = {
                            "handoff_id": data.get("handoff_id"),
                            "state": "awaiting_approval",
                            "validation": dict(data.get("validation") or {}),
                        }
            elif kind == "capsule.handoff_validation_failed":
                pending = current.get("pending_handoff")
                if isinstance(pending, dict) and pending.get("handoff_id") == data.get("handoff_id"):
                    pending["state"] = "validation_failed"
                    pending["validation"] = dict(data.get("validation") or {})
            elif kind == "capsule.handoff_approved":
                pending = current.get("pending_handoff")
                if isinstance(pending, dict) and pending.get("handoff_id") == data.get("handoff_id"):
                    pending["approved_at"] = event.get("created_at")
                    pending["approved_by"] = data.get("approved_by")
                    pending["state"] = "approved"
                    current["handoff_review"] = None
            elif kind == "capsule.auto_handoff_blocked":
                current["handoff_review"] = {
                    "state": "blocked",
                    "reason": data.get("reason"),
                    "target_agent": data.get("to_agent"),
                    "created_at": event.get("created_at"),
                }
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
                    "correlation": dict(data.get("correlation") or {}),
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
                current["handoff_review"] = None
                current["last_context_fit"] = handoff.get("context_fit") or None
                if handoff["to_agent"] in AGENTS:
                    current["current_agent"] = handoff["to_agent"]
                current["current_stage"] = handoff["to_stage"]
            elif kind == "capsule.handoff_completed":
                for handoff in reversed(current["handoffs"]):
                    if handoff.get("handoff_id") != data.get("handoff_id"):
                        continue
                    handoff["completion"] = {
                        "state": "completed",
                        "sentinel_verified": bool(data.get("sentinel_verified")),
                        "correlation": dict(data.get("correlation") or {}),
                        "receipt": dict(data.get("receipt") or {}),
                    }
                    handoff["completed_at"] = event.get("created_at")
                    break
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
        for item in await self._projections(1, capsule_id=capsule_id):
            if item.get("capsule_id") == capsule_id:
                return item
        raise CapsuleNotFound(capsule_id)

    async def context_projection(self, capsule_id: str) -> dict[str, Any]:
        try:
            return await self.context_projector.project_capsule(capsule_id)
        except KeyError as exc:
            raise CapsuleNotFound(capsule_id) from exc

    async def compact_context(self, capsule_id: str) -> dict[str, Any]:
        try:
            return await self.context_projector.compact_capsule(capsule_id)
        except KeyError as exc:
            raise CapsuleNotFound(capsule_id) from exc

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
        contract_data = metadata.get("contract")
        if contract_data is not None:
            metadata["contract"] = normalize_capsule_contract(contract_data)
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

    async def update_contract(self, capsule_id: str, contract: dict[str, Any]) -> dict[str, Any]:
        current = await self.get(capsule_id)
        if current.get("status") != "active":
            raise ValueError("capsule is not active")
        normalized = normalize_capsule_contract(contract)
        await self.db.add_event(
            "capsule.contract_updated",
            f"{capsule_id} contract updated",
            data={"capsule_id": capsule_id, "contract": normalized},
        )
        return await self.get(capsule_id)

    async def lane_states(self) -> dict[str, dict[str, Any]]:
        raw_rows = await self.db.list_lane_states() if hasattr(self.db, "list_lane_states") else []
        rows = {str(row.get("agent") or ""): dict(row) for row in raw_rows}
        return {
            agent: rows.get(
                agent,
                {
                    "agent": agent,
                    "state": "normal",
                    "reason": None,
                    "actor": "default",
                    "updated_at": None,
                },
            )
            for agent in sorted(AGENTS)
        }

    async def set_lane_state(
        self,
        agent: str,
        state: str,
        *,
        reason: str | None = None,
        actor: str = "operator",
        auto_handoff: bool = True,
    ) -> dict[str, Any]:
        agent = str(agent or "").strip().casefold()
        state = str(state or "").strip().casefold()
        if agent not in AGENTS:
            raise ValueError(f"unsupported agent: {agent}")
        if state not in LANE_STATES:
            raise ValueError("lane state must be normal, draining, disabled, or emergency")

        previous = (await self.lane_states())[agent]
        current = await self.db.set_lane_state(agent, state, reason=reason, actor=actor)
        await self.db.add_event(
            "lane.state_changed",
            f"{agent} lane {previous.get('state')} -> {state}",
            severity="warning" if state in {"disabled", "emergency"} else "info",
            data={
                "agent": agent,
                "previous_state": previous.get("state"),
                "state": state,
                "reason": reason,
                "actor": actor,
                "auto_handoff": bool(auto_handoff),
            },
        )

        handoff_results: list[dict[str, Any]] = []
        if auto_handoff and state in {"draining", "disabled", "emergency"}:
            handoff_results = await self.auto_handoff_from_lane(agent, reason=reason or state)
        return {
            "lane": current,
            "previous": previous,
            "auto_handoff": handoff_results,
        }

    @staticmethod
    def _candidate_order(source_agent: str) -> tuple[str, ...]:
        if source_agent == "claude":
            return ("hermes", "codex")
        if source_agent == "hermes":
            return ("claude", "codex")
        return ("claude", "hermes")

    @staticmethod
    def _auto_target_context_fit(
        capsule: dict[str, Any],
        capability_data: dict[str, Any],
    ) -> dict[str, Any]:
        profile_data = capsule.get("context_profile") or (
            capsule.get("metadata") or {}
        ).get("context_profile")
        if not isinstance(profile_data, dict):
            return {
                "fit": False,
                "reason": "capsule_context_profile_required",
                "context_fit": {},
            }
        try:
            profile = ContextProfile(
                source_tokens=int(profile_data.get("source_tokens") or 0),
                retained_tokens=int(profile_data.get("retained_tokens") or 0),
            )
            capability = ModelCapability(
                provider=str(capability_data.get("provider") or ""),
                model_id=str(capability_data.get("model_id") or ""),
                context_window=int(capability_data.get("context_window") or 0),
                max_output_tokens=int(capability_data.get("max_output_tokens") or 0),
                system_prompt_tokens=int(
                    capability_data.get("system_prompt_tokens") or 0
                ),
                tool_schema_tokens=int(
                    capability_data.get("tool_schema_tokens") or 0
                ),
                safety_reserve_tokens=int(
                    capability_data.get("safety_reserve_tokens") or 4096
                ),
                last_verified_at=capability_data.get("last_verified_at"),
            )
            fit = evaluate_context_fit(profile, capability)
        except (TypeError, ValueError) as exc:
            return {
                "fit": False,
                "reason": f"context_fit_invalid: {exc}",
                "context_fit": {},
            }
        return {
            "fit": bool(fit.get("fit")),
            "reason": None if fit.get("fit") else "capsule_context_too_large",
            "context_fit": fit,
        }

    def _available_target_pane(
        self,
        target_agent: str,
        snapshot: dict[str, Any],
        *,
        workspace: str | None = None,
    ) -> dict[str, Any] | None:
        if self.herdr is None:
            return None
        panes = self.herdr.pane_list(snapshot)
        prefixes = ("hirda-certification", "hirda-live-certification")
        candidates = []
        for pane in panes:
            if str(pane.get("agent") or "").casefold() != target_agent:
                continue
            name = str(pane.get("name") or "").strip().casefold()
            label = str(pane.get("label") or "").strip().casefold()
            if name.startswith(prefixes) or label.startswith(prefixes):
                continue
            if str(pane.get("agent_status") or "").casefold() not in {"idle", "done"}:
                continue
            if workspace:
                pane_workspace = str(
                    pane.get("foreground_cwd") or pane.get("cwd") or ""
                ).strip()
                if not pane_workspace or pane_workspace != str(workspace).strip():
                    continue
            candidates.append(dict(pane))
        if not candidates:
            return None
        return sorted(candidates, key=lambda item: str(item.get("pane_id") or ""))[0]

    async def auto_handoff_from_lane(
        self,
        source_agent: str,
        *,
        reason: str = "lane_unavailable",
    ) -> list[dict[str, Any]]:
        source_agent = str(source_agent or "").strip().casefold()
        if source_agent not in AGENTS:
            raise ValueError(f"unsupported agent: {source_agent}")

        lane_states = await self.lane_states()
        snapshot = await self.herdr.refresh() if self.herdr is not None else {"status": "down"}
        active = [
            capsule
            for capsule in (await self._projections(100))
            if capsule.get("status") == "active"
            and capsule.get("current_agent") == source_agent
            and not capsule.get("pending_handoff")
        ]
        results: list[dict[str, Any]] = []

        for capsule in active:
            capsule_id = str(capsule.get("capsule_id") or "")
            contract = capsule.get("contract")
            if not isinstance(contract, dict):
                await self.db.add_event(
                    "capsule.auto_handoff_blocked",
                    f"{capsule_id} auto handoff blocked: capsule_contract_required",
                    severity="warning",
                    data={
                        "capsule_id": capsule_id,
                        "from_agent": source_agent,
                        "reason": "capsule_contract_required",
                    },
                )
                results.append({"capsule_id": capsule_id, "status": "blocked", "reason": "capsule_contract_required"})
                continue

            portability = str(contract.get("portability") or "pinned").casefold()
            if portability == "pinned":
                await self.db.add_event(
                    "capsule.auto_handoff_blocked",
                    f"{capsule_id} auto handoff blocked: capsule_pinned",
                    severity="warning",
                    data={
                        "capsule_id": capsule_id,
                        "from_agent": source_agent,
                        "reason": "capsule_pinned",
                    },
                )
                results.append({"capsule_id": capsule_id, "status": "blocked", "reason": "capsule_pinned"})
                continue

            source_lane_state = str(
                (lane_states.get(source_agent) or {}).get("state") or "normal"
            )
            current_stage = str(capsule.get("current_stage") or "agent_runtime")
            await self.db.add_event(
                "capsule.auto_handoff_started",
                f"{capsule_id} automatic lane rollover started from {source_agent}",
                severity="warning" if source_lane_state in {"disabled", "emergency"} else "info",
                data={
                    "capsule_id": capsule_id,
                    "from_agent": source_agent,
                    "source_lane_state": source_lane_state,
                    "stage": current_stage,
                    "portability": portability,
                    "reason": reason,
                },
            )

            selected_agent = None
            selected_pane = None
            selected_capability = None
            selected_fit: dict[str, Any] | None = None
            candidate_evaluations: list[dict[str, Any]] = []
            capabilities = contract.get("target_capabilities") or {}
            for target in self._candidate_order(source_agent):
                target_state = str(
                    (lane_states.get(target) or {}).get("state") or "normal"
                )
                if target_state != "normal":
                    candidate_evaluations.append(
                        {
                            "agent": target,
                            "eligible": False,
                            "reason": f"lane_{target_state}",
                        }
                    )
                    continue
                capability = (
                    capabilities.get(target)
                    if isinstance(capabilities, dict)
                    else None
                )
                if not isinstance(capability, dict):
                    candidate_evaluations.append(
                        {
                            "agent": target,
                            "eligible": False,
                            "reason": "verified_target_capability_required",
                        }
                    )
                    continue
                pane = self._available_target_pane(
                    target,
                    snapshot,
                    workspace=str(capsule.get("workspace") or "").strip() or None,
                )
                if pane is None:
                    candidate_evaluations.append(
                        {
                            "agent": target,
                            "eligible": False,
                            "reason": "idle_target_pane_in_workspace_required",
                        }
                    )
                    continue
                fit_result = self._auto_target_context_fit(capsule, capability)
                candidate_evaluations.append(
                    {
                        "agent": target,
                        "pane_id": pane.get("pane_id"),
                        "eligible": bool(fit_result.get("fit")),
                        "reason": fit_result.get("reason"),
                        "context_fit": fit_result.get("context_fit") or {},
                    }
                )
                if not fit_result.get("fit"):
                    continue
                selected_agent = target
                selected_pane = pane
                selected_capability = capability
                selected_fit = dict(fit_result.get("context_fit") or {})
                break

            if selected_agent is None or selected_pane is None or selected_capability is None:
                await self.db.add_event(
                    "capsule.auto_handoff_blocked",
                    f"{capsule_id} auto handoff blocked: no_compatible_target",
                    severity="warning",
                    data={
                        "capsule_id": capsule_id,
                        "from_agent": source_agent,
                        "source_lane_state": source_lane_state,
                        "reason": "no_compatible_target",
                        "candidates": candidate_evaluations,
                    },
                )
                results.append(
                    {
                        "capsule_id": capsule_id,
                        "status": "blocked",
                        "reason": "no_compatible_target",
                        "candidates": candidate_evaluations,
                    }
                )
                continue

            handoff_metadata = {
                "model_capability": dict(selected_capability),
                "target_pane": selected_pane.get("pane_id"),
                "requires_validation": True,
                "approval_required": portability == "guarded",
                "handoff_mode": f"{portability}_auto",
                "contract": contract,
                "auto_failover": {
                    "source_lane_state": source_lane_state,
                    "trigger_reason": reason,
                    "source_stage": current_stage,
                    "selected_target": selected_agent,
                    "selected_pane": selected_pane.get("pane_id"),
                    "candidate_evaluations": candidate_evaluations,
                },
            }
            try:
                state = await self.handoff(
                    capsule_id,
                    from_agent=source_agent,
                    to_agent=selected_agent,
                    reason=f"lane_{source_lane_state}:{reason}",
                    from_stage=current_stage,
                    to_stage=current_stage,
                    metadata=handoff_metadata,
                )
            except (ValueError, CapsuleDeliveryError) as exc:
                await self.db.add_event(
                    "capsule.auto_handoff_blocked",
                    f"{capsule_id} auto handoff failed: {exc}",
                    severity="warning",
                    data={
                        "capsule_id": capsule_id,
                        "from_agent": source_agent,
                        "to_agent": selected_agent,
                        "source_lane_state": source_lane_state,
                        "reason": str(exc),
                        "candidates": candidate_evaluations,
                    },
                )
                results.append(
                    {
                        "capsule_id": capsule_id,
                        "status": "blocked",
                        "reason": str(exc),
                        "candidates": candidate_evaluations,
                    }
                )
                continue

            pending = state.get("pending_handoff") or {}
            await self.db.add_event(
                "capsule.auto_handoff_dispatched",
                f"{capsule_id} automatic lane rollover dispatched to {selected_agent}",
                data={
                    "capsule_id": capsule_id,
                    "handoff_id": pending.get("handoff_id"),
                    "from_agent": source_agent,
                    "to_agent": selected_agent,
                    "source_lane_state": source_lane_state,
                    "from_stage": current_stage,
                    "to_stage": current_stage,
                    "target_pane": selected_pane.get("pane_id"),
                    "portability": portability,
                    "context_fit": selected_fit or {},
                    "candidates": candidate_evaluations,
                },
            )
            results.append(
                {
                    "capsule_id": capsule_id,
                    "status": "dispatched",
                    "to_agent": selected_agent,
                    "portability": portability,
                    "source_lane_state": source_lane_state,
                    "stage": current_stage,
                    "context_fit": selected_fit or {},
                    "pending_handoff": pending,
                }
            )
        return results

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
        target_lane = (await self.lane_states()).get(to_agent) or {}
        if str(target_lane.get("state") or "normal") != "normal" and not bool(metadata.get("override_lane_state")):
            raise ValueError(f"target_lane_not_accepting_work:{target_lane.get('state')}")

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

            requires_validation = bool(metadata.get("requires_validation"))
            if requires_validation:
                contract_data = metadata.get("contract") or current.get("contract")
                if not isinstance(contract_data, dict):
                    raise ValueError("capsule_contract_required")
                normalized_contract = normalize_capsule_contract(contract_data)
                metadata["contract"] = normalized_contract
                if not normalized_contract.get("handoff_checks"):
                    raise ValueError("capsule_handoff_checks_required")

            handoff_id = f"H-{uuid4().hex[:8].upper()}"
            a2a_task_id = f"A2A-{uuid4().hex[:8].upper()}"
            turn_id = str(metadata.get("turn_id") or f"T-{uuid4().hex[:12].upper()}")
            source_session = str(
                metadata.get("source_session")
                or current.get("source_pane")
                or f"{capsule_id}:{from_agent}"
            )
            correlation = {
                "schema": "hirda-handoff-correlation-v1",
                "handoff_id": handoff_id,
                "capsule_id": capsule_id,
                "turn_id": turn_id,
                "source_session": source_session,
                "target_session": None,
                "generation": f"G-{uuid4().hex[:12].upper()}",
                "from_agent": from_agent,
                "to_agent": to_agent,
            }
            delivery_token = secrets.token_urlsafe(32)
            delivery_token_sha256 = hashlib.sha256(delivery_token.encode("utf-8")).hexdigest()
            completion_nonce = secrets.token_urlsafe(18)
            completion_sentinel = (
                f"HIRDA_END:{handoff_id}:{turn_id}:{completion_nonce}"
            )
            completion_sentinel_sha256 = hashlib.sha256(
                completion_sentinel.encode("utf-8")
            ).hexdigest()

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
                        "correlation": correlation,
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
                    "completion_sentinel_sha256": completion_sentinel_sha256,
                    "correlation": correlation,
                    "metadata": metadata,
                },
            )
            context_projection = await self.context_projector.project_capsule(
                capsule_id
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

        dispatch_correlation = {
            **correlation,
            "target_session": str(pane.get("pane_id") or ""),
        }

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
                    "correlation": dispatch_correlation,
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
                "handoff_correlation": dispatch_correlation,
                "context_projection": context_projection,
                "context_projection_sha256": context_projection["projection_sha256"],
                "context_profile": current.get("context_profile"),
                "contract": metadata.get("contract") or current.get("contract"),
                "handoff_policy": {
                    "requires_validation": bool(metadata.get("requires_validation")),
                    "approval_required": bool(metadata.get("approval_required")),
                    "mode": metadata.get("handoff_mode") or "manual",
                },
                "metadata": current.get("metadata") or {},
            },
            ensure_ascii=False,
            indent=2,
        )
        validation_url = (
            f"{self.local_api_base}/api/capsules/{capsule_id}"
            f"/handoff/{handoff_id}/validate"
        )
        completion_url = (
            f"{self.local_api_base}/api/capsules/{capsule_id}"
            f"/handoff/{handoff_id}/complete"
        )
        completion_body = json.dumps(
            {
                "agent": to_agent,
                "delivery_token": delivery_token,
                "sentinel": completion_sentinel,
                "receipt": {
                    "transport": "herdr",
                    "pane_id": pane.get("pane_id"),
                    "a2a_task_id": a2a_task_id,
                    "correlation": dispatch_correlation,
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        validation_note = ""
        if metadata.get("requires_validation"):
            validation_note = (
                "\nAfter ACK, you still do NOT own the capsule. Validate the Capsule Contract before changing "
                "its locked decisions. Run every handoff_check against the current workspace/state. "
                "Then POST validation evidence to:\n"
                f"{validation_url}\n"
                "JSON fields: agent, delivery_token, passed, checks. "
                "checks must contain one object per exact handoff_check with criterion, status='pass', and evidence. "
                "For guarded handoff also include a concise plan. Ownership remains with the source until HIRDA "
                "records validation and, when required, operator approval.\n"
                f"Use this delivery_token only for ACK/validation: {delivery_token}\n"
            )

        handoff_mode = str(metadata.get("handoff_mode") or "manual")
        auto_rollover_note = ""
        if handoff_mode.endswith("_auto"):
            if metadata.get("approval_required"):
                auto_rollover_note = (
                    "\nThis is an AUTOMATIC LANE ROLLOVER with guarded portability. "
                    "After ACK and validation, STOP and wait for HIRDA approval. Do not continue capsule work, "
                    "change locked decisions, or send the completion callback until HIRDA tells you approval "
                    "was granted and ownership is committed. After the resume notice, continue from the delivered "
                    "context_projection without restarting completed work.\n"
                )
            else:
                auto_rollover_note = (
                    "\nThis is an AUTOMATIC LANE ROLLOVER. After ACK and required validation succeed and "
                    "HIRDA commits ownership to you, continue exactly from context_projection.context.runtime.current_stage "
                    "and context_projection.context.next_actions. Preserve objective, locked_decisions, constraints, "
                    "artifacts, and checkpoint. Do not restart work that the source lane already completed.\n"
                )
        prompt = (
            "HIRDA CAPSULE DELIVERY — physical handoff\n\n"
            f"You are the target agent for capsule {capsule_id}.\n"
            "Before doing any capsule work, acknowledge physical receipt by running EXACTLY this local command:\n\n"
            f"curl -fsS -X POST '{ack_url}' -H 'Content-Type: application/json' --data '{ack_body}'\n\n"
            "If ACK fails, do not claim ownership. "
            "A validation-gated handoff is NOT committed by ACK alone.\n"
            f"{validation_note}"
            f"{auto_rollover_note}\n"
            "Do not use silence or an idle timeout as proof that this turn is complete. "
            "When all work for this handoff turn is fully complete, report the explicit "
            "per-turn completion sentinel by running EXACTLY this local command:\n\n"
            f"curl -fsS -X POST '{completion_url}' -H 'Content-Type: application/json' --data '{completion_body}'\n\n"
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
                "correlation": dispatch_correlation,
                "context_projection_sha256": context_projection["projection_sha256"],
                "context_projection_stream_seq": context_projection["through_stream_seq"],
                "context_projection_version": context_projection["projection_version"],
                "a2a": a2a,
            },
        )
        return await self.get(capsule_id)

    @staticmethod
    def _handoff_event(
        current: dict[str, Any],
        handoff_id: str,
        kind: str,
    ) -> dict[str, Any] | None:
        for event in current.get("events") or []:
            data = event.get("data") or {}
            if data.get("handoff_id") == handoff_id and event.get("kind") == kind:
                return dict(data)
        return None

    @staticmethod
    def _verify_delivery_token(requested: dict[str, Any], delivery_token: str) -> None:
        if not delivery_token:
            raise ValueError("delivery_token is required")
        expected_hash = str(requested.get("delivery_token_sha256") or "")
        actual_hash = hashlib.sha256(delivery_token.encode("utf-8")).hexdigest()
        if not expected_hash or not secrets.compare_digest(expected_hash, actual_hash):
            raise ValueError("invalid handoff delivery token")

    @staticmethod
    def _verify_handoff_correlation(
        expected: dict[str, Any],
        receipt: dict[str, Any],
    ) -> dict[str, Any]:
        if not expected:
            return {}
        actual = receipt.get("correlation")
        if not isinstance(actual, dict):
            raise ValueError("handoff ACK correlation missing")
        fields = (
            "schema",
            "handoff_id",
            "capsule_id",
            "turn_id",
            "source_session",
            "target_session",
            "generation",
            "from_agent",
            "to_agent",
        )
        mismatched = [
            field
            for field in fields
            if actual.get(field) != expected.get(field)
        ]
        if mismatched:
            raise ValueError(
                "handoff ACK correlation mismatch: " + ",".join(mismatched)
            )
        return {field: actual.get(field) for field in fields}

    @staticmethod
    def _verify_completion_sentinel(
        requested: dict[str, Any],
        sentinel: str,
    ) -> None:
        if not sentinel:
            raise ValueError("completion sentinel is required")
        expected_hash = str(requested.get("completion_sentinel_sha256") or "")
        actual_hash = hashlib.sha256(sentinel.encode("utf-8")).hexdigest()
        if not expected_hash or not secrets.compare_digest(expected_hash, actual_hash):
            raise ValueError("invalid handoff completion sentinel")

    async def _commit_handoff(
        self,
        capsule_id: str,
        handoff_id: str,
        *,
        requested: dict[str, Any],
        receipt: dict[str, Any] | None = None,
        validation: dict[str, Any] | None = None,
        approved_by: str | None = None,
    ) -> None:
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
            "correlation": dict(
                receipt.get("correlation")
                or requested.get("correlation")
                or {}
            ),
            "receipt": receipt,
            "validation": dict(validation or {}),
            "approved_by": approved_by,
        }
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
            f"{capsule_id} ownership committed to {requested.get('to_agent')}",
            data={**common, "a2a": a2a},
        )
        handoff_mode = str((requested.get("metadata") or {}).get("handoff_mode") or "")
        if handoff_mode.endswith("_auto"):
            await self.db.add_event(
                "capsule.auto_handoff_committed",
                f"{capsule_id} automatic lane rollover committed to {requested.get('to_agent')}",
                data={
                    "capsule_id": capsule_id,
                    "handoff_id": handoff_id,
                    "from_agent": requested.get("from_agent"),
                    "to_agent": requested.get("to_agent"),
                    "from_stage": requested.get("from_stage"),
                    "to_stage": requested.get("to_stage"),
                    "handoff_mode": handoff_mode,
                    "target_pane": receipt.get("pane_id"),
                    "correlation": dict(common.get("correlation") or {}),
                },
            )

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

        async with self._handoff_lock:
            current = await self.get(capsule_id)
            requested = self._handoff_event(current, handoff_id, "capsule.handoff_requested")
            if requested is None:
                raise ValueError("handoff request not found")
            if agent != requested.get("to_agent"):
                raise ValueError("handoff ACK agent mismatch")
            self._verify_delivery_token(requested, delivery_token)

            receipt = dict(receipt or {})
            dispatched = self._handoff_event(
                current,
                handoff_id,
                "capsule.handoff_dispatched",
            )
            expected_correlation = dict(
                (dispatched or {}).get("correlation")
                or requested.get("correlation")
                or {}
            )
            verified_correlation = self._verify_handoff_correlation(
                expected_correlation,
                receipt,
            )
            if verified_correlation:
                receipt["correlation"] = verified_correlation

            if self._handoff_event(current, handoff_id, "capsule.handoff_committed") is not None:
                return current
            if self._handoff_event(current, handoff_id, "capsule.handoff_failed") is not None:
                raise ValueError("handoff already failed")
            if current.get("current_agent") != requested.get("from_agent"):
                raise ValueError("handoff source no longer owns capsule")

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
                "correlation": verified_correlation,
                "receipt": receipt,
            }
            if self._handoff_event(current, handoff_id, "capsule.handoff_acknowledged") is None:
                await self.db.add_event(
                    "capsule.handoff_acknowledged",
                    f"{capsule_id} receipt acknowledged by {agent}",
                    data=common,
                )

            metadata = dict(requested.get("metadata") or {})
            if metadata.get("requires_validation"):
                return await self.get(capsule_id)

            await self._commit_handoff(
                capsule_id,
                handoff_id,
                requested=requested,
                receipt=receipt,
            )
            return await self.get(capsule_id)

    async def validate_handoff(
        self,
        capsule_id: str,
        handoff_id: str,
        *,
        agent: str,
        delivery_token: str,
        passed: bool,
        checks: list[dict[str, Any]] | None = None,
        plan: str | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if agent not in AGENTS:
            raise ValueError("unsupported handoff agent")

        async with self._handoff_lock:
            current = await self.get(capsule_id)
            requested = self._handoff_event(current, handoff_id, "capsule.handoff_requested")
            if requested is None:
                raise ValueError("handoff request not found")
            if agent != requested.get("to_agent"):
                raise ValueError("handoff validation agent mismatch")
            self._verify_delivery_token(requested, delivery_token)

            if self._handoff_event(current, handoff_id, "capsule.handoff_committed") is not None:
                return current
            if self._handoff_event(current, handoff_id, "capsule.handoff_acknowledged") is None:
                raise ValueError("handoff must be acknowledged before validation")
            if current.get("current_agent") != requested.get("from_agent"):
                raise ValueError("handoff source no longer owns capsule")

            metadata = dict(requested.get("metadata") or {})
            if not metadata.get("requires_validation"):
                raise ValueError("handoff does not require validation")
            contract = metadata.get("contract")
            if not isinstance(contract, dict):
                raise ValueError("capsule_contract_required")

            expected_checks = [str(item) for item in contract.get("handoff_checks") or []]
            supplied = [dict(item) for item in (checks or []) if isinstance(item, dict)]
            passed_by_criterion = {
                str(item.get("criterion") or ""): item
                for item in supplied
                if str(item.get("status") or "").casefold() == "pass"
            }
            missing = [criterion for criterion in expected_checks if criterion not in passed_by_criterion]
            effective_passed = bool(passed) and not missing

            validation = {
                "passed": effective_passed,
                "reported_passed": bool(passed),
                "checks": supplied,
                "missing_checks": missing,
                "plan": str(plan or "").strip() or None,
                "evidence": dict(evidence or {}),
                "validated_by": agent,
            }
            if not effective_passed:
                await self.db.add_event(
                    "capsule.handoff_validation_failed",
                    f"{capsule_id} handoff validation failed",
                    severity="warning",
                    data={
                        "capsule_id": capsule_id,
                        "handoff_id": handoff_id,
                        "from_agent": requested.get("from_agent"),
                        "to_agent": requested.get("to_agent"),
                        "validation": validation,
                    },
                )
                return await self.get(capsule_id)

            await self.db.add_event(
                "capsule.handoff_validated",
                f"{capsule_id} handoff contract validated by {agent}",
                data={
                    "capsule_id": capsule_id,
                    "handoff_id": handoff_id,
                    "from_agent": requested.get("from_agent"),
                    "to_agent": requested.get("to_agent"),
                    "validation": validation,
                },
            )

            if metadata.get("approval_required"):
                return await self.get(capsule_id)

            pending = current.get("pending_handoff") or {}
            await self._commit_handoff(
                capsule_id,
                handoff_id,
                requested=requested,
                receipt=dict(pending.get("receipt") or {}),
                validation=validation,
            )
            return await self.get(capsule_id)

    async def complete_handoff(
        self,
        capsule_id: str,
        handoff_id: str,
        *,
        agent: str,
        delivery_token: str,
        sentinel: str,
        receipt: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if agent not in AGENTS:
            raise ValueError("unsupported handoff agent")

        async with self._handoff_lock:
            current = await self.get(capsule_id)
            requested = self._handoff_event(
                current,
                handoff_id,
                "capsule.handoff_requested",
            )
            if requested is None:
                raise ValueError("handoff request not found")
            if agent != requested.get("to_agent"):
                raise ValueError("handoff completion agent mismatch")

            self._verify_delivery_token(requested, delivery_token)
            self._verify_completion_sentinel(requested, sentinel)

            committed = self._handoff_event(
                current,
                handoff_id,
                "capsule.handoff_committed",
            )
            if committed is None:
                raise ValueError("handoff must be committed before completion")
            if current.get("current_agent") != agent:
                raise ValueError("handoff completion agent does not own capsule")

            receipt = dict(receipt or {})
            expected_correlation = dict(
                committed.get("correlation")
                or requested.get("correlation")
                or {}
            )
            verified_correlation = self._verify_handoff_correlation(
                expected_correlation,
                receipt,
            )
            if verified_correlation:
                receipt["correlation"] = verified_correlation

            if self._handoff_event(
                current,
                handoff_id,
                "capsule.handoff_completed",
            ) is not None:
                return current

            await self.db.add_event(
                "capsule.handoff_completed",
                f"{capsule_id} handoff turn completed by {agent}",
                data={
                    "capsule_id": capsule_id,
                    "handoff_id": handoff_id,
                    "from_agent": requested.get("from_agent"),
                    "to_agent": requested.get("to_agent"),
                    "correlation": verified_correlation,
                    "receipt": receipt,
                    "sentinel_verified": True,
                },
            )
            handoff_mode = str(
                (requested.get("metadata") or {}).get("handoff_mode") or ""
            )
            if handoff_mode.endswith("_auto"):
                await self.db.add_event(
                    "capsule.auto_handoff_completed",
                    f"{capsule_id} automatic lane rollover turn completed by {agent}",
                    data={
                        "capsule_id": capsule_id,
                        "handoff_id": handoff_id,
                        "from_agent": requested.get("from_agent"),
                        "to_agent": requested.get("to_agent"),
                        "handoff_mode": handoff_mode,
                        "correlation": verified_correlation,
                        "sentinel_verified": True,
                    },
                )
            return await self.get(capsule_id)

    async def _resume_guarded_auto_handoff(
        self,
        capsule_id: str,
        handoff_id: str,
        *,
        agent: str,
        pane_id: str,
        approved_by: str,
    ) -> None:
        if self.herdr is None:
            await self.db.add_event(
                "capsule.auto_handoff_resume_failed",
                f"{capsule_id} automatic rollover resume failed: herdr_not_configured",
                severity="warning",
                data={
                    "capsule_id": capsule_id,
                    "handoff_id": handoff_id,
                    "to_agent": agent,
                    "target_pane": pane_id,
                    "reason": "herdr_not_configured",
                },
            )
            return
        try:
            snapshot = await self.herdr.refresh()
            if snapshot.get("status") == "down":
                raise CapsuleDeliveryError(
                    f"herdr_unavailable: {snapshot.get('error') or 'unknown'}"
                )
            pane = self.herdr.find_pane(pane_id=pane_id)
            if not pane or str(pane.get("pane_id") or "") != pane_id:
                raise CapsuleDeliveryError("target_pane_not_found")
            if pane.get("agent") and str(pane.get("agent")) != agent:
                raise CapsuleDeliveryError("target_pane_agent_mismatch")
            tool = self.herdr.tool("herdr_prompt_agent")
            if not tool:
                raise CapsuleDeliveryError("herdr_prompt_agent_missing")
            prompt = (
                "HIRDA AUTO ROLLOVER RESUME — guarded approval granted\n\n"
                f"Capsule: {capsule_id}\n"
                f"Handoff: {handoff_id}\n"
                f"Approved by: {approved_by}\n"
                "HIRDA has committed capsule ownership to you. Continue from the exact "
                "context_projection delivered with this handoff. Preserve objective, locked_decisions, "
                "constraints, artifacts, checkpoint, current stage, and completed work. Continue the "
                "existing next_actions; do not restart the task. When the handoff turn is fully complete, "
                "execute the original completion callback and sentinel from the delivery message.\n"
            )
            args = build_tool_arguments(
                tool,
                pane=pane,
                pane_id=pane_id,
                agent=agent,
                prompt=prompt,
                timeout_seconds=5,
            )
            input_schema = tool.get("inputSchema") or tool.get("input_schema") or {}
            properties = (
                input_schema.get("properties")
                if isinstance(input_schema, dict)
                else {}
            )
            if isinstance(properties, dict) and "wait" in properties:
                args["wait"] = False
            await self.herdr.client().call_tool(
                "herdr_prompt_agent",
                args,
                client_name=f"hirda-auto-resume-{handoff_id.lower()}",
            )
            await self.db.add_event(
                "capsule.auto_handoff_resumed",
                f"{capsule_id} automatic rollover resumed on {agent}",
                data={
                    "capsule_id": capsule_id,
                    "handoff_id": handoff_id,
                    "to_agent": agent,
                    "target_pane": pane_id,
                    "approved_by": approved_by,
                },
            )
        except Exception as exc:
            await self.db.add_event(
                "capsule.auto_handoff_resume_failed",
                f"{capsule_id} automatic rollover resume failed: {exc}",
                severity="warning",
                data={
                    "capsule_id": capsule_id,
                    "handoff_id": handoff_id,
                    "to_agent": agent,
                    "target_pane": pane_id,
                    "reason": str(exc),
                },
            )

    async def approve_handoff(
        self,
        capsule_id: str,
        handoff_id: str,
        *,
        approved_by: str,
    ) -> dict[str, Any]:
        approved_by = str(approved_by or "").strip()
        if not approved_by:
            raise ValueError("approved_by is required")

        resume_target: tuple[str, str] | None = None
        async with self._handoff_lock:
            current = await self.get(capsule_id)
            requested = self._handoff_event(
                current,
                handoff_id,
                "capsule.handoff_requested",
            )
            if requested is None:
                raise ValueError("handoff request not found")
            if (
                self._handoff_event(
                    current,
                    handoff_id,
                    "capsule.handoff_committed",
                )
                is not None
            ):
                return current

            metadata = dict(requested.get("metadata") or {})
            if not metadata.get("approval_required"):
                raise ValueError("handoff does not require approval")
            validated = self._handoff_event(
                current,
                handoff_id,
                "capsule.handoff_validated",
            )
            if validated is None:
                raise ValueError("handoff must pass validation before approval")
            validation = dict(validated.get("validation") or {})
            if not validation.get("passed"):
                raise ValueError("handoff validation has not passed")
            if current.get("current_agent") != requested.get("from_agent"):
                raise ValueError("handoff source no longer owns capsule")

            await self.db.add_event(
                "capsule.handoff_approved",
                f"{capsule_id} guarded handoff approved by {approved_by}",
                data={
                    "capsule_id": capsule_id,
                    "handoff_id": handoff_id,
                    "from_agent": requested.get("from_agent"),
                    "to_agent": requested.get("to_agent"),
                    "approved_by": approved_by,
                },
            )
            pending = current.get("pending_handoff") or {}
            receipt = dict(pending.get("receipt") or {})
            await self._commit_handoff(
                capsule_id,
                handoff_id,
                requested=requested,
                receipt=receipt,
                validation=validation,
                approved_by=approved_by,
            )
            if str(metadata.get("handoff_mode") or "") == "guarded_auto":
                target_agent = str(requested.get("to_agent") or "")
                target_pane = str(receipt.get("pane_id") or "")
                if target_agent and target_pane:
                    resume_target = (target_agent, target_pane)

        if resume_target is not None:
            await self._resume_guarded_auto_handoff(
                capsule_id,
                handoff_id,
                agent=resume_target[0],
                pane_id=resume_target[1],
                approved_by=approved_by,
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
