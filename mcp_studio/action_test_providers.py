from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .action_adapter import ActionEnvelope, normalize_action_envelope


TEST_PROVIDER_SCHEMA = "hirda-action-test-providers-v1"


@dataclass(frozen=True, slots=True)
class ActionTestProvider:
    provider_id: str
    domain: str
    title: str
    description: str
    envelope: ActionEnvelope
    executor_attached: bool = False

    def build_envelope(self) -> ActionEnvelope:
        return self.envelope

    def summary(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "domain": self.domain,
            "title": self.title,
            "description": self.description,
            "action_count": len(self.envelope.actions),
            "executor_attached": self.executor_attached,
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.summary(),
            "envelope": self.envelope.as_dict(),
        }


def _browser() -> ActionTestProvider:
    envelope = normalize_action_envelope(
        {
            "schema": "hirda-action-envelope-v1",
            "request_id": "jaa1:browser:search",
            "provider": "browser",
            "domain": "browser",
            "goal": "Choose the next bounded step for a search page whose query is already present.",
            "state": {
                "page_kind": "search",
                "query_present": True,
                "results_visible": False,
                "loading": False,
                "network_idle": True,
            },
            "actions": [
                {
                    "action_id": "browser:wait",
                    "operation": "wait",
                    "title": "Wait for page state",
                    "description": "Observe without causing an external side effect.",
                    "permission_class": "read",
                    "arguments": {},
                    "constraints": {"milliseconds": 500},
                    "risk": {
                        "consequential": False,
                        "reversible": True,
                        "external_side_effect": False,
                    },
                },
                {
                    "action_id": "browser:submit-search",
                    "operation": "submit",
                    "title": "Submit existing search",
                    "description": "Submit the query and navigate to results.",
                    "permission_class": "execute",
                    "arguments": {"target": "search-form"},
                    "constraints": {"query_already_present": True},
                    "risk": {
                        "consequential": False,
                        "reversible": True,
                        "external_side_effect": True,
                    },
                },
                {
                    "action_id": "browser:clear-query",
                    "operation": "clear",
                    "title": "Clear the query",
                    "description": "Remove the current query without submitting anything.",
                    "permission_class": "write",
                    "arguments": {"target": "search-input"},
                    "constraints": {"field_editable": True},
                    "risk": {
                        "consequential": False,
                        "reversible": True,
                        "external_side_effect": False,
                    },
                },
            ],
            "metadata": {
                "fixture": "jaa1-browser",
                "executor_attached": False,
            },
        }
    )
    return ActionTestProvider(
        provider_id="browser",
        domain="browser",
        title="Browser",
        description="DOM-like search flow with wait, submit, and local edit candidates.",
        envelope=envelope,
    )


def _world() -> ActionTestProvider:
    envelope = normalize_action_envelope(
        {
            "schema": "hirda-action-envelope-v1",
            "request_id": "jaa1:world:social",
            "provider": "world",
            "domain": "world",
            "goal": "Choose a bounded response to a low-priority social stimulus while an activity is in progress.",
            "state": {
                "activity": "drawing",
                "social_stimulus": {
                    "kind": "npc_message",
                    "resident_id": "npc-mali",
                    "response_required": False,
                },
                "energy": 0.72,
                "social_drive": 0.56,
            },
            "actions": [
                {
                    "action_id": "world:observe",
                    "operation": "observe",
                    "title": "Observe without responding",
                    "description": "Keep the current activity and collect more context.",
                    "permission_class": "read",
                    "arguments": {},
                    "constraints": {},
                    "risk": {
                        "consequential": False,
                        "reversible": True,
                        "external_side_effect": False,
                    },
                },
                {
                    "action_id": "world:respond",
                    "operation": "respond",
                    "title": "Respond briefly",
                    "description": "Allow a short social response while preserving the current task.",
                    "permission_class": "execute",
                    "arguments": {"resident_id": "npc-mali", "intensity": "low"},
                    "constraints": {"preserve_active_activity": True},
                    "risk": {
                        "consequential": False,
                        "reversible": True,
                        "external_side_effect": True,
                    },
                },
                {
                    "action_id": "world:defer",
                    "operation": "defer",
                    "title": "Defer the interaction",
                    "description": "Acknowledge that the interaction can be handled later.",
                    "permission_class": "execute",
                    "arguments": {"resident_id": "npc-mali"},
                    "constraints": {"response_required": False},
                    "risk": {
                        "consequential": False,
                        "reversible": True,
                        "external_side_effect": True,
                    },
                },
            ],
            "metadata": {
                "fixture": "jaa1-world",
                "executor_attached": False,
            },
        }
    )
    return ActionTestProvider(
        provider_id="world",
        domain="world",
        title="World",
        description="Earth-like social state with observe, respond, and defer candidates.",
        envelope=envelope,
    )


