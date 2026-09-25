from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any

from .db import Database


PROJECTION_SCHEMA = "hirda-context-projection-v1"
PROJECTION_VERSION = 1
COMPACTION_KIND = "capsule.context_compacted"


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _empty_state(capsule_id: str) -> dict[str, Any]:
    return {
        "capsule_id": capsule_id,
        "title": capsule_id,
        "workspace": None,
        "source_pane": None,
        "status": "active",
        "current_agent": "claude",
        "current_stage": "ingress",
        "context_profile": None,
        "contract": None,
        "runtime_metadata": {},
        "last_handoff": None,
        "last_completion": None,
    }


def _observable_event(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": event.get("event_id"),
        "stream_seq": event.get("stream_seq"),
        "kind": event.get("kind"),
        "created_at": event.get("created_at"),
        "message": event.get("message"),
        "correlation_id": event.get("correlation_id"),
        "data": deepcopy(event.get("data") or {}),
    }


def _contract_context(contract: dict[str, Any] | None) -> dict[str, Any]:
    value = dict(contract or {})
    return {
        "objective": value.get("objective"),
        "locked_decisions": list(value.get("locked_decisions") or []),
        "artifacts": list(value.get("artifacts") or []),
        "current_state": dict(value.get("current_state") or {}),
        "checkpoint": dict(value.get("checkpoint") or {}),
        "next_actions": list(value.get("next_actions") or []),
        "acceptance_criteria": list(value.get("acceptance_criteria") or []),
        "handoff_checks": list(value.get("handoff_checks") or []),
        "constraints": list(value.get("constraints") or []),
        "portability": value.get("portability") or "pinned",
        "target_capabilities": deepcopy(value.get("target_capabilities") or {}),
    }


def _reduce_event(state: dict[str, Any], event: dict[str, Any]) -> None:
    kind = str(event.get("kind") or "")
    data = dict(event.get("data") or {})

    if kind == "capsule.created":
        state["title"] = data.get("title") or state["title"]
        state["workspace"] = data.get("workspace")
        state["source_pane"] = data.get("source_pane")
        state["current_agent"] = data.get("agent") or state["current_agent"]
        state["current_stage"] = data.get("stage") or "ingress"
        metadata = dict(data.get("metadata") or {})
        state["runtime_metadata"] = metadata
        state["context_profile"] = deepcopy(metadata.get("context_profile"))
        contract = metadata.get("contract")
        if isinstance(contract, dict):
            state["contract"] = deepcopy(contract)
        return

    if kind == "capsule.contract_updated":
        contract = data.get("contract")
        if isinstance(contract, dict):
            state["contract"] = deepcopy(contract)
        return

    if kind == "capsule.stage":
        state["current_stage"] = data.get("stage") or state["current_stage"]
        if data.get("agent"):
            state["current_agent"] = data["agent"]
        metadata = data.get("metadata")
        if isinstance(metadata, dict):
            state["runtime_metadata"].update(deepcopy(metadata))
        return

    if kind == "capsule.handoff_requested":
        state["last_handoff"] = {
            "handoff_id": data.get("handoff_id"),
            "state": "requested",
            "from_agent": data.get("from_agent"),
            "to_agent": data.get("to_agent"),
            "from_stage": data.get("from_stage"),
            "to_stage": data.get("to_stage"),
            "reason": data.get("reason"),
            "correlation": deepcopy(data.get("correlation") or {}),
            "validation": None,
            "approved_by": None,
        }
        return

    if kind == "capsule.handoff_dispatched":
        handoff = state.get("last_handoff")
        if isinstance(handoff, dict) and handoff.get("handoff_id") == data.get("handoff_id"):
            handoff["state"] = "dispatched"
            handoff["target_pane"] = data.get("target_pane")
            if data.get("correlation"):
                handoff["correlation"] = deepcopy(data["correlation"])
        return

    if kind == "capsule.handoff_acknowledged":
        handoff = state.get("last_handoff")
        if isinstance(handoff, dict) and handoff.get("handoff_id") == data.get("handoff_id"):
            handoff["state"] = "acknowledged"
            if data.get("correlation"):
                handoff["correlation"] = deepcopy(data["correlation"])
        return

    if kind in {"capsule.handoff_validated", "capsule.handoff_validation_failed"}:
        handoff = state.get("last_handoff")
        if isinstance(handoff, dict) and handoff.get("handoff_id") == data.get("handoff_id"):
            handoff["state"] = (
                "validated"
                if kind == "capsule.handoff_validated"
                else "validation_failed"
            )
            handoff["validation"] = deepcopy(data.get("validation") or {})
        return

    if kind == "capsule.handoff_approved":
        handoff = state.get("last_handoff")
        if isinstance(handoff, dict) and handoff.get("handoff_id") == data.get("handoff_id"):
            handoff["state"] = "approved"
            handoff["approved_by"] = data.get("approved_by")
        return
    if kind == "capsule.handoff_committed":
        state["current_agent"] = data.get("to_agent") or state["current_agent"]
        state["current_stage"] = data.get("to_stage") or state["current_stage"]
        state["last_handoff"] = {
            "handoff_id": data.get("handoff_id"),
            "state": "committed",
            "from_agent": data.get("from_agent"),
            "to_agent": data.get("to_agent"),
            "from_stage": data.get("from_stage"),
            "to_stage": data.get("to_stage"),
            "reason": data.get("reason"),
            "target_pane": data.get("target_pane"),
            "correlation": deepcopy(data.get("correlation") or {}),
            "validation": deepcopy(data.get("validation") or {}),
            "approved_by": data.get("approved_by"),
        }
        return

    if kind == "capsule.handoff_completed":
        state["last_completion"] = {
            "handoff_id": data.get("handoff_id"),
            "state": "completed",
            "sentinel_verified": bool(data.get("sentinel_verified")),
            "correlation": deepcopy(data.get("correlation") or {}),
        }
        return

    if kind in {"capsule.handoff_failed", "capsule.handoff_dispatch_uncertain"}:
        handoff = state.get("last_handoff")
        if isinstance(handoff, dict) and handoff.get("handoff_id") == data.get("handoff_id"):
            handoff["state"] = (
                "dispatch_uncertain"
                if kind == "capsule.handoff_dispatch_uncertain"
                else "failed"
            )
            handoff["error"] = data.get("error")
        return

    if kind == "capsule.handoff_blocked":
        state["last_handoff"] = {
            "handoff_id": data.get("handoff_id"),
            "state": "blocked",
            "from_agent": data.get("from_agent"),
            "to_agent": data.get("to_agent"),
            "reason": data.get("reason"),
            "correlation": deepcopy(data.get("correlation") or {}),
        }
        return

    if kind == "capsule.completed":
        state["status"] = "completed"
        state["current_stage"] = data.get("stage") or "result"
        if data.get("agent"):
            state["current_agent"] = data["agent"]
        metadata = data.get("metadata")
        if isinstance(metadata, dict):
            state["runtime_metadata"].update(deepcopy(metadata))


