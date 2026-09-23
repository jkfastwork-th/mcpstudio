from __future__ import annotations

import re

import pytest

from mcp_studio.capsules import CapsuleNotFound, CapsuleService
from mcp_studio.agent_runtimes import AgentRuntimeInventory
from mcp_studio.context_fit import ContextProfile, ModelCapability, evaluate_context_fit


class FakeDeliveryClient:
    def __init__(self):
        self.calls = []

    async def call_tool(self, name, args, client_name=None):
        self.calls.append({"name": name, "args": dict(args), "client_name": client_name})
        return {"content": [{"type": "text", "text": '{"ok":true}'}]}


class FakeDeliveryHerdr:
    def __init__(self):
        self.snapshot = {"status": "healthy"}
        self.client_instance = FakeDeliveryClient()
        self.tool_definition = {
            "name": "herdr_prompt_agent",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "Unique agent name or pane ID.",
                    },
                    "message": {"type": "string"},
                    "wait": {"type": "boolean", "default": False},
                    "timeout_ms": {"type": "integer"},
                },
                "required": ["agent_id", "message"],
            },
        }

    async def refresh(self):
        return self.snapshot

    def find_pane(self, *, pane_id=None, workspace=None, agent=None):
        pane = {
            "pane_id": "w1:p7",
            "workspace_id": "w1",
            "agent": "hermes",
            "agent_status": "idle",
        }
        if pane_id and pane_id != pane["pane_id"]:
            return None
        if agent and agent != pane["agent"]:
            return None
        return pane

    def tool(self, name):
        return self.tool_definition if name == "herdr_prompt_agent" else None

    def client(self):
        return self.client_instance


class FakeDB:
    def __init__(self):
        self.events = []

    async def add_event(self, kind, message, *, severity="info", server_id=None, data=None):
        i = len(self.events) + 1
        self.events.append(
            {
                "id": i,
                "kind": kind,
                "severity": severity,
                "server_id": server_id,
                "message": message,
                "data": data or {},
                "created_at": f"2026-09-18T08:00:{i:02d}+00:00",
            }
        )

    async def recent_events(self, limit=100):
        return list(reversed(self.events))[:limit]


@pytest.mark.asyncio
async def test_capsule_handoff_uses_capsule_id_as_visual_connector():
    herdr = FakeDeliveryHerdr()
    service = CapsuleService(FakeDB(), herdr)
    created = await service.create(
        title="Continue MCP Studio",
        workspace="mcp-studio",
        source_pane="w1:p1",
        agent="claude",
        capsule_id="C-204",
        metadata={"context_profile": {"source_tokens": 120000, "retained_tokens": 120000}},
    )
    assert created["capsule_id"] == "C-204"
    assert created["current_agent"] == "claude"

    await service.set_stage("C-204", stage="agent_runtime", agent="claude")
    handed = await service.handoff(
        "C-204",
        from_agent="claude",
        to_agent="hermes",
        reason="quota_exhausted",
        metadata={
            "model_capability": {
                "provider": "nous",
                "model_id": "upstage/solar-pro4:free",
                "context_window": 524288,
                "max_output_tokens": 32768,
                "system_prompt_tokens": 6000,
                "tool_schema_tokens": 8000,
                "safety_reserve_tokens": 16000,
                "last_verified_at": "2026-09-18T09:30:00Z",
            }
        },
    )

    assert handed["current_agent"] == "claude"
    assert handed["current_stage"] == "agent_runtime"
    assert handed["last_handoff"] is None
    assert handed["pending_handoff"]["handoff_id"].startswith("H-")
    assert handed["pending_handoff"]["reason"] == "quota_exhausted"
    assert handed["pending_handoff"]["context_fit"]["fit"] is True
    assert handed["pending_handoff"]["state"] == "dispatched"
    assert handed["capsule_type"] == "full"
    assert handed["context_profile"]["retained_percent"] == 100.0

    prompt = herdr.client_instance.calls[-1]["args"]["message"]
    token_match = re.search(r'"delivery_token":"([^"]+)"', prompt)
    assert token_match
    committed = await service.acknowledge_handoff(
        "C-204",
        handed["pending_handoff"]["handoff_id"],
        agent="hermes",
        delivery_token=token_match.group(1),
        receipt={"transport": "herdr", "pane_id": "w1:p7"},
    )

    assert committed["current_agent"] == "hermes"
    assert committed["last_handoff"]["connector_id"] == "C-204"
    assert committed["last_handoff"]["handoff_id"].startswith("H-")
    assert committed["last_handoff"]["reason"] == "quota_exhausted"
    assert committed["last_handoff"]["context_fit"]["fit"] is True
    assert committed["last_handoff"]["a2a"]["protocol"] == "a2a"
    assert committed["last_handoff"]["a2a"]["task_id"].startswith("A2A-")
    assert committed["last_handoff"]["a2a"]["context_id"] == "C-204"
    assert committed["last_handoff"]["a2a"]["task"]["kind"] == "task"
    assert committed["last_handoff"]["a2a"]["task"]["status"]["state"] == "working"

    overview = await service.overview()
    assert overview["summary"] == {"active": 1, "total": 1, "handoffs": 1}

    done = await service.complete("C-204", agent="hermes")
    assert done["status"] == "completed"
    assert done["current_stage"] == "result"