def _finance() -> ActionTestProvider:
    envelope = normalize_action_envelope(
        {
            "schema": "hirda-action-envelope-v1",
            "request_id": "jaa1:finance:rebalance",
            "provider": "finance",
            "domain": "finance",
            "goal": "Choose among already-computed portfolio workflow candidates without placing any order.",
            "state": {
                "cash_pct": 18.0,
                "target_cash_pct": 12.0,
                "allocation_drift_pct": 4.2,
                "market_data_fresh": True,
                "market_open": True,
                "pending_orders": 0,
            },
            "actions": [
                {
                    "action_id": "finance:hold",
                    "operation": "hold",
                    "title": "Hold current allocation",
                    "description": "Do not prepare or place an order.",
                    "permission_class": "read",
                    "arguments": {},
                    "constraints": {},
                    "risk": {
                        "consequential": False,
                        "reversible": True,
                        "external_side_effect": False,
                        "financial": False,
                    },
                },
                {
                    "action_id": "finance:refresh-data",
                    "operation": "refresh",
                    "title": "Refresh market data",
                    "description": "Refresh observations before choosing a portfolio action.",
                    "permission_class": "read",
                    "arguments": {"feed": "market"},
                    "constraints": {"orders_forbidden": True},
                    "risk": {
                        "consequential": False,
                        "reversible": True,
                        "external_side_effect": False,
                        "financial": False,
                    },
                },
                {
                    "action_id": "finance:prepare-rebalance",
                    "operation": "prepare_rebalance",
                    "title": "Prepare rebalance preview",
                    "description": "Prepare an already-computed rebalance plan for human review. The playground cannot place orders.",
                    "permission_class": "execute",
                    "arguments": {"plan_id": "demo-plan-a"},
                    "constraints": {
                        "orders_forbidden": True,
                        "within_position_limits": True,
                        "within_order_value_limit": True,
                    },
                    "risk": {
                        "consequential": True,
                        "reversible": True,
                        "external_side_effect": False,
                        "financial": True,
                        "requires_human_approval": True,
                    },
                },
            ],
            "metadata": {
                "fixture": "jaa1-finance",
                "executor_attached": False,
                "orders_forbidden": True,
            },
        }
    )
    return ActionTestProvider(
        provider_id="finance",
        domain="finance",
        title="Finance",
        description="Portfolio workflow fixture with financial consequence metadata but no broker execution.",
        envelope=envelope,
    )


def all_test_providers() -> tuple[ActionTestProvider, ...]:
    return (_browser(), _world(), _finance())


def list_test_providers() -> dict[str, Any]:
    providers = all_test_providers()
    return {
        "schema": TEST_PROVIDER_SCHEMA,
        "providers": [provider.summary() for provider in providers],
        "executor_attached": False,
    }


def get_test_provider(provider_id: str) -> ActionTestProvider:
    normalized = str(provider_id or "").strip().casefold()
    for provider in all_test_providers():
        if provider.provider_id == normalized:
            return provider
    raise KeyError(normalized)