class ContextProjector:
    def __init__(self, db: Database, *, recent_limit: int = 20):
        self.db = db
        self.recent_limit = max(1, int(recent_limit))
    async def _events(self, capsule_id: str) -> list[dict[str, Any]]:
        if hasattr(self.db, "list_events"):
            return await self.db.list_events(
                stream_id=f"capsule:{capsule_id}",
                kind_prefix="capsule.",
                limit=None,
                ascending=True,
            )

        raw = await self.db.recent_events(500)
        events = []
        for event in reversed(raw):
            data = dict(event.get("data") or {})
            if str(data.get("capsule_id") or "") != capsule_id:
                continue
            if not str(event.get("kind") or "").startswith("capsule."):
                continue
            item = dict(event)
            item.setdefault("stream_seq", item.get("id"))
            item.setdefault("stream_id", f"capsule:{capsule_id}")
            item.setdefault("event_id", f"LEGACY-{item.get('id')}")
            events.append(item)
        return events

    @staticmethod
    def _latest_checkpoint(events: list[dict[str, Any]]) -> dict[str, Any] | None:
        for event in reversed(events):
            if event.get("kind") != COMPACTION_KIND:
                continue
            data = dict(event.get("data") or {})
            if int(data.get("projection_version") or 0) != PROJECTION_VERSION:
                continue
            base_state = data.get("base_state")
            recent_events = data.get("recent_events")
            if not isinstance(base_state, dict) or not isinstance(recent_events, list):
                continue
            expected_hash = str(data.get("checkpoint_sha256") or "")
            actual_hash = _canonical_sha256(
                {
                    "through_stream_seq": int(data.get("through_stream_seq") or 0),
                    "source_event_hash": data.get("source_event_hash"),
                    "base_state": base_state,
                    "recent_events": recent_events,
                }
            )
            if expected_hash and expected_hash == actual_hash:
                return event
        return None

    def _projection_document(
        self,
        *,
        capsule_id: str,
        state: dict[str, Any],
        recent_events: list[dict[str, Any]],
        through_stream_seq: int,
        source_event_hash: str | None,
        checkpoint_used: bool,
        checkpoint_event_id: str | None,
    ) -> dict[str, Any]:
        contract = _contract_context(state.get("contract"))
        context = {
            **contract,
            "runtime": {
                "title": state.get("title"),
                "status": state.get("status"),
                "current_agent": state.get("current_agent"),
                "current_stage": state.get("current_stage"),
                "workspace": state.get("workspace"),
                "source_pane": state.get("source_pane"),
                "context_profile": deepcopy(state.get("context_profile")),
                "metadata": deepcopy(state.get("runtime_metadata") or {}),
            },
            "handoff": deepcopy(state.get("last_handoff")),
            "completion": deepcopy(state.get("last_completion")),
        }
        semantic = {
            "schema": PROJECTION_SCHEMA,
            "projection_version": PROJECTION_VERSION,
            "stream_id": f"capsule:{capsule_id}",
            "capsule_id": capsule_id,
            "through_stream_seq": through_stream_seq,
            "source_event_hash": source_event_hash,
            "context": context,
            "recent_events": recent_events,
        }
        fingerprint = _canonical_sha256(semantic)
        return {
            **semantic,
            "checkpoint": {
                "used": checkpoint_used,
                "event_id": checkpoint_event_id,
            },
            "projection_sha256": fingerprint,
        }
    async def project_capsule(
        self,
        capsule_id: str,
        *,
        use_checkpoint: bool = True,
    ) -> dict[str, Any]:
        events = await self._events(capsule_id)
        if not events:
            raise KeyError(capsule_id)

        state = _empty_state(capsule_id)
        recent_events: list[dict[str, Any]] = []
        start_after = 0
        checkpoint_used = False
        checkpoint_event_id: str | None = None

        checkpoint = self._latest_checkpoint(events) if use_checkpoint else None
        if checkpoint is not None:
            data = dict(checkpoint.get("data") or {})
            state = deepcopy(data["base_state"])
            recent_events = deepcopy(data["recent_events"])
            start_after = int(data.get("through_stream_seq") or 0)
            checkpoint_used = True
            checkpoint_event_id = str(checkpoint.get("event_id") or "") or None

        for event in events:
            seq = int(event.get("stream_seq") or 0)
            if seq <= start_after:
                continue
            if event.get("kind") != COMPACTION_KIND:
                _reduce_event(state, event)
                recent_events.append(_observable_event(event))
                recent_events = recent_events[-self.recent_limit :]

        last_event = events[-1]
        return self._projection_document(
            capsule_id=capsule_id,
            state=state,
            recent_events=recent_events,
            through_stream_seq=int(last_event.get("stream_seq") or 0),
            source_event_hash=str(last_event.get("event_hash") or "") or None,
            checkpoint_used=checkpoint_used,
            checkpoint_event_id=checkpoint_event_id,
        )

    async def compact_capsule(self, capsule_id: str) -> dict[str, Any]:
        projection = await self.project_capsule(
            capsule_id,
            use_checkpoint=False,
        )
        checkpoint_payload = {
            "through_stream_seq": projection["through_stream_seq"],
            "source_event_hash": projection["source_event_hash"],
            "base_state": {
                "capsule_id": capsule_id,
                "title": projection["context"]["runtime"].get("title")
                or capsule_id,
                "workspace": projection["context"]["runtime"].get("workspace"),
                "source_pane": projection["context"]["runtime"].get("source_pane"),
                "status": projection["context"]["runtime"].get("status"),
                "current_agent": projection["context"]["runtime"].get("current_agent"),
                "current_stage": projection["context"]["runtime"].get("current_stage"),
                "context_profile": deepcopy(
                    projection["context"]["runtime"].get("context_profile")
                ),
                "contract": {
                    "objective": projection["context"].get("objective"),
                    "locked_decisions": projection["context"].get("locked_decisions") or [],
                    "artifacts": projection["context"].get("artifacts") or [],
                    "current_state": projection["context"].get("current_state") or {},
                    "checkpoint": projection["context"].get("checkpoint") or {},
                    "next_actions": projection["context"].get("next_actions") or [],
                    "acceptance_criteria": projection["context"].get("acceptance_criteria") or [],
                    "handoff_checks": projection["context"].get("handoff_checks") or [],
                    "constraints": projection["context"].get("constraints") or [],
                    "portability": projection["context"].get("portability") or "pinned",
                    "target_capabilities": deepcopy(
                        projection["context"].get("target_capabilities") or {}
                    ),
                },
                "runtime_metadata": deepcopy(
                    projection["context"]["runtime"].get("metadata") or {}
                ),
                "last_handoff": deepcopy(projection["context"].get("handoff")),
                "last_completion": deepcopy(projection["context"].get("completion")),
            },
            "recent_events": deepcopy(projection["recent_events"]),
        }
        checkpoint_sha256 = _canonical_sha256(checkpoint_payload)
        await self.db.add_event(
            COMPACTION_KIND,
            f"{capsule_id} context projection compacted",
            data={
                "capsule_id": capsule_id,
                "projection_version": PROJECTION_VERSION,
                **checkpoint_payload,
                "checkpoint_sha256": checkpoint_sha256,
                "base_projection_sha256": projection["projection_sha256"],
            },
        )
        return await self.project_capsule(capsule_id, use_checkpoint=True)