@pytest.mark.asyncio
async def test_capsule_missing_fails_closed():
    service = CapsuleService(FakeDB())
    with pytest.raises(CapsuleNotFound):
        await service.handoff(
            "C-missing",
            from_agent="claude",
            to_agent="hermes",
        )


def test_context_profile_types_and_fit_math():
    full = ContextProfile(source_tokens=100000, retained_tokens=100000)
    compact = ContextProfile(source_tokens=100000, retained_tokens=62000)
    minimal = ContextProfile(source_tokens=100000, retained_tokens=18000)
    assert full.capsule_type == "full" and full.retained_percent == 100.0
    assert compact.capsule_type == "compact" and compact.retained_percent == 62.0
    assert minimal.capsule_type == "minimal" and minimal.retained_percent == 18.0

    cap = ModelCapability(
        provider="test",
        model_id="model-a",
        context_window=100000,
        max_output_tokens=10000,
        system_prompt_tokens=5000,
        tool_schema_tokens=5000,
        safety_reserve_tokens=10000,
    )
    safe = evaluate_context_fit(compact, cap)
    blocked = evaluate_context_fit(ContextProfile(source_tokens=120000, retained_tokens=90000), cap)
    assert safe["fit"] is True
    assert safe["usable_context_tokens"] == 70000
    assert blocked["fit"] is False
    assert blocked["fit_state"] == "blocked"


@pytest.mark.asyncio
async def test_handoff_fails_closed_when_context_exceeds_target_model():
    db = FakeDB()
    service = CapsuleService(db)
    await service.create(
        title="Oversized capsule",
        agent="claude",
        capsule_id="C-BIG",
        metadata={"context_profile": {"source_tokens": 200000, "retained_tokens": 150000}},
    )
    with pytest.raises(ValueError, match="capsule_context_too_large"):
        await service.handoff(
            "C-BIG",
            from_agent="claude",
            to_agent="hermes",
            metadata={
                "model_capability": {
                    "provider": "nous",
                    "model_id": "small-target",
                    "context_window": 100000,
                    "max_output_tokens": 10000,
                    "system_prompt_tokens": 5000,
                    "tool_schema_tokens": 5000,
                    "safety_reserve_tokens": 10000,
                }
            },
        )
    current = await service.get("C-BIG")
    assert current["current_agent"] == "claude"
    assert current["handoffs"] == []
    assert len(current["blocked_handoffs"]) == 1
    assert current["blocked_handoffs"][0]["reason"] == "capsule_context_too_large"
    assert current["blocked_handoffs"][0]["a2a"]["task"]["status"]["state"] == "rejected"


@pytest.mark.asyncio
async def test_handoff_requires_verified_context_profile_and_capability():
    db = FakeDB()
    service = CapsuleService(db)
    await service.create(title="No context profile", agent="claude", capsule_id="C-NOPROFILE")
    with pytest.raises(ValueError, match="capsule_context_profile_required"):
        await service.handoff("C-NOPROFILE", from_agent="claude", to_agent="codex")
    current = await service.get("C-NOPROFILE")
    assert current["current_agent"] == "claude"
    assert current["blocked_handoffs"][0]["reason"] == "capsule_context_profile_required"


class FakeHerdr:
    snapshot = {
        "status": "healthy",
        "panes": {
            "panes": [
                {
                    "pane_id": "w1:p1",
                    "workspace_id": "w1",
                    "agent": "claude",
                    "agent_status": "blocked",
                    "model": "claude-sonnet",
                    "provider": "anthropic",
                },
                {
                    "pane_id": "w1:p7",
                    "workspace_id": "w1",
                    "agent": "hermes",
                    "agent_status": "idle",
                    "model": "upstage/solar-pro4:free",
                    "provider": "nous",
                },
            ]
        },
    }

    @staticmethod
    def pane_list(snapshot):
        return snapshot["panes"]["panes"]


def test_runtime_inventory_detects_three_lanes_and_nous_free(monkeypatch):
    monkeypatch.setattr(
        "mcp_studio.agent_runtimes.shutil.which",
        lambda name: f"/usr/local/bin/{name}",
    )
    monkeypatch.setattr(
        AgentRuntimeInventory,
        "_version",
        staticmethod(lambda binary: f"{binary.rsplit('/', 1)[-1]} v-test"),
    )
    monkeypatch.setattr(
        AgentRuntimeInventory,
        "_hermes_config",
        staticmethod(lambda: {}),
    )
    monkeypatch.setattr(
        AgentRuntimeInventory,
        "_credential_marker",
        classmethod(lambda cls, key: None),
    )

    data = AgentRuntimeInventory(FakeHerdr(), ttl_seconds=60).snapshot(force=True)
    by_id = {item["id"]: item for item in data["runtimes"]}

    assert set(by_id) == {"claude", "codex", "hermes"}
    assert by_id["claude"]["status"] == "blocked"
    assert by_id["codex"]["status"] == "available"
    assert by_id["hermes"]["status"] == "ready"
    assert by_id["hermes"]["model"] == "upstage/solar-pro4:free"
    assert by_id["hermes"]["free"] is True
    assert by_id["hermes"]["nous_free"]["active"] is True
    assert by_id["hermes"]["auth_health"]["status"] == "unknown"
    assert by_id["hermes"]["limit_health"]["status"] == "interactive_only"
    assert by_id["codex"]["auth_health"]["status"] == "unknown"
    assert by_id["codex"]["limit_health"]["status"] == "interactive_only"
    assert data["summary"]["installed"] == 3
    assert data["summary"]["free_active"] == 1
    assert data["summary"]["auth_healthy"] == 0
    assert data["summary"]["auth_verified"] == 0
    assert data["summary"]["limit_constrained"] == 0


class FakeHerdrHealth:
    snapshot = {
        "status": "healthy",
        "panes": {
            "panes": [
                {
                    "pane_id": "w1:claude",
                    "workspace_id": "w1",
                    "agent": "claude",
                    "agent_status": "blocked",
                    "auth_status": "unauthenticated",
                    "last_error": "401 authentication failed",
                },
                {
                    "pane_id": "w1:codex",
                    "workspace_id": "w1",
                    "agent": "codex",
                    "agent_status": "idle",
                    "auth_status": "authenticated",
                    "rate_limit_status": "healthy",
                    "rate_limit_remaining": "87%",
                    "rate_limit_reset_at": "2026-09-19T13:00:00Z",
                },
                {
                    "pane_id": "w1:hermes",
                    "workspace_id": "w1",
                    "agent": "hermes",
                    "agent_status": "blocked",
                    "last_error": "429 rate limit exceeded",
                },
            ]
        },
    }

    @staticmethod
    def pane_list(snapshot):
        return snapshot["panes"]["panes"]


def test_runtime_inventory_exposes_auth_and_limit_health(monkeypatch):
    monkeypatch.setattr(
        "mcp_studio.agent_runtimes.shutil.which",
        lambda name: f"/usr/local/bin/{name}",
    )
    monkeypatch.setattr(
        AgentRuntimeInventory,
        "_version",
        staticmethod(lambda binary: "v-test"),
    )
    monkeypatch.setattr(
        AgentRuntimeInventory,
        "_hermes_config",
        staticmethod(lambda: {}),
    )
    monkeypatch.setattr(
        AgentRuntimeInventory,
        "_credential_marker",
        classmethod(lambda cls, key: None),
    )

    data = AgentRuntimeInventory(FakeHerdrHealth(), ttl_seconds=60).snapshot(force=True)
    by_id = {item["id"]: item for item in data["runtimes"]}

    assert by_id["claude"]["auth_health"] == {
        "status": "error",
        "authenticated": False,
        "source": "pane_metadata",
        "detail": "Runtime reports authentication state: unauthenticated.",
    }
    assert by_id["claude"]["last_error"] == "401 authentication failed"
    assert by_id["codex"]["auth_health"]["status"] == "logged_in"
    assert by_id["codex"]["auth_health"]["authenticated"] is True
    assert by_id["codex"]["limit_health"]["status"] == "healthy"
    assert by_id["codex"]["limit_health"]["remaining"] == "87%"
    assert by_id["codex"]["limit_health"]["reset_at"] == "2026-09-19T13:00:00Z"
    assert by_id["hermes"]["limit_health"]["status"] == "limited"
    assert by_id["hermes"]["limit_health"]["source"] == "runtime_error"
    assert data["summary"]["auth_healthy"] == 1
    assert data["summary"]["auth_verified"] == 1
    assert data["summary"]["limit_constrained"] == 1


def test_cli_auth_probes_use_runtime_native_status(monkeypatch):
    def fake_probe(binary, args, timeout=4.0):
        if binary.endswith("claude"):
            return {
                "returncode": 0,
                "stdout": '{"loggedIn":true,"authMethod":"claude.ai","subscriptionType":"pro"}',
                "stderr": "",
            }
        if binary.endswith("codex"):
            return {
                "returncode": 0,
                "stdout": "",
                "stderr": "Logged in using ChatGPT",
            }
        if binary.endswith("hermes"):
            return {
                "returncode": 0,
                "stdout": "9router: logged in",
                "stderr": "",
            }
        return None

    monkeypatch.setattr(
        AgentRuntimeInventory,
        "_run_probe",
        staticmethod(fake_probe),
    )

    claude = AgentRuntimeInventory._cli_auth_health(
        "claude", binary="/usr/bin/claude", provider="Anthropic"
    )
    codex = AgentRuntimeInventory._cli_auth_health(
        "codex", binary="/usr/bin/codex", provider="OpenAI"
    )
    hermes = AgentRuntimeInventory._cli_auth_health(
        "hermes", binary="/usr/bin/hermes", provider="9router"
    )

    assert claude["status"] == "logged_in"
    assert claude["authenticated"] is True
    assert "claude.ai" in claude["detail"]
    assert codex == {
        "status": "logged_in",
        "authenticated": True,
        "source": "codex_login_status",
        "detail": "Codex reports logged in using ChatGPT.",
    }
    assert hermes == {
        "status": "logged_in",
        "authenticated": True,
        "source": "hermes_auth_status",
        "detail": "Hermes reports 9router logged in.",
    }


def test_cli_auth_probe_reports_logged_out(monkeypatch):
    monkeypatch.setattr(
        AgentRuntimeInventory,
        "_run_probe",
        staticmethod(
            lambda binary, args, timeout=4.0: {
                "returncode": 1,
                "stdout": "",
                "stderr": "Not logged in",
            }
        ),
    )
    health = AgentRuntimeInventory._cli_auth_health(
        "codex", binary="/usr/bin/codex", provider="OpenAI"
    )
    assert health["status"] == "error"
    assert health["authenticated"] is False



def test_hermes_custom_provider_logged_out_is_non_blocking(monkeypatch):
    monkeypatch.setattr(
        AgentRuntimeInventory,
        "_run_probe",
        staticmethod(
            lambda binary, args, timeout=4.0: {
                "returncode": 0,
                "stdout": "custom:9router: logged out",
                "stderr": "",
            }
        ),
    )
    health = AgentRuntimeInventory._cli_auth_health(
        "hermes", binary="/usr/bin/hermes", provider="9router"
    )
    assert health == {
        "status": "configured",
        "authenticated": None,
        "source": "hermes_custom_provider",
        "detail": (
            "Hermes custom provider 9router has no Hermes-managed login; "
            "runtime availability must be verified by the configured backend."
        ),
    }


def test_hermes_regular_provider_logged_out_remains_error(monkeypatch):
    monkeypatch.setattr(
        AgentRuntimeInventory,
        "_run_probe",
        staticmethod(
            lambda binary, args, timeout=4.0: {
                "returncode": 0,
                "stdout": "9router: logged out",
                "stderr": "",
            }
        ),
    )
    health = AgentRuntimeInventory._cli_auth_health(
        "hermes", binary="/usr/bin/hermes", provider="9router"
    )
    assert health["status"] == "error"
    assert health["authenticated"] is False
