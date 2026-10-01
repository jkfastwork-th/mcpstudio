import asyncio
from types import SimpleNamespace

import pytest

from mcp_studio.agent_runtimes import AgentRuntimeInventory
from mcp_studio.cognitive_router import (
    CognitiveRequest,
    CognitiveRouter,
    CognitiveRouterError,
    JevCognitiveJudgment,
    _extract_marked_result,
    _runtime_terminal_error,
    normalize_cognitive_request,
)


PROMPT_TOOL = {
    "name": "herdr_prompt_agent",
    "inputSchema": {
        "type": "object",
        "properties": {
            "pane_id": {"type": "string"},
            "prompt": {"type": "string"},
        },
        "required": ["pane_id", "prompt"],
    },
}
READ_TOOL = {
    "name": "herdr_read_agent",
    "inputSchema": {
        "type": "object",
        "properties": {"pane_id": {"type": "string"}},
        "required": ["pane_id"],
    },
}
WAIT_TOOL = {
    "name": "herdr_wait_agent",
    "inputSchema": {
        "type": "object",
        "properties": {
            "pane_id": {"type": "string"},
            "until": {"type": "string"},
            "timeout_seconds": {"type": "integer"},
        },
        "required": ["pane_id"],
    },
}


class FakeClient:
    def __init__(self, herdr):
        self.herdr = herdr

    async def call_tool(self, name, arguments, client_name="test"):
        pane = arguments.get("pane_id")
        self.herdr.calls.append((name, dict(arguments), client_name))
        if name == "herdr_prompt_agent" and pane in self.herdr.fail_prompt_panes:
            raise RuntimeError(f"runtime down: {pane}")
        if name == "herdr_prompt_agent":
            prompt = str(arguments.get("prompt") or "")
            request_id = None
            for line in prompt.splitlines():
                if line.startswith("request_id:"):
                    request_id = line.split(":", 1)[1].strip()
                    break
            if request_id:
                self.herdr.request_ids[pane] = request_id
            return {"content": [{"type": "text", "text": '{"accepted":true}'}]}
        if name == "herdr_wait_agent":
            self.herdr.waited_panes.add(pane)
            return {"content": [{"type": "text", "text": '{"status":"idle"}'}]}
        if name == "herdr_read_agent":
            if pane not in self.herdr.waited_panes:
                raise RuntimeError(f"read before idle: {pane}")
            value = self.herdr.outputs.get(pane, f"RESULT:{pane}")
            request_id = self.herdr.request_ids.get(pane)
            if request_id and "<<<HIRDA_COGNITIVE_RESULT:" not in value:
                value = (
                    f"<<<HIRDA_COGNITIVE_RESULT:{request_id}>>>\n"
                    f"{value}\n"
                    "<<<END_HIRDA_COGNITIVE_RESULT>>>"
                )
            return {
                "content": [
                    {
                        "type": "text",
                        "text": value,
                    }
                ]
            }
        return {}


class FakeHerdr:
    def __init__(self):
        self.calls = []
        self.fail_prompt_panes = set()
        self.request_ids = {}
        self.waited_panes = set()
        self.outputs = {
            "wH:p1": "HERMES_RESULT",
            "wC:p1": "CLAUDE_RESULT",
        }
        self.snapshot = {
            "status": "healthy",
            "panes": {
                "panes": [
                    {"pane_id": "wH:p1", "agent": "hermes", "agent_status": "idle"},
                    {"pane_id": "wC:p1", "agent": "claude", "agent_status": "idle"},
                ]
            },
        }
        self.tools = {
            "herdr_prompt_agent": PROMPT_TOOL,
            "herdr_wait_agent": WAIT_TOOL,
            "herdr_read_agent": READ_TOOL,
        }

    async def refresh(self):
        return self.snapshot

    def tool(self, name):
        return self.tools.get(name)

    def client(self, **kwargs):
        return FakeClient(self)

    def find_pane(self, *, pane_id=None, workspace=None, agent=None):
        panes = self.snapshot["panes"]["panes"]
        if pane_id:
            return next((pane for pane in panes if pane["pane_id"] == pane_id), None)
        if agent:
            return next((pane for pane in panes if pane["agent"] == agent), None)
        return panes[0] if panes else None


class FakeRuntimes:
    def snapshot(self, *, force=False):
        return {
            "runtimes": [
                {
                    "id": "hermes",
                    "name": "Hermes",
                    "brand": "Nous / custom",
                    "provider": "Nous",
                    "model": "upstage/solar-pro4:free",
                    "installed": True,
                    "observed": True,
                    "status": "ready",
                    "free": True,
                    "auth_health": {"status": "healthy"},
                    "limit_health": {"status": "healthy"},
                },
                {
                    "id": "claude",
                    "name": "Claude",
                    "brand": "Anthropic",
                    "provider": "Anthropic",
                    "model": "sonnet",
                    "installed": True,
                    "observed": True,
                    "status": "ready",
                    "free": False,
                    "auth_health": {"status": "healthy"},
                    "limit_health": {"status": "healthy"},
                },
            ]
        }


class ProfiledHerdr(FakeHerdr):
    def __init__(self):
        super().__init__()
        self.snapshot["panes"]["panes"].append(
            {"pane_id": "wX:p1", "agent": "codex", "agent_status": "idle"}
        )
        self.outputs["wX:p1"] = "CODEX_RESULT"


class ProfiledRuntimes(FakeRuntimes):
    def __init__(self, *, codex_limit_status: str = "healthy"):
        self.codex_limit_status = codex_limit_status

    def snapshot(self, *, force=False):
        data = super().snapshot(force=force)
        by_id = {row["id"]: row for row in data["runtimes"]}
        for runtime_id, row in by_id.items():
            profile = AgentRuntimeInventory.CAPABILITY_PROFILES[runtime_id]
            row["capability_profile"] = {
                "schema": AgentRuntimeInventory.CAPABILITY_PROFILE_SCHEMA,
                "source": "builtin_runtime_profile",
                **profile,
                "skills": dict(profile["skills"]),
            }
        codex_profile = AgentRuntimeInventory.CAPABILITY_PROFILES["codex"]
        data["runtimes"].append(
            {
                "id": "codex",
                "name": "Codex",
                "brand": "OpenAI",
                "provider": "OpenAI",
                "model": "gpt-codex",
                "installed": True,
                "observed": True,
                "status": "ready",
                "free": False,
                "auth_health": {"status": "healthy"},
                "limit_health": {"status": self.codex_limit_status},
                "capability_profile": {
                    "schema": AgentRuntimeInventory.CAPABILITY_PROFILE_SCHEMA,
                    "source": "builtin_runtime_profile",
                    **codex_profile,
                    "skills": dict(codex_profile["skills"]),
                },
            }
        )
        return data


class MutableRuntimeHealth(FakeRuntimes):
    def __init__(self):
        self.claude_status = "ready"
        self.claude_auth = "healthy"
        self.claude_limit = "healthy"

    def snapshot(self, *, force=False):
        data = super().snapshot(force=force)
        claude = next(row for row in data["runtimes"] if row["id"] == "claude")
        claude["status"] = self.claude_status
        claude["auth_health"] = {"status": self.claude_auth}
        claude["limit_health"] = {"status": self.claude_limit}
        return data


class FakeLaneStates:
    def __init__(self, **states):
        self.states = states
        self.actors = {agent: "test" for agent in ("claude", "codex", "hermes")}
        self.reasons = {agent: None for agent in ("claude", "codex", "hermes")}
        self.calls = []

    async def lane_states(self):
        return {
            agent: {
                "agent": agent,
                "state": self.states.get(agent, "normal"),
                "reason": self.reasons.get(agent),
                "actor": self.actors.get(agent, "test"),
                "updated_at": None,
            }
            for agent in ("claude", "codex", "hermes")
        }

    async def set_lane_state(
        self,
        agent,
        state,
        *,
        reason=None,
        actor="operator",
        auto_handoff=True,
    ):
        previous = (await self.lane_states())[agent]
        self.states[agent] = state
        self.actors[agent] = actor
        self.reasons[agent] = reason
        lane = (await self.lane_states())[agent]
        self.calls.append(
            {
                "agent": agent,
                "state": state,
                "reason": reason,
                "actor": actor,
                "auto_handoff": auto_handoff,
            }
        )
        return {"lane": lane, "previous": previous, "auto_handoff": []}


def studio():
    return SimpleNamespace(
        jev_enabled=False,
        jev_mode="shadow",
        jev_model="jev-latest",
        jev_min_confidence=0.90,
    )


@pytest.mark.asyncio
async def test_fast_lane_prefers_hermes_free_runtime():
    router = CognitiveRouter(studio(), FakeHerdr(), FakeRuntimes())
    result = await router.execute(
        {
            "request_id": "COG-FAST",
            "prompt": "Summarize this short note.",
            "capability": "fast_utility",
        }
    )
    assert result.status == "success"
    assert result.lane == "fast"
    assert result.runtime == "hermes"
    assert result.output == "HERMES_RESULT"
    assert result.fallback_used is False
    assert result.identity_continuity is True
    assert result.semantic_authority_changed is False
    assert result.task_semantic_profile["task_type"] == "utility"
    assert result.task_semantic_profile["reasoning_depth"] == "fast"
    assert result.task_semantic_profile["routing_authority"] is False

@pytest.mark.asyncio
async def test_result_marker_isolates_current_answer_from_pane_history():
    herdr = FakeHerdr()
    herdr.outputs["wH:p1"] = (
        "old pane transcript\n"
        "<<<HIRDA_COGNITIVE_RESULT:COG-MARKER:A1>>>\n"
        "CURRENT_RESULT\n"
        "<<<END_HIRDA_COGNITIVE_RESULT>>>\n"
        "shell prompt"
    )
    router = CognitiveRouter(studio(), herdr, FakeRuntimes())
    result = await router.execute(
        {
            "request_id": "COG-MARKER",
            "prompt": "Return the current result.",
            "capability": "fast_utility",
        }
    )
    assert result.status == "success"
    assert result.output == "CURRENT_RESULT"


@pytest.mark.asyncio
async def test_result_marker_normalizes_escaped_boundary_newlines_only():
    herdr = FakeHerdr()
    herdr.outputs["wH:p1"] = (
        "<<<HIRDA_COGNITIVE_RESULT:COG-ESCAPED:A1>>>"
        "\\nCURRENT_RESULT\\n"
        "<<<END_HIRDA_COGNITIVE_RESULT>>>"
    )
    router = CognitiveRouter(studio(), herdr, FakeRuntimes())
    result = await router.execute(
        {
            "request_id": "COG-ESCAPED",
            "prompt": "Return the current result.",
            "capability": "fast_utility",
        }
    )
    assert result.status == "success"
    assert result.output == "CURRENT_RESULT"


def test_extract_marked_result_from_nested_herdr_envelope():
    start = "<<<HIRDA_COGNITIVE_RESULT:NESTED>>>"
    end = "<<<END_HIRDA_COGNITIVE_RESULT>>>"
    payload = {
        "result": {
            "content": [
                {"type": "text", "text": f"noise\\n{start}\\nNESTED_OK\\n{end}\\n$ "}
            ]
        }
    }
    assert _extract_marked_result(payload, start, end) == "NESTED_OK"


def test_extract_marked_result_when_markers_are_split_across_fragments():
    start = "<<<HIRDA_COGNITIVE_RESULT:SPLIT>>>"
    end = "<<<END_HIRDA_COGNITIVE_RESULT>>>"
    payload = ["old transcript", start, "SPLIT_OK", end, "prompt"]
    assert _extract_marked_result(payload, start, end) == "SPLIT_OK"


def test_extract_marked_result_ignores_protocol_instruction_markers():
    start = "<<<HIRDA_COGNITIVE_RESULT:PROTOCOL>>>"
    end = "<<<END_HIRDA_COGNITIVE_RESULT>>>"
    payload = (
        f"1. First line: {start}\n"
        "2. Then write the complete ACTUAL answer to the request.\n"
        f"3. Final line: {end}\n"
        f"{start}\n"
        "ACTUAL_OK\n"
        f"{end}"
    )
    assert _extract_marked_result(payload, start, end) == "ACTUAL_OK"


def test_extract_marked_result_accepts_known_terminal_bullet_prefix():
    start = "<<<HIRDA_COGNITIVE_RESULT:BULLET>>>"
    end = "<<<END_HIRDA_COGNITIVE_RESULT>>>"
    payload = f"● {start}\nBULLET_OK\n{end}"
    assert _extract_marked_result(payload, start, end) == "BULLET_OK"


def test_extract_marked_result_reconstructs_soft_wrapped_terminal_marker():
    start = "<<<HIRDA_COGNITIVE_RESULT:COG-DEDICATED-HERMES-EXEC2>>>"
    end = "<<<END_HIRDA_COGNITIVE_RESULT>>>"
    payload = (
        "<<<HIRDA_COGNITIVE_RESULT:COG-DEDICATED-HERMES-EXEC2\n"
        ">>>\n"
        "HIRDA_HERMES_CERT_OK\n"
        f"{end}"
    )
    assert _extract_marked_result(payload, start, end) == "HIRDA_HERMES_CERT_OK"


@pytest.mark.asyncio
async def test_unmarked_pane_history_fails_over_instead_of_becoming_cognitive_output():
    herdr = FakeHerdr()
    herdr.outputs["wH:p1"] = (
        "old pane transcript\n"
        "<<<HIRDA_COGNITIVE_RESULT:OLD-REQUEST>>>\n"
        "STALE_RESULT\n"
        "<<<END_HIRDA_COGNITIVE_RESULT>>>"
    )
    router = CognitiveRouter(studio(), herdr, FakeRuntimes())
    result = await router.execute(
        {
            "request_id": "COG-UNMARKED",
            "prompt": "Return the current result.",
            "capability": "fast_utility",
            "preferred_depth": "fast",
        }
    )
    assert result.status == "success"
    assert result.runtime == "claude"
    assert result.fallback_used is True
    assert result.output == "CLAUDE_RESULT"
    assert result.attempts[0]["runtime"] == "hermes"
    assert result.attempts[0]["status"] == "failed"
    assert "markers_missing" in result.attempts[0]["detail"]

@pytest.mark.asyncio
async def test_empty_marked_result_fails_over_instead_of_becoming_success():
    herdr = FakeHerdr()
    herdr.outputs["wH:p1"] = (
        "<<<HIRDA_COGNITIVE_RESULT:COG-EMPTY:A1>>>\n"
        "\n"
        "<<<END_HIRDA_COGNITIVE_RESULT>>>"
    )
    router = CognitiveRouter(studio(), herdr, FakeRuntimes())
    result = await router.execute(
        {
            "request_id": "COG-EMPTY",
            "prompt": "Return the current result.",
            "capability": "fast_utility",
            "preferred_depth": "fast",
        }
    )
    assert result.status == "success"
    assert result.runtime == "claude"
    assert result.fallback_used is True
    assert result.output == "CLAUDE_RESULT"
    assert "empty_marked_cognitive_result" in result.attempts[0]["detail"]


@pytest.mark.asyncio
async def test_prompt_contains_markers_not_placeholder():
    """Regression: prompt sent to runtime must contain both exact markers
    and must NOT contain the literal placeholder '<final answer here>'."""
    herdr = FakeHerdr()
    router = CognitiveRouter(studio(), herdr, FakeRuntimes())
    await router.execute(
        {
            "request_id": "COG-PROMPT",
            "prompt": "Return exactly HELLO.",
            "capability": "fast_utility",
        }
    )
    # Find the prompt tool call
    prompt_calls = [c for c in herdr.calls if c[0] == "herdr_prompt_agent"]
    assert prompt_calls, "herdr_prompt_agent was not called"
    sent_prompt = prompt_calls[0][1].get("prompt", "")
    # Must contain both markers
    assert "<<<HIRDA_COGNITIVE_RESULT:COG-PROMPT:A1>>>" in sent_prompt
    assert "<<<END_HIRDA_COGNITIVE_RESULT>>>" in sent_prompt
    # Must NOT present an empty marker pair for the runtime to copy.
    assert (
        "<<<HIRDA_COGNITIVE_RESULT:COG-PROMPT:A1>>>\n"
        "<<<END_HIRDA_COGNITIVE_RESULT>>>"
    ) not in sent_prompt
    # Must NOT contain the literal placeholder
    assert "<final answer here>" not in sent_prompt
    # Output-format wording inside the task must be scoped to the payload so
    # requests such as "Return exactly X" cannot suppress HIRDA framing.
    assert "HIRDA TRANSPORT CONTRACT" in sent_prompt
    assert "applies ONLY to the payload BETWEEN the marker lines" in sent_prompt
    assert "REQUEST PAYLOAD (content instructions only):" in sent_prompt


@pytest.mark.asyncio
async def test_missing_markers_get_one_bounded_repair_retry():
    class RepairClient(FakeClient):
        async def call_tool(self, name, arguments, client_name="test"):
            pane = arguments.get("pane_id")
            if name == "herdr_read_agent" and pane == "wH:p1":
                self.herdr.read_count += 1
                if self.herdr.read_count <= 8:
                    return {"content": [{"type": "text", "text": "UNMARKED_CURRENT_ANSWER"}]}
            return await super().call_tool(name, arguments, client_name=client_name)

    class RepairHerdr(FakeHerdr):
        def __init__(self):
            super().__init__()
            self.read_count = 0

        def client(self, **kwargs):
            return RepairClient(self)

    herdr = RepairHerdr()
    router = CognitiveRouter(studio(), herdr, FakeRuntimes())
    result = await router.execute(
        {
            "request_id": "COG-REPAIR",
            "prompt": "Return the bounded current result.",
            "capability": "fast_utility",
            "preferred_depth": "fast",
            "preferred_runtime": "hermes",
            "allow_fallback": False,
            "timeout_seconds": 30,
        }
    )

    assert result.status == "success"
    assert result.runtime == "hermes"
    assert result.output == "HERMES_RESULT"
    prompt_calls = [call for call in herdr.calls if call[0] == "herdr_prompt_agent"]
    assert len(prompt_calls) == 2
    assert "HIRDA RESULT PROTOCOL REPAIR" in prompt_calls[1][1]["prompt"]
    assert "HIRDA TRANSPORT CONTRACT" in prompt_calls[1][1]["prompt"]
    assert "applies ONLY to the payload BETWEEN the marker lines" in prompt_calls[1][1]["prompt"]
    assert "COG-REPAIR" in prompt_calls[1][1]["prompt"]


@pytest.mark.asyncio
async def test_terminal_marker_miss_uses_session_transcript_fallback():
    class TranscriptClient(FakeClient):
        async def call_tool(self, name, arguments, client_name="test"):
            if name == "herdr_read_agent":
                pane = arguments.get("pane_id")
                self.herdr.calls.append((name, dict(arguments), client_name))
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": "Worked for 17s · done",
                        }
                    ]
                }
            return await super().call_tool(
                name,
                arguments,
                client_name=client_name,
            )

    class TranscriptHerdr(FakeHerdr):
        def client(self, **kwargs):
            return TranscriptClient(self)

        def read_session_result(self, pane, request_id):
            return (
                f"<<<HIRDA_COGNITIVE_RESULT:{request_id}>>>\n"
                "TRANSCRIPT_FALLBACK_OK\n"
                "<<<END_HIRDA_COGNITIVE_RESULT>>>"
            )

    router = CognitiveRouter(studio(), TranscriptHerdr(), FakeRuntimes())
    result = await router.execute(
        {
            "request_id": "COG-TRANSCRIPT-FALLBACK",
            "prompt": "Return the current result.",
            "capability": "coding",
            "preferred_runtime": "claude",
            "allow_fallback": False,
            "timeout_seconds": 30,
        }
    )

    assert result.status == "success"
    assert result.runtime == "claude"
    assert result.output == "TRANSCRIPT_FALLBACK_OK"
    assert result.fallback_used is False


@pytest.mark.asyncio
async def test_terminal_marker_miss_reads_correlated_session_provider_error():
    class TranscriptErrorClient(FakeClient):
        async def call_tool(self, name, arguments, client_name="test"):
            pane = arguments.get("pane_id")
            if name == "herdr_read_agent" and pane == "wC:p1":
                self.herdr.calls.append((name, dict(arguments), client_name))
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": "Worked for 22s · done",
                        }
                    ]
                }
            return await super().call_tool(
                name,
                arguments,
                client_name=client_name,
            )

    class TranscriptErrorHerdr(FakeHerdr):
        def client(self, **kwargs):
            return TranscriptErrorClient(self)

        def read_session_result(self, pane, request_id):
            return None

        def read_session_error(self, pane, request_id):
            if str((pane or {}).get("agent") or "") != "claude":
                return None
            return (
                "HIRDA COGNITIVE REQUEST\n"
                f"request_id: {request_id}\n"
                "API Error: 503 [codex/gpt-6-astra] [429]: "
                "The usage limit has been reached"
            )

    lanes = FakeLaneStates()
    router = CognitiveRouter(
        studio(),
        TranscriptErrorHerdr(),
        FakeRuntimes(),
        capsules=lanes,
    )
    result = await router.execute(
        {
            "request_id": "COG-TRANSCRIPT-429",
            "prompt": "Deep request.",
            "capability": "reasoning_high",
            "allow_fallback": True,
            "timeout_seconds": 30,
        }
    )

    assert result.status == "success"
    assert result.runtime == "hermes"
    assert result.attempts[0]["runtime"] == "claude"
    assert result.attempts[0]["error"] == "runtime_quota_exhausted"
    assert result.attempts[0]["runtime_backoff"]["active"] is True
    lane = (await lanes.lane_states())["claude"]
    assert lane["state"] == "emergency"
    assert "runtime_quota_exhausted" in str(lane["reason"])


@pytest.mark.asyncio
async def test_delayed_marker_visibility_retries_read_before_repair_prompt():
    class DelayedReadClient(FakeClient):
        async def call_tool(self, name, arguments, client_name="test"):
            pane = arguments.get("pane_id")
            if name == "herdr_read_agent" and pane == "wH:p1":
                self.herdr.read_count += 1
                if self.herdr.read_count == 1:
                    return {"content": [{"type": "text", "text": "PANE_OUTPUT_NOT_COMMITTED_YET"}]}
            return await super().call_tool(name, arguments, client_name=client_name)

    class DelayedReadHerdr(FakeHerdr):
        def __init__(self):
            super().__init__()
            self.read_count = 0

        def client(self, **kwargs):
            return DelayedReadClient(self)

    herdr = DelayedReadHerdr()
    router = CognitiveRouter(studio(), herdr, FakeRuntimes())
    result = await router.execute(
        {
            "request_id": "COG-DELAYED-READ",
            "prompt": "Return the bounded current result.",
            "capability": "fast_utility",
            "preferred_depth": "fast",
            "preferred_runtime": "hermes",
            "allow_fallback": False,
            "timeout_seconds": 30,
        }
    )

    assert result.status == "success"
    assert result.runtime == "hermes"
    assert result.output == "HERMES_RESULT"
    assert herdr.read_count == 2
    prompt_calls = [call for call in herdr.calls if call[0] == "herdr_prompt_agent"]
    assert len(prompt_calls) == 1


@pytest.mark.asyncio
async def test_read_requests_wider_window_when_live_schema_supports_lines():
    class LinesAwareHerdr(FakeHerdr):
        def __init__(self):
            super().__init__()
            read_tool = dict(READ_TOOL)
            read_schema = dict(read_tool["inputSchema"])
            props = dict(read_schema["properties"])
            props["lines"] = {"type": "integer"}
            read_schema["properties"] = props
            read_tool["inputSchema"] = read_schema
            self.tools["herdr_read_agent"] = read_tool

    herdr = LinesAwareHerdr()
    router = CognitiveRouter(studio(), herdr, FakeRuntimes())
    result = await router.execute(
        {
            "request_id": "COG-WIDE-READ",
            "prompt": "Return the bounded current result.",
            "capability": "fast_utility",
            "preferred_runtime": "hermes",
            "allow_fallback": False,
            "timeout_seconds": 30,
        }
    )

    assert result.status == "success"
    read_calls = [call for call in herdr.calls if call[0] == "herdr_read_agent"]
    assert read_calls
    assert read_calls[0][1]["lines"] == 240


@pytest.mark.asyncio
async def test_prompt_wait_completion_skips_redundant_wait_barrier():
    class WaitAwareClient(FakeClient):
        async def call_tool(self, name, arguments, client_name="test"):
            if name == "herdr_prompt_agent" and arguments.get("wait") is True:
                pane = arguments.get("pane_id")
                self.herdr.waited_panes.add(pane)
            return await super().call_tool(name, arguments, client_name=client_name)

    class WaitAwareHerdr(FakeHerdr):
        def __init__(self):
            super().__init__()
            prompt_tool = dict(PROMPT_TOOL)
            prompt_schema = dict(prompt_tool["inputSchema"])
            props = dict(prompt_schema["properties"])
            props["wait"] = {"type": "boolean"}
            prompt_schema["properties"] = props
            prompt_tool["inputSchema"] = prompt_schema
            self.tools["herdr_prompt_agent"] = prompt_tool

        def client(self, **kwargs):
            return WaitAwareClient(self)

    herdr = WaitAwareHerdr()
    router = CognitiveRouter(studio(), herdr, FakeRuntimes())
    result = await router.execute(
        {
            "request_id": "COG-WAIT-BARRIER",
            "prompt": "Return the current result.",
            "capability": "fast_utility",
            "preferred_runtime": "hermes",
            "allow_fallback": False,
        }
    )

    assert result.status == "success"
    prompt_calls = [call for call in herdr.calls if call[0] == "herdr_prompt_agent"]
    wait_calls = [call for call in herdr.calls if call[0] == "herdr_wait_agent"]
    assert prompt_calls
    assert prompt_calls[0][1]["wait"] is True
    assert wait_calls == []


@pytest.mark.asyncio
async def test_concurrent_cognitive_requests_are_serialized_per_pane():
    class OverlapDetectClient(FakeClient):
        async def call_tool(self, name, arguments, client_name="test"):
            if name != "herdr_prompt_agent":
                return await super().call_tool(
                    name,
                    arguments,
                    client_name=client_name,
                )
            self.herdr.active_prompt_calls += 1
            self.herdr.max_active_prompt_calls = max(
                self.herdr.max_active_prompt_calls,
                self.herdr.active_prompt_calls,
            )
            try:
                await asyncio.sleep(0.02)
                return await super().call_tool(
                    name,
                    arguments,
                    client_name=client_name,
                )
            finally:
                self.herdr.active_prompt_calls -= 1

    class OverlapDetectHerdr(FakeHerdr):
        def __init__(self):
            super().__init__()
            self.active_prompt_calls = 0
            self.max_active_prompt_calls = 0

        def client(self, **kwargs):
            return OverlapDetectClient(self)

    herdr = OverlapDetectHerdr()
    router = CognitiveRouter(studio(), herdr, FakeRuntimes())

    first, second = await asyncio.gather(
        router.execute(
            {
                "request_id": "COG-SERIAL-ONE",
                "prompt": "Return ONE.",
                "capability": "fast_utility",
                "preferred_runtime": "hermes",
                "allow_fallback": False,
                "timeout_seconds": 5,
            }
        ),
        router.execute(
            {
                "request_id": "COG-SERIAL-TWO",
                "prompt": "Return TWO.",
                "capability": "fast_utility",
                "preferred_runtime": "hermes",
                "allow_fallback": False,
                "timeout_seconds": 5,
            }
        ),
    )

    assert first.status == "success"
    assert second.status == "success"
    assert herdr.max_active_prompt_calls == 1


@pytest.mark.asyncio
async def test_claude_wait_barrier_targets_done_state_when_prompt_wait_is_unavailable():
    herdr = FakeHerdr()
    router = CognitiveRouter(studio(), herdr, FakeRuntimes())
    result = await router.execute(
        {
            "request_id": "COG-CLAUDE-DONE-BARRIER",
            "prompt": "Return the current result.",
            "capability": "reasoning_high",
            "preferred_depth": "deep",
            "preferred_runtime": "claude",
            "allow_fallback": False,
            "timeout_seconds": 30,
        }
    )

    assert result.status == "success"
    wait_calls = [call for call in herdr.calls if call[0] == "herdr_wait_agent"]
    assert len(wait_calls) == 1
    assert wait_calls[0][1]["until"] == "done"


@pytest.mark.asyncio
async def test_deep_lane_prefers_claude():
    router = CognitiveRouter(studio(), FakeHerdr(), FakeRuntimes())
    result = await router.execute(
        {
            "request_id": "COG-DEEP",
            "prompt": "Reason through a multi-step architecture conflict.",
            "capability": "reasoning_high",
        }
    )
    assert result.status == "success"
    assert result.lane == "deep"
    assert result.runtime == "claude"
    assert result.output == "CLAUDE_RESULT"


@pytest.mark.asyncio
async def test_disabled_claude_lane_is_removed_from_deep_candidates():
    router = CognitiveRouter(
        studio(),
        FakeHerdr(),
        FakeRuntimes(),
        FakeLaneStates(claude="disabled"),
    )
    result = await router.execute(
        {
            "request_id": "COG-DEEP-CLAUDE-DISABLED",
            "prompt": "Continue a complex task while Claude is disabled.",
            "capability": "reasoning_high",
        }
    )
    assert result.status == "success"
    assert result.lane == "deep"
    assert result.runtime == "hermes"
    plan = await router.plan(
        {
            "request_id": "COG-PLAN-CLAUDE-DISABLED",
            "prompt": "Plan a complex task.",
            "capability": "reasoning_high",
        }
    )
    assert plan["lane_states"]["claude"]["state"] == "disabled"
    assert [candidate["runtime"] for candidate in plan["candidates"]] == ["hermes"]


@pytest.mark.asyncio
async def test_runtime_health_failure_moves_normal_lane_to_hirda_emergency():
    cfg = studio()
    cfg.cognitive_runtime_recovery_successes = 2
    runtimes = MutableRuntimeHealth()
    runtimes.claude_status = "down"
    lanes = FakeLaneStates()
    router = CognitiveRouter(cfg, FakeHerdr(), runtimes, lanes)

    plan = await router.plan(
        {
            "request_id": "COG-CLAUDE-DOWN",
            "prompt": "Plan a deep task while Claude is unavailable.",
            "capability": "reasoning_high",
        }
    )

    assert plan["lane_states"]["claude"]["state"] == "emergency"
    assert lanes.actors["claude"] == "hirda/runtime-health"
    assert lanes.reasons["claude"] == "runtime_health:runtime_status_down"
    assert lanes.calls[-1]["auto_handoff"] is True
    assert [candidate["runtime"] for candidate in plan["candidates"]] == ["hermes"]


@pytest.mark.asyncio
async def test_hirda_runtime_lane_recovers_after_consecutive_healthy_checks():
    cfg = studio()
    cfg.cognitive_runtime_recovery_successes = 2
    runtimes = MutableRuntimeHealth()
    runtimes.claude_status = "down"
    lanes = FakeLaneStates()
    router = CognitiveRouter(cfg, FakeHerdr(), runtimes, lanes)

    await router.plan(
        {
            "request_id": "COG-CLAUDE-DOWN-FIRST",
            "prompt": "Detect the Claude outage.",
            "capability": "reasoning_high",
        }
    )
    assert lanes.states["claude"] == "emergency"

    runtimes.claude_status = "ready"
    first_healthy = await router.plan(
        {
            "request_id": "COG-CLAUDE-HEALTHY-1",
            "prompt": "Check recovery once.",
            "capability": "reasoning_high",
        }
    )
    assert first_healthy["lane_states"]["claude"]["state"] == "emergency"

    second_healthy = await router.plan(
        {
            "request_id": "COG-CLAUDE-HEALTHY-2",
            "prompt": "Check recovery twice.",
            "capability": "reasoning_high",
        }
    )
    assert second_healthy["lane_states"]["claude"]["state"] == "normal"
    assert lanes.calls[-1]["actor"] == "hirda/runtime-health"
    assert lanes.calls[-1]["auto_handoff"] is False
    assert lanes.calls[-1]["reason"] == "runtime_recovered_after_2_healthy_checks"
    assert second_healthy["candidates"][0]["runtime"] == "claude"


@pytest.mark.asyncio
async def test_runtime_recovery_never_reopens_operator_owned_lane():
    cfg = studio()
    cfg.cognitive_runtime_recovery_successes = 1
    lanes = FakeLaneStates(claude="disabled")
    runtimes = MutableRuntimeHealth()
    router = CognitiveRouter(cfg, FakeHerdr(), runtimes, lanes)

    plan = await router.plan(
        {
            "request_id": "COG-MANUAL-CLAUDE-DISABLED",
            "prompt": "Respect the operator-owned lane state.",
            "capability": "reasoning_high",
        }
    )

    assert plan["lane_states"]["claude"]["state"] == "disabled"
    assert lanes.calls == []
    assert [candidate["runtime"] for candidate in plan["candidates"]] == ["hermes"]


@pytest.mark.asyncio
async def test_fast_lane_fails_over_from_hermes_to_claude():
    herdr = FakeHerdr()
    herdr.fail_prompt_panes.add("wH:p1")
    router = CognitiveRouter(studio(), herdr, FakeRuntimes())
    result = await router.execute(
        {
            "request_id": "COG-FAILOVER",
            "prompt": "Routine request that should survive Hermes failure.",
            "capability": "fast_utility",
        }
    )
    assert result.status == "success"
    assert result.runtime == "claude"
    assert result.fallback_used is True
    assert result.degraded is True
    assert [item["runtime"] for item in result.attempts] == ["hermes", "claude"]
    assert result.attempts[0]["status"] == "failed"
    assert result.attempts[0]["attempt_id"] == "COG-FAILOVER:A1"
    assert result.attempts[0]["pane_id"] == "wH:p1"
    assert result.attempts[0]["accepted"] is False
    assert result.attempts[1]["status"] == "success"
    assert result.attempts[1]["attempt_id"] == "COG-FAILOVER:A2"
    assert result.attempts[1]["pane_id"] == "wC:p1"
    assert result.attempts[1]["accepted"] is True
    prompt_calls = [call for call in herdr.calls if call[0] == "herdr_prompt_agent"]
    assert "request_id: COG-FAILOVER:A1" in prompt_calls[0][1]["prompt"]
    assert "parent_request_id: COG-FAILOVER" in prompt_calls[0][1]["prompt"]
    assert "<<<HIRDA_COGNITIVE_RESULT:COG-FAILOVER:A1>>>" in prompt_calls[0][1]["prompt"]
    assert "request_id: COG-FAILOVER:A2" in prompt_calls[1][1]["prompt"]
    assert "parent_request_id: COG-FAILOVER" in prompt_calls[1][1]["prompt"]
    assert "<<<HIRDA_COGNITIVE_RESULT:COG-FAILOVER:A2>>>" in prompt_calls[1][1]["prompt"]


@pytest.mark.asyncio
async def test_fast_lane_skips_busy_hermes_and_falls_back_to_claude():
    herdr = FakeHerdr()
    for pane in herdr.snapshot["panes"]["panes"]:
        if pane["agent"] == "hermes":
            pane["agent_status"] = "working"
    router = CognitiveRouter(studio(), herdr, FakeRuntimes())
    result = await router.execute(
        {
            "request_id": "COG-BUSY-HERMES",
            "prompt": "Routine request should not be dispatched into a busy Hermes pane.",
            "capability": "fast_utility",
            "preferred_depth": "fast",
            "allow_fallback": True,
        }
    )
    assert result.status == "success"
    assert result.runtime == "claude"
    assert result.fallback_used is True
    assert result.degraded is True
    assert result.attempts[0]["runtime"] == "hermes"
    assert result.attempts[0]["error"] == "runtime_busy"
    assert result.attempts[1]["runtime"] == "claude"
    assert result.attempts[1]["status"] == "success"


@pytest.mark.asyncio
async def test_owned_enforced_pane_waits_for_busy_lifecycle_to_settle():
    class SettlingOwnedHerdr(FakeHerdr):
        def __init__(self):
            super().__init__()
            self.refresh_count = 0
            for pane in self.snapshot["panes"]["panes"]:
                if pane["agent"] == "hermes":
                    pane["agent_status"] = "working"

        async def refresh(self):
            self.refresh_count += 1
            if self.refresh_count >= 2:
                for pane in self.snapshot["panes"]["panes"]:
                    if pane["agent"] == "hermes":
                        pane["agent_status"] = "idle"
            return self.snapshot

    herdr = SettlingOwnedHerdr()
    cfg = studio()
    cfg.cognitive_pane_routing_mode = "enforce"
    cfg.cognitive_runtime_bindings = {
        "hermes": {
            "machine_id": "openclaw",
            "backend_id": "hermes-runtime",
            "pane_ids": ["wH:p1"],
        }
    }
    router = CognitiveRouter(cfg, herdr, FakeRuntimes())

    result = await router.execute(
        {
            "request_id": "COG-OWNED-BUSY-SETTLES",
            "prompt": "Return the dedicated lane result.",
            "capability": "fast_utility",
            "preferred_runtime": "hermes",
            "allow_fallback": False,
            "timeout_seconds": 5,
        }
    )

    assert result.status == "success"
    assert result.runtime == "hermes"
    assert result.fallback_used is False
    assert herdr.refresh_count >= 2


@pytest.mark.asyncio
async def test_busy_preferred_runtime_without_fallback_fails_cleanly():
    herdr = FakeHerdr()
    for pane in herdr.snapshot["panes"]["panes"]:
        if pane["agent"] == "hermes":
            pane["agent_status"] = "working"
    router = CognitiveRouter(studio(), herdr, FakeRuntimes())
    result = await router.execute(
        {
            "request_id": "COG-BUSY-NO-FALLBACK",
            "prompt": "Do not dispatch into a busy runtime.",
            "capability": "fast_utility",
            "preferred_depth": "fast",
            "preferred_runtime": "hermes",
            "allow_fallback": False,
        }
    )
    assert result.status == "failed"
    assert result.fallback_used is False
    assert result.attempts == (
        {
            "attempt_id": "COG-BUSY-NO-FALLBACK:A1",
            "runtime": "hermes",
            "pane_id": "wH:p1",
            "status": "failed",
            "accepted": False,
            "error": "runtime_busy",
            "detail": "cognitive runtime pane is working",
        },
    )


@pytest.mark.asyncio
async def test_context_exhausted_runtime_fails_over_with_specific_error():
    class ContextErrorClient(FakeClient):
        async def call_tool(self, name, arguments, client_name="test"):
            pane = arguments.get("pane_id")
            if name == "herdr_read_agent" and pane == "wC:p1":
                request_id = self.herdr.request_ids.get(pane, "")
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "HIRDA COGNITIVE REQUEST\n"
                                f"request_id: {request_id}\n"
                                "● [Error] Your input exceeds the context window of this model."
                            ),
                        }
                    ]
                }
            return await super().call_tool(name, arguments, client_name=client_name)

    class ContextErrorHerdr(FakeHerdr):
        def client(self, **kwargs):
            return ContextErrorClient(self)

    herdr = ContextErrorHerdr()
    router = CognitiveRouter(studio(), herdr, FakeRuntimes())
    result = await router.execute(
        {
            "request_id": "COG-CONTEXT-FALLBACK",
            "prompt": "Complex request should fall back if Claude context is exhausted.",
            "capability": "reasoning_high",
            "preferred_depth": "deep",
            "allow_fallback": True,
        }
    )

    assert result.status == "success"
    assert result.runtime == "hermes"
    assert result.fallback_used is True
    assert result.degraded is True
    assert result.attempts[0]["runtime"] == "claude"
    assert result.attempts[0]["error"] == "runtime_context_exhausted"
    assert result.attempts[1]["runtime"] == "hermes"
    assert result.attempts[1]["status"] == "success"


@pytest.mark.parametrize(
    ("terminal_text", "expected"),
    [
        ("⏳ rate limited — resets in ~7m, retrying in 419s", "runtime_rate_limited"),
        ("Provider said: HTTP 401: Your API key is invalid", "runtime_auth_failed"),
        ("Your plan does not include paid service access; upgrade or add credits", "runtime_quota_exhausted"),
        ("API Error: 503 [429]: The usage limit has been reached", "runtime_quota_exhausted"),
    ],
)
def test_runtime_terminal_error_classifies_provider_failures(terminal_text, expected):
    request_id = "COG-PROVIDER-FAILURE"
    value = (
        "HIRDA COGNITIVE REQUEST\n"
        f"request_id: {request_id}\n"
        f"{terminal_text}"
    )
    assert _runtime_terminal_error(value, request_id) == expected


@pytest.mark.asyncio
async def test_prompt_timeout_reads_correlated_provider_failure_and_falls_back():
    class RateLimitedClient(FakeClient):
        async def call_tool(self, name, arguments, client_name="test"):
            pane = arguments.get("pane_id")
            if name == "herdr_prompt_agent" and pane == "wH:p1":
                raise RuntimeError("HERDR_TIMEOUT")
            if name == "herdr_read_agent" and pane == "wH:p1":
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "HIRDA COGNITIVE REQUEST\n"
                                "request_id: COG-RATE-LIMIT-FALLBACK:A1\n"
                                "⏳ rate limited — resets in ~7m, retrying in 419s"
                            ),
                        }
                    ]
                }
            return await super().call_tool(
                name,
                arguments,
                client_name=client_name,
            )

    class RateLimitedHerdr(FakeHerdr):
        def client(self, **kwargs):
            return RateLimitedClient(self)

    herdr = RateLimitedHerdr()
    router = CognitiveRouter(studio(), herdr, FakeRuntimes())
    result = await router.execute(
        {
            "request_id": "COG-RATE-LIMIT-FALLBACK",
            "prompt": "Return the current result.",
            "capability": "fast_utility",
            "allow_fallback": True,
            "timeout_seconds": 30,
        }
    )

    assert result.status == "success"
    assert result.runtime != "hermes"
    assert result.fallback_used is True
    assert result.attempts[0]["runtime"] == "hermes"
    assert result.attempts[0]["error"] == "runtime_rate_limited"


@pytest.mark.asyncio
async def test_runtime_rate_limit_backoff_skips_lane_on_next_request():
    class ClaudeRateLimitedClient(FakeClient):
        async def call_tool(self, name, arguments, client_name="test"):
            pane = arguments.get("pane_id")
            if name == "herdr_prompt_agent" and pane == "wC:p1":
                prompt = str(arguments.get("prompt") or "")
                request_id = next(
                    (
                        line.split(":", 1)[1].strip()
                        for line in prompt.splitlines()
                        if line.startswith("request_id:")
                    ),
                    "unknown",
                )
                self.herdr.request_ids[pane] = request_id
                raise RuntimeError("provider request failed")
            if name == "herdr_read_agent" and pane == "wC:p1":
                request_id = self.herdr.request_ids.get(pane, "unknown")
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "HIRDA COGNITIVE REQUEST\n"
                                f"request_id: {request_id}\n"
                                "HTTP 429: rate limited, retrying later"
                            ),
                        }
                    ]
                }
            return await super().call_tool(
                name,
                arguments,
                client_name=client_name,
            )

    class ClaudeRateLimitedHerdr(FakeHerdr):
        def client(self, **kwargs):
            return ClaudeRateLimitedClient(self)

    lanes = FakeLaneStates()
    router = CognitiveRouter(
        studio(),
        ClaudeRateLimitedHerdr(),
        FakeRuntimes(),
        capsules=lanes,
    )
    first = await router.execute(
        {
            "request_id": "COG-BACKOFF-FIRST",
            "prompt": "Deep request that should fail over.",
            "capability": "reasoning_high",
            "allow_fallback": True,
            "timeout_seconds": 30,
        }
    )

    assert first.status == "success"
    assert first.runtime == "hermes"
    assert first.attempts[0]["runtime"] == "claude"
    assert first.attempts[0]["error"] == "runtime_rate_limited"
    assert first.attempts[0]["runtime_backoff"]["active"] is True
    lane = (await lanes.lane_states())["claude"]
    assert lane["state"] == "emergency"
    assert lane["actor"] == "hirda/runtime-backoff"
    assert "runtime_rate_limited" in str(lane["reason"])

    second = await router.execute(
        {
            "request_id": "COG-BACKOFF-SECOND",
            "prompt": "Another deep request.",
            "capability": "reasoning_high",
            "allow_fallback": True,
            "timeout_seconds": 30,
        }
    )

    assert second.status == "success"
    assert second.runtime == "hermes"
    assert second.fallback_used is False
    assert [item["runtime"] for item in second.attempts] == ["hermes"]


@pytest.mark.asyncio
async def test_expired_runtime_backoff_reopens_lane_for_probe():
    lanes = FakeLaneStates(claude="emergency")
    lanes.actors["claude"] = "hirda/runtime-backoff"
    lanes.reasons["claude"] = (
        "runtime_backoff;"
        "error=runtime_rate_limited;"
        "until=1.000;"
        "failures=2;"
        "seconds=600.000"
    )
    router = CognitiveRouter(
        studio(),
        FakeHerdr(),
        FakeRuntimes(),
        capsules=lanes,
    )

    plan = await router.plan(
        {
            "request_id": "COG-BACKOFF-PROBE",
            "prompt": "Deep request after backoff expiry.",
            "capability": "reasoning_high",
        }
    )

    assert plan["lane_states"]["claude"]["state"] == "normal"
    assert plan["runtime_backoff"]["runtimes"]["claude"]["active"] is False
    assert plan["runtime_backoff"]["runtimes"]["claude"]["probe_due"] is True
    assert plan["candidates"][0]["runtime"] == "claude"


@pytest.mark.asyncio
async def test_dual_supervisor_specialist_backoff_degrades_team_plan_to_hermes():
    cfg = studio()
    cfg.cognitive_semantic_routing_mode = "canary"
    cfg.cognitive_semantic_canary_min_margin = 0.0
    cfg.cognitive_supervisor_policy_mode = "enforce"
    cfg.cognitive_supervisor_runtime = "claude"
    cfg.cognitive_hermes_team_delegation_mode = "canary"
    cfg.cognitive_hermes_team_require_opt_in = True

    lanes = FakeLaneStates(claude="emergency", codex="emergency")
    for runtime_id, error in (
        ("claude", "runtime_quota_exhausted"),
        ("codex", "runtime_rate_limited"),
    ):
        lanes.actors[runtime_id] = "hirda/runtime-backoff"
        lanes.reasons[runtime_id] = (
            "runtime_backoff;"
            f"error={error};"
            "until=9999999999.000;"
            "failures=1;"
            "seconds=3600.000"
        )

    router = CognitiveRouter(
        cfg,
        ProfiledHerdr(),
        ProfiledRuntimes(),
        capsules=lanes,
    )
    plan = await router.plan(
        {
            "request_id": "COG-DUAL-BACKOFF",
            "prompt": "Implement and test a coding change.",
            "capability": "coding",
            "preferred_depth": "deep",
            "context": {"team_delegation": True},
        }
    )

    assert [item["runtime"] for item in plan["candidates"]] == ["hermes"]
    assert plan["supervisor_policy"]["applied"] is False
    assert "supervisor_runtime_unavailable" in plan["supervisor_policy"]["guard_reasons"]
    assert plan["team_delegation"]["applied"] is False
    assert plan["runtime_backoff"]["active_count"] == 2


def test_runtime_snapshot_overlay_marks_execution_backoff_as_limit_constrained():
    snapshot = FakeRuntimes().snapshot()
    snapshot["summary"] = {"limit_constrained": 0}
    backoff = {
        "schema": "hirda-runtime-backoff-v1",
        "active_count": 1,
        "runtimes": {
            "claude": {
                "runtime": "claude",
                "active": True,
                "error": "runtime_quota_exhausted",
                "until_epoch": 9999999999.0,
                "remaining_seconds": 3600.0,
            }
        },
    }

    decorated = CognitiveRouter.decorate_runtime_snapshot_with_backoff(
        snapshot,
        backoff,
    )
    claude = next(
        row for row in decorated["runtimes"] if row["id"] == "claude"
    )

    assert claude["limit_health"]["status"] == "exhausted"
    assert claude["limit_health"]["source"] == "runtime_backoff"
    assert claude["runtime_backoff"]["active"] is True
    assert decorated["summary"]["limit_constrained"] == 1


@pytest.mark.asyncio
async def test_deep_lane_fails_over_from_claude_to_hermes():
    herdr = FakeHerdr()
    herdr.fail_prompt_panes.add("wC:p1")
    router = CognitiveRouter(studio(), herdr, FakeRuntimes())
    result = await router.execute(
        {
            "request_id": "COG-DEEP-FAILOVER",
            "prompt": "Complex reasoning request that should survive Claude failure.",
            "capability": "reasoning_high",
        }
    )
    assert result.status == "success"
    assert result.runtime == "hermes"
    assert result.fallback_used is True
    assert result.degraded is True
    assert [item["runtime"] for item in result.attempts] == ["claude", "hermes"]
    assert result.attempts[0]["status"] == "failed"
    assert result.attempts[1]["status"] == "success"


@pytest.mark.asyncio
async def test_plan_keeps_jev_advisory_and_external_semantic_authority():
    router = CognitiveRouter(studio(), FakeHerdr(), FakeRuntimes())
    plan = await router.plan(
        {
            "request_id": "COG-PLAN",
            "prompt": "Hello",
            "capability": "social_dialogue",
        }
    )
    assert plan["jev"]["advisory_only"] is True
    assert plan["jev"]["routing_authority"] is False
    assert plan["identity_continuity"] is True
    assert plan["semantic_authority_changed"] is False
    assert plan["task_semantic_profile"]["schema"] == "hirda-task-semantic-profile-v1"
    assert plan["task_semantic_profile"]["task_type"] == "conversation"
    assert plan["task_semantic_profile"]["declared_capability"] == "social_dialogue"
    assert plan["task_semantic_profile"]["source"] == "declared_capability"
    assert plan["task_semantic_profile"]["routing_authority"] is False
    assert plan["candidates"][0]["runtime"] == "hermes"


@pytest.mark.asyncio
async def test_generic_reflection_accepts_jev_semantic_evidence_without_granting_authority(monkeypatch):
    async def fake_evaluate(_studio, request):
        return JevCognitiveJudgment(
            schema="hirda-jev-cognitive-judgment-v1",
            enabled=True,
            evaluated=True,
            mode="teacher",
            request_id=request.request_id,
            suggested_lane="deep",
            confidence=0.97,
            needs_tool=0.94,
            needs_specialist=0.88,
            model="jev-test",
            error=None,
            task_type="software_engineering",
            coding=0.98,
            latency_priority="quality",
        )

    monkeypatch.setattr(
        "mcp_studio.cognitive_router.evaluate_cognitive_request",
        fake_evaluate,
    )
    router = CognitiveRouter(studio(), FakeHerdr(), FakeRuntimes())
    plan = await router.plan(
        {
            "request_id": "COG-SEMANTIC-PROFILE",
            "prompt": "Inspect this failing function and determine what kind of work is needed.",
            "capability": "reflection",
        }
    )

    profile = plan["task_semantic_profile"]
    assert profile["source"] == "jev_advisory"
    assert profile["task_type"] == "software_engineering"
    assert profile["declared_capability"] == "reflection"
    assert profile["coding"] == pytest.approx(0.98 * 0.97 * 0.35)
    assert profile["tool_need"] == pytest.approx(0.94)
    assert "software_engineering" in profile["capabilities"]
    assert "tool_operation" in profile["capabilities"]
    assert profile["reasoning_depth"] == "deep"
    assert profile["latency_priority"] == "quality"
    assert profile["jev_confidence"] == pytest.approx(0.97)
    assert profile["routing_authority"] is False
    assert plan["request"]["capability"] == "reflection"
    assert plan["semantic_authority_changed"] is False



@pytest.mark.asyncio
async def test_reasoning_high_keeps_declared_reasoning_anchor_when_jev_reports_coding(
    monkeypatch,
):
    async def fake_evaluate(_studio, request):
        return JevCognitiveJudgment(
            schema="hirda-jev-cognitive-judgment-v1",
            enabled=True,
            evaluated=True,
            mode="shadow",
            request_id=request.request_id,
            suggested_lane="deep",
            confidence=1.0,
            needs_tool=0.78,
            needs_specialist=0.52,
            model="jev-test",
            error=None,
            task_type="planning",
            coding=0.69,
            latency_priority="quality",
        )

    monkeypatch.setattr(
        "mcp_studio.cognitive_router.evaluate_cognitive_request",
        fake_evaluate,
    )
    router = CognitiveRouter(studio(), ProfiledHerdr(), ProfiledRuntimes())
    plan = await router.plan(
        {
            "request_id": "COG-SEMANTIC-REASONING-ANCHOR",
            "prompt": "Analyze a dependency graph and derive the safest migration order.",
            "capability": "reasoning_high",
        }
    )

    profile = plan["task_semantic_profile"]
    assert profile["task_type"] == "general_reasoning"
    assert profile["source"] == "declared_capability"
    assert profile["coding"] == pytest.approx(0.69 * 0.35)
    assert plan["semantic_routing"]["semantic_preference"] == "claude"


@pytest.mark.asyncio
async def test_tool_heavy_reflection_adds_tool_operation_modifier_and_prefers_codex(
    monkeypatch,
):
    async def fake_evaluate(_studio, request):
        return JevCognitiveJudgment(
            schema="hirda-jev-cognitive-judgment-v1",
            enabled=True,
            evaluated=True,
            mode="shadow",
            request_id=request.request_id,
            suggested_lane="fast",
            confidence=0.38,
            needs_tool=0.71,
            needs_specialist=0.40,
            model="jev-test",
            error=None,
            task_type="reflection",
            coding=0.64,
            latency_priority="balanced",
        )

    monkeypatch.setattr(
        "mcp_studio.cognitive_router.evaluate_cognitive_request",
        fake_evaluate,
    )
    router = CognitiveRouter(studio(), ProfiledHerdr(), ProfiledRuntimes())
    plan = await router.plan(
        {
            "request_id": "COG-SEMANTIC-TOOL-REFLECTION",
            "prompt": "Choose the minimum safe read-only tool sequence to inspect a service.",
            "capability": "reflection",
        }
    )

    profile = plan["task_semantic_profile"]
    assert profile["task_type"] == "reflection"
    assert "tool_operation" in profile["capabilities"]
    assert profile["coding"] == pytest.approx(0.64 * 0.38 * 0.35)
    semantic = plan["semantic_routing"]
    assert semantic["semantic_preference"] == "codex"
    ranking = {row["runtime"]: row for row in semantic["rankings"]}
    assert ranking["codex"]["weights"]["tool_fit"] == pytest.approx(0.20)


@pytest.mark.asyncio
async def test_high_confidence_reflection_promotes_tool_operation_at_moderate_tool_need(
    monkeypatch,
):
    async def fake_evaluate(_studio, request):
        return JevCognitiveJudgment(
            schema="hirda-jev-cognitive-judgment-v1",
            enabled=True,
            evaluated=True,
            mode="shadow",
            request_id=request.request_id,
            suggested_lane="deep",
            confidence=0.93,
            needs_tool=0.56,
            needs_specialist=0.46,
            model="jev-test",
            error=None,
            task_type="planning",
            coding=0.77,
            latency_priority="balanced",
        )

    monkeypatch.setattr(
        "mcp_studio.cognitive_router.evaluate_cognitive_request",
        fake_evaluate,
    )
    router = CognitiveRouter(studio(), ProfiledHerdr(), ProfiledRuntimes())
    plan = await router.plan(
        {
            "request_id": "COG-SEMANTIC-MODERATE-TOOL-NEED",
            "prompt": (
                "Plan read-only terminal and file operations to diagnose "
                "a failing development server."
            ),
            "capability": "reflection",
        }
    )

    profile = plan["task_semantic_profile"]
    assert "tool_operation" in profile["capabilities"]
    assert plan["semantic_routing"]["semantic_preference"] == "codex"


@pytest.mark.asyncio
async def test_low_confidence_reflection_does_not_promote_tool_operation(
    monkeypatch,
):
    async def fake_evaluate(_studio, request):
        return JevCognitiveJudgment(
            schema="hirda-jev-cognitive-judgment-v1",
            enabled=True,
            evaluated=True,
            mode="shadow",
            request_id=request.request_id,
            suggested_lane="fast",
            confidence=0.05,
            needs_tool=0.39,
            needs_specialist=0.30,
            model="jev-test",
            error=None,
            task_type="reflection",
            coding=0.65,
            latency_priority="balanced",
        )

    monkeypatch.setattr(
        "mcp_studio.cognitive_router.evaluate_cognitive_request",
        fake_evaluate,
    )
    router = CognitiveRouter(studio(), ProfiledHerdr(), ProfiledRuntimes())
    plan = await router.plan(
        {
            "request_id": "COG-SEMANTIC-LOW-CONFIDENCE",
            "prompt": "Choose a minimal sequence of filesystem checks before editing code.",
            "capability": "reflection",
        }
    )

    profile = plan["task_semantic_profile"]
    assert "tool_operation" not in profile["capabilities"]
    assert profile["coding"] == pytest.approx(0.65 * 0.05 * 0.35)
    assert plan["semantic_routing"]["semantic_preference"] == "claude"


@pytest.mark.asyncio
async def test_semantic_shadow_ranking_prefers_codex_for_coding_without_reordering_execution():
    router = CognitiveRouter(studio(), ProfiledHerdr(), ProfiledRuntimes())
    plan = await router.plan(
        {
            "request_id": "COG-SHADOW-CODING",
            "prompt": "Fix the failing TypeScript test and verify the patch.",
            "capability": "coding",
        }
    )

    semantic = plan["semantic_routing"]
    assert semantic["mode"] == "shadow"
    assert semantic["applied"] is False
    assert semantic["current_selected"] == "claude"
    assert semantic["semantic_preference"] == "codex"
    assert semantic["would_select"] == "codex"
    assert semantic["changed"] is True
    assert semantic["ranked_order"][0] == "codex"
    assert plan["candidates"][0]["runtime"] == "claude"

    ranking = {row["runtime"]: row for row in semantic["rankings"]}
    assert ranking["codex"]["score"] > ranking["claude"]["score"]
    assert ranking["codex"]["components"]["capability_fit"] > ranking["claude"]["components"]["capability_fit"]


@pytest.mark.asyncio
async def test_hermes_team_delegation_shadow_observes_claude_supervised_coding():
    router = CognitiveRouter(studio(), ProfiledHerdr(), ProfiledRuntimes())
    plan = await router.plan(
        {
            "request_id": "COG-TEAM-SHADOW-CODING",
            "prompt": "Implement the guarded router change and verify the regression suite.",
            "capability": "coding",
        }
    )

    team = plan["team_delegation"]
    assert team["schema"] == "hirda-hermes-team-delegation-v1"
    assert team["mode"] == "shadow"
    assert team["applied"] is False
    assert team["eligible"] is True
    assert team["would_delegate"] is True
    assert team["topology"] == "claude>hermes>bots>hermes>claude"
    assert team["supervisor_runtime"] == "claude"
    assert team["team_lead_runtime"] == "hermes"
    assert team["guard_passed"] is True
    assert team["guard_reasons"] == ["shadow_observation_only"]
    assert team["semantic_authority_changed"] is False
    assert team["execution_authority_changed"] is False
    assert plan["candidates"][0]["runtime"] == "claude"


@pytest.mark.asyncio
async def test_hermes_team_delegation_shadow_rejects_non_coding_task():
    router = CognitiveRouter(studio(), ProfiledHerdr(), ProfiledRuntimes())
    plan = await router.plan(
        {
            "request_id": "COG-TEAM-SHADOW-SOCIAL",
            "prompt": "Reply naturally to the user.",
            "capability": "social_dialogue",
        }
    )

    team = plan["team_delegation"]
    assert team["applied"] is False
    assert team["eligible"] is False
    assert team["would_delegate"] is False
    assert "task_not_software_engineering" in team["guard_reasons"]


@pytest.mark.asyncio
async def test_hermes_team_delegation_never_overrides_explicit_non_claude_runtime():
    router = CognitiveRouter(studio(), ProfiledHerdr(), ProfiledRuntimes())
    plan = await router.plan(
        {
            "request_id": "COG-TEAM-EXPLICIT-CODEX",
            "prompt": "Implement this directly with Codex.",
            "capability": "coding",
            "preferred_depth": "deep",
            "preferred_runtime": "codex",
        }
    )

    team = plan["team_delegation"]
    assert team["applied"] is False
    assert team["eligible"] is False
    assert team["would_delegate"] is False
    assert team["explicit_constraint"] == "preferred_runtime"
    assert team["guard_reasons"] == ["explicit_preferred_runtime"]


@pytest.mark.asyncio
async def test_hermes_team_delegation_accepts_explicit_claude_supervisor():
    router = CognitiveRouter(studio(), ProfiledHerdr(), ProfiledRuntimes())
    plan = await router.plan(
        {
            "request_id": "COG-TEAM-EXPLICIT-CLAUDE",
            "prompt": "Supervise this implementation through the Hermes team.",
            "capability": "coding",
            "preferred_depth": "deep",
            "preferred_runtime": "claude",
        }
    )

    team = plan["team_delegation"]
    assert team["applied"] is False
    assert team["eligible"] is True
    assert team["would_delegate"] is True
    assert team["topology"] == "claude>hermes>bots>hermes>claude"
    assert team["supervisor_runtime"] == "claude"
    assert team["explicit_constraint"] == "preferred_runtime"
    assert team["guard_reasons"] == ["shadow_observation_only"]


@pytest.mark.asyncio
async def test_supervisor_policy_enforce_keeps_codex_specialist_but_appoints_claude():
    cfg = studio()
    cfg.cognitive_semantic_routing_mode = "canary"
    cfg.cognitive_supervisor_policy_mode = "enforce"
    router = CognitiveRouter(cfg, ProfiledHerdr(), ProfiledRuntimes())
    plan = await router.plan(
        {
            "request_id": "COG-SUPERVISOR-CODING",
            "prompt": "Implement and verify a multi-file routing change.",
            "capability": "coding",
            "preferred_depth": "deep",
        }
    )

    semantic = plan["semantic_routing"]
    supervisor = plan["supervisor_policy"]
    assert semantic["effective_selected"] == "codex"
    assert plan["candidates"][0]["runtime"] == "codex"
    assert supervisor["mode"] == "enforce"
    assert supervisor["applied"] is True
    assert supervisor["effective_supervisor"] == "claude"
    assert supervisor["specialist_runtime"] == "codex"
    assert supervisor["semantic_execution_runtime"] == "codex"
    assert supervisor["semantic_authority_changed"] is False
    assert supervisor["execution_order_changed"] is False


@pytest.mark.asyncio
async def test_team_canary_requires_explicit_request_opt_in():
    cfg = studio()
    cfg.cognitive_semantic_routing_mode = "canary"
    cfg.cognitive_supervisor_policy_mode = "enforce"
    cfg.cognitive_hermes_team_delegation_mode = "canary"
    cfg.cognitive_hermes_team_require_opt_in = True
    router = CognitiveRouter(cfg, ProfiledHerdr(), ProfiledRuntimes())

    plan = await router.plan(
        {
            "request_id": "COG-TEAM-CANARY-NO-OPTIN",
            "prompt": "Implement and test this routing change.",
            "capability": "coding",
            "preferred_depth": "deep",
        }
    )
    team = plan["team_delegation"]
    assert team["eligible"] is True
    assert team["applied"] is False
    assert team["guard_reasons"] == ["canary_opt_in_required"]

    opted_in = await router.plan(
        {
            "request_id": "COG-TEAM-CANARY-OPTIN",
            "prompt": "Implement and test this routing change.",
            "capability": "coding",
            "preferred_depth": "deep",
            "context": {"team_delegation": True},
        }
    )
    opted_team = opted_in["team_delegation"]
    assert opted_team["eligible"] is True
    assert opted_team["applied"] is True
    assert opted_team["authority"] == "hirda_canary"
    assert opted_team["topology"] == "claude>hermes>bots>hermes>claude"
    assert opted_team["guard_reasons"] == ["controlled_canary_opt_in"]


@pytest.mark.asyncio
async def test_controlled_team_execute_runs_supervisor_team_and_review():
    cfg = studio()
    cfg.cognitive_semantic_routing_mode = "canary"
    cfg.cognitive_supervisor_policy_mode = "enforce"
    cfg.cognitive_supervisor_runtime = "claude"
    cfg.cognitive_hermes_team_delegation_mode = "canary"
    cfg.cognitive_hermes_team_require_opt_in = True
    cfg.cognitive_hermes_team_timeout_seconds = 600.0
    cfg.cognitive_hermes_team_profiles = {
        "coder": "coder",
        "tester": "tester",
        "reviewer": "reviewer",
    }
    router = CognitiveRouter(cfg, ProfiledHerdr(), ProfiledRuntimes())
    runtime_calls = []

    async def fake_invoke_specific(runtime_id, request, runtime_rows):
        runtime_calls.append((runtime_id, request.request_id, request.prompt))
        if request.request_id.endswith("-SUPERVISOR"):
            return "MISSION_CONTRACT"
        if request.request_id.endswith("-REVIEW"):
            assert "HIRDA EXECUTION ENVELOPE" in request.prompt
            assert '"status": "success"' in request.prompt
            assert '"backend": "hermes_group_chat"' in request.prompt
            assert '"member_output_roles": ["coder", "reviewer", "tester"]' in request.prompt
            assert '"recovery_rounds": 1' in request.prompt
            assert "HERMES GROUP FINAL ROLE REPORTS" in request.prompt
            return "FINAL_REVIEW"
        raise AssertionError(request.request_id)

    class FakeGroupClient:
        async def run_mission(
            self,
            mission,
            *,
            request_id,
            timeout_seconds,
            scope_key=None,
        ):
            assert request_id == "COG-TEAM-EXEC"
            assert "/tmp/project" in str(scope_key)
            assert timeout_seconds == 600.0
            assert "@coder" in mission
            assert "@tester" in mission
            assert "@reviewer" in mission
            assert "MISSION_CONTRACT" in mission
            return SimpleNamespace(
                room_id="hirda-cognitive-team",
                output="TEAM_REPORT",
                events=[{"kind": "message.member"}],
                latest_seq=7,
                driver_status={"working": False},
                member_outputs={
                    "coder": "CODER_FINAL",
                    "tester": "TESTER_FINAL",
                    "reviewer": "REVIEWER_FINAL",
                },
                recovery_rounds=1,
                lifecycle={
                    "scope_digest": "abc123",
                    "family_id": "hirda-cognitive-team-sabc123",
                    "room_id": "hirda-cognitive-team-sabc123",
                    "generation": 0,
                    "rotated": False,
                    "fresh_session_boundary": True,
                },
            )

    router._invoke_specific_runtime = fake_invoke_specific
    router.hermes_groups = FakeGroupClient()

    result = await router.execute(
        {
            "request_id": "COG-TEAM-EXEC",
            "prompt": "Implement the change.",
            "capability": "coding",
            "preferred_depth": "deep",
            "timeout_seconds": 120,
            "context": {
                "team_delegation": True,
                "workspace_path": "/tmp/project",
            },
        }
    )

    assert result.status == "success"
    assert result.runtime == "claude"
    assert result.output == "FINAL_REVIEW"
    assert result.fallback_used is False
    assert [item["stage"] for item in result.attempts] == [
        "supervisor_plan",
        "team_execution",
        "supervisor_review",
    ]
    assert [call[1] for call in runtime_calls] == [
        "COG-TEAM-EXEC-SUPERVISOR",
        "COG-TEAM-EXEC-REVIEW",
    ]
    assert result.supervisor_policy["effective_supervisor"] == "claude"
    assert result.semantic_routing["semantic_preference"] == "codex"
    assert result.semantic_routing["effective_selected"] == "codex"
    assert result.semantic_routing["execution_applied"] is False
    assert result.semantic_routing["execution_authority"] == "controlled_team"
    assert result.semantic_routing["execution_runtime"] == "claude"
    assert result.semantic_routing["specialist_runtime"] == "codex"
    assert result.semantic_authority_changed is False
    assert result.team_delegation["applied"] is True
    assert result.team_delegation["execution"]["status"] == "success"
    assert result.team_delegation["execution"]["native_bot_mode"] is True
    assert result.team_delegation["execution"]["member_output_roles"] == [
        "coder",
        "reviewer",
        "tester",
    ]
    assert result.team_delegation["execution"]["recovery_rounds"] == 1
    assert result.team_delegation["execution"]["lifecycle"]["scope_digest"] == "abc123"
    assert result.team_delegation["execution"]["lifecycle"]["generation"] == 0
    assert result.attempts[1]["lifecycle"]["fresh_session_boundary"] is True


def test_hermes_group_scope_key_is_workspace_stable_and_tenant_aware():
    cfg = studio()
    cfg.cognitive_hermes_group_scope_mode = "workspace"
    router = CognitiveRouter(cfg, ProfiledHerdr(), ProfiledRuntimes())

    request_a = CognitiveRequest(
        schema="hirda-cognitive-request-v1",
        request_id="REQ-A",
        prompt="x",
        context={
            "workspace_path": "/tmp/project",
            "account_id": "acct-a",
        },
        conversation_id="conv-a",
    )
    request_b = CognitiveRequest(
        schema="hirda-cognitive-request-v1",
        request_id="REQ-B",
        prompt="x",
        context={
            "workspace_path": "/tmp/project",
            "account_id": "acct-a",
        },
        conversation_id="conv-b",
    )
    request_other_account = CognitiveRequest(
        schema="hirda-cognitive-request-v1",
        request_id="REQ-C",
        prompt="x",
        context={
            "workspace_path": "/tmp/project",
            "account_id": "acct-b",
        },
        conversation_id="conv-a",
    )

    assert router._hermes_group_scope_key(request_a) == router._hermes_group_scope_key(
        request_b
    )
    assert router._hermes_group_scope_key(
        request_a
    ) != router._hermes_group_scope_key(request_other_account)


def test_hermes_group_scope_key_can_follow_conversation():
    cfg = studio()
    cfg.cognitive_hermes_group_scope_mode = "conversation"
    router = CognitiveRouter(cfg, ProfiledHerdr(), ProfiledRuntimes())

    first = CognitiveRequest(
        schema="hirda-cognitive-request-v1",
        request_id="REQ-A",
        prompt="x",
        context={"workspace_path": "/tmp/project"},
        conversation_id="conv-a",
    )
    second = CognitiveRequest(
        schema="hirda-cognitive-request-v1",
        request_id="REQ-B",
        prompt="x",
        context={"workspace_path": "/tmp/project"},
        conversation_id="conv-b",
    )

    assert router._hermes_group_scope_key(first) != router._hermes_group_scope_key(
        second
    )


def test_hermes_group_scope_key_isolated_by_cognitive_principal():
    cfg = studio()
    cfg.cognitive_hermes_group_scope_mode = "workspace"
    router = CognitiveRouter(cfg, ProfiledHerdr(), ProfiledRuntimes())

    fern = CognitiveRequest(
        schema="hirda-cognitive-request-v1",
        request_id="REQ-FERN",
        prompt="x",
        context={"workspace_path": "/tmp/project", "account_id": "acct-a"},
        principal_id="fern",
    )
    nova = CognitiveRequest(
        schema="hirda-cognitive-request-v1",
        request_id="REQ-NOVA",
        prompt="x",
        context={"workspace_path": "/tmp/project", "account_id": "acct-a"},
        principal_id="nova",
    )

    assert router._hermes_group_scope_key(fern) != router._hermes_group_scope_key(
        nova
    )


def test_hermes_shared_group_scope_still_isolates_named_principals():
    cfg = studio()
    cfg.cognitive_hermes_group_scope_mode = "shared"
    router = CognitiveRouter(cfg, ProfiledHerdr(), ProfiledRuntimes())

    anonymous = CognitiveRequest(
        schema="hirda-cognitive-request-v1",
        request_id="REQ-SHARED",
        prompt="x",
    )
    fern = CognitiveRequest(
        schema="hirda-cognitive-request-v1",
        request_id="REQ-SHARED-FERN",
        prompt="x",
        principal_id="fern",
    )
    nova = CognitiveRequest(
        schema="hirda-cognitive-request-v1",
        request_id="REQ-SHARED-NOVA",
        prompt="x",
        principal_id="nova",
    )

    assert router._hermes_group_scope_key(anonymous) == "shared"
    assert router._hermes_group_scope_key(fern) != "shared"
    assert router._hermes_group_scope_key(fern) != router._hermes_group_scope_key(
        nova
    )


def test_normalize_cognitive_request_uses_agent_id_as_principal_fallback():
    request = normalize_cognitive_request(
        {
            "request_id": "REQ-PRINCIPAL-FALLBACK",
            "prompt": "x",
            "agent_id": "fern",
        }
    )
    explicit = normalize_cognitive_request(
        {
            "request_id": "REQ-PRINCIPAL-EXPLICIT",
            "prompt": "x",
            "agent_id": "runtime-agent",
            "principal_id": "nova",
        }
    )

    assert request.agent_id == "fern"
    assert request.principal_id == "fern"
    assert explicit.agent_id == "runtime-agent"
    assert explicit.principal_id == "nova"


@pytest.mark.asyncio
async def test_semantic_shadow_ranking_prefers_claude_for_social_dialogue():
    router = CognitiveRouter(studio(), ProfiledHerdr(), ProfiledRuntimes())
    plan = await router.plan(
        {
            "request_id": "COG-SHADOW-SOCIAL",
            "prompt": "Mali asks Nova how she feels today.",
            "capability": "social_dialogue",
        }
    )

    semantic = plan["semantic_routing"]
    assert semantic["semantic_preference"] == "claude"
    assert semantic["ranked_order"][0] == "claude"
    assert semantic["current_selected"] == "hermes"
    assert semantic["changed"] is True


@pytest.mark.asyncio
async def test_semantic_shadow_ranking_prefers_claude_for_research():
    router = CognitiveRouter(studio(), ProfiledHerdr(), ProfiledRuntimes())
    plan = await router.plan(
        {
            "request_id": "COG-SHADOW-RESEARCH",
            "prompt": "Compare the evidence and explain the strongest interpretation.",
            "capability": "research",
        }
    )

    semantic = plan["semantic_routing"]
    assert semantic["semantic_preference"] == "claude"
    assert semantic["ranked_order"][0] == "claude"


@pytest.mark.asyncio
async def test_semantic_shadow_ranking_prefers_hermes_for_fast_utility():
    router = CognitiveRouter(studio(), ProfiledHerdr(), ProfiledRuntimes())
    plan = await router.plan(
        {
            "request_id": "COG-SHADOW-FAST",
            "prompt": "Summarize these three short lines.",
            "capability": "fast_utility",
        }
    )

    semantic = plan["semantic_routing"]
    assert semantic["semantic_preference"] == "hermes"
    assert semantic["ranked_order"][0] == "hermes"


@pytest.mark.asyncio
async def test_semantic_shadow_ranking_skips_exhausted_codex_and_falls_back_for_coding():
    router = CognitiveRouter(
        studio(),
        ProfiledHerdr(),
        ProfiledRuntimes(codex_limit_status="exhausted"),
    )
    plan = await router.plan(
        {
            "request_id": "COG-SHADOW-CODING-QUOTA",
            "prompt": "Repair the failing build.",
            "capability": "coding",
        }
    )

    semantic = plan["semantic_routing"]
    assert "codex" not in semantic["ranked_order"]
    assert semantic["semantic_preference"] == "claude"
    assert semantic["would_select"] == "claude"


@pytest.mark.asyncio
async def test_semantic_shadow_ranking_respects_explicit_runtime_constraint():
    cfg = studio()
    router = CognitiveRouter(cfg, ProfiledHerdr(), ProfiledRuntimes())
    plan = await router.plan(
        {
            "request_id": "COG-SHADOW-PREFERRED",
            "prompt": "Fix the failing TypeScript test.",
            "capability": "coding",
            "preferred_runtime": "claude",
        }
    )

    semantic = plan["semantic_routing"]
    assert semantic["semantic_preference"] == "codex"
    assert semantic["explicit_constraint"] == "preferred_runtime"
    assert semantic["would_select"] == "claude"
    assert semantic["changed"] is False

@pytest.mark.asyncio
async def test_semantic_canary_applies_high_margin_primary_without_enabling_fallback():
    cfg = studio()
    cfg.cognitive_semantic_routing_mode = "canary"
    cfg.cognitive_semantic_canary_min_margin = 0.03
    router = CognitiveRouter(cfg, ProfiledHerdr(), ProfiledRuntimes())

    result = await router.execute(
        {
            "request_id": "COG-CANARY-CODING",
            "prompt": "Fix the failing TypeScript test and verify the patch.",
            "capability": "coding",
            "allow_fallback": False,
        }
    )

    semantic = result.semantic_routing
    assert result.status == "success"
    assert result.runtime == "codex"
    assert result.fallback_used is False
    assert result.semantic_authority_changed is True
    assert semantic["mode"] == "canary"
    assert semantic["applied"] is True
    assert semantic["current_selected"] == "claude"
    assert semantic["semantic_preference"] == "codex"
    assert semantic["effective_selected"] == "codex"
    assert semantic["top_margin"] >= semantic["min_margin"]
    assert semantic["guard_passed"] is True
    assert semantic["guard_reasons"] == []


@pytest.mark.asyncio
async def test_semantic_canary_keeps_current_route_when_margin_is_below_guard():
    cfg = studio()
    cfg.cognitive_semantic_routing_mode = "canary"
    cfg.cognitive_semantic_canary_min_margin = 0.50
    router = CognitiveRouter(cfg, ProfiledHerdr(), ProfiledRuntimes())

    plan = await router.plan(
        {
            "request_id": "COG-CANARY-GUARD-MARGIN",
            "prompt": "Fix the failing TypeScript test and verify the patch.",
            "capability": "coding",
            "allow_fallback": True,
        }
    )

    semantic = plan["semantic_routing"]
    assert semantic["semantic_preference"] == "codex"
    assert semantic["current_selected"] == "claude"
    assert semantic["effective_selected"] == "claude"
    assert semantic["applied"] is False
    assert semantic["guard_passed"] is False
    assert "semantic_margin_below_threshold" in semantic["guard_reasons"]
    assert plan["candidates"][0]["runtime"] == "claude"
    assert plan["semantic_authority_changed"] is False


@pytest.mark.asyncio
async def test_semantic_canary_never_overrides_explicit_runtime_constraint():
    cfg = studio()
    cfg.cognitive_semantic_routing_mode = "canary"
    cfg.cognitive_semantic_canary_min_margin = 0.0
    router = CognitiveRouter(cfg, ProfiledHerdr(), ProfiledRuntimes())

    plan = await router.plan(
        {
            "request_id": "COG-CANARY-EXPLICIT",
            "prompt": "Fix the failing TypeScript test.",
            "capability": "coding",
            "preferred_runtime": "claude",
            "allow_fallback": True,
        }
    )

    semantic = plan["semantic_routing"]
    assert semantic["semantic_preference"] == "codex"
    assert semantic["explicit_constraint"] == "preferred_runtime"
    assert semantic["applied"] is False
    assert semantic["effective_selected"] == "claude"
    assert "explicit_preferred_runtime" in semantic["guard_reasons"]
    assert plan["candidates"][0]["runtime"] == "claude"
    assert plan["semantic_authority_changed"] is False


@pytest.mark.asyncio
async def test_pane_routing_shadow_reports_unowned_without_changing_selection():
    cfg = studio()
    cfg.cognitive_pane_routing_mode = "shadow"
    cfg.cognitive_runtime_bindings = {
        "hermes": {
            "machine_id": "openclaw",
            "backend_id": "hermes-runtime",
            "pane_name_prefixes": ["hirda-cognitive-"],
        }
    }
    router = CognitiveRouter(cfg, FakeHerdr(), FakeRuntimes())

    plan = await router.plan(
        {
            "request_id": "COG-PANE-SHADOW",
            "prompt": "Return exactly OK.",
            "capability": "fast_utility",
        }
    )

    assert plan["candidates"][0]["runtime"] == "hermes"
    assert plan["candidates"][0]["pane_owned"] is False
    assert plan["pane_routing"]["mode"] == "shadow"
    assert plan["pane_routing"]["runtimes"]["hermes"]["owned_pane_count"] == 0


@pytest.mark.asyncio
async def test_pane_routing_enforce_uses_only_owned_runtime_pane():
    herdr = FakeHerdr()
    herdr.snapshot["panes"]["panes"].extend(
        [
            {
                "pane_id": "wH:pShared",
                "agent": "hermes",
                "agent_status": "idle",
                "name": "user-task",
            },
            {
                "pane_id": "wH:pCog",
                "agent": "hermes",
                "agent_status": "done",
                "name": "hirda-cognitive-hermes",
            },
        ]
    )
    cfg = studio()
    cfg.cognitive_pane_routing_mode = "enforce"
    cfg.cognitive_runtime_bindings = {
        "hermes": {
            "machine_id": "openclaw",
            "backend_id": "hermes-runtime",
            "pane_name_prefixes": ["hirda-cognitive-"],
        }
    }
    router = CognitiveRouter(cfg, herdr, FakeRuntimes())

    plan = await router.plan(
        {
            "request_id": "COG-PANE-ENFORCE",
            "prompt": "Return exactly OK.",
            "capability": "fast_utility",
            "preferred_runtime": "hermes",
            "allow_fallback": False,
        }
    )

    assert len(plan["candidates"]) == 1
    assert plan["candidates"][0]["pane_id"] == "wH:pCog"
    assert plan["candidates"][0]["pane_owned"] is True
    assert plan["pane_routing"]["enforced"] is True
    assert (
        plan["pane_routing"]["runtimes"]["hermes"]["available_owned_pane_count"]
        == 1
    )


@pytest.mark.asyncio
async def test_pane_routing_enforce_rejects_explicit_unowned_target():
    herdr = FakeHerdr()
    herdr.snapshot["panes"]["panes"].append(
        {
            "pane_id": "wH:pShared",
            "agent": "hermes",
            "agent_status": "idle",
            "name": "user-task",
        }
    )
    cfg = studio()
    cfg.cognitive_pane_routing_mode = "enforce"
    cfg.cognitive_runtime_bindings = {
        "hermes": {
            "machine_id": "openclaw",
            "backend_id": "hermes-runtime",
            "pane_name_prefixes": ["hirda-cognitive-"],
        }
    }
    router = CognitiveRouter(cfg, herdr, FakeRuntimes())

    plan = await router.plan(
        {
            "request_id": "COG-PANE-UNOWNED",
            "prompt": "Return exactly OK.",
            "capability": "fast_utility",
            "preferred_runtime": "hermes",
            "target_pane_id": "wH:pShared",
            "allow_fallback": False,
        }
    )

    assert plan["candidates"] == []


@pytest.mark.asyncio
async def test_anonymous_routing_does_not_select_principal_owned_pane_in_enforce_mode():
    herdr = FakeHerdr()
    herdr.snapshot["panes"]["panes"].append(
        {
            "pane_id": "wH:p0",
            "agent": "hermes",
            "agent_status": "idle",
            "agent_name": "hirda-nova-hermes",
        }
    )
    cfg = studio()
    cfg.cognitive_pane_routing_mode = "enforce"
    cfg.cognitive_runtime_bindings = {
        "hermes": {
            "machine_id": "openclaw",
            "backend_id": "hermes-runtime",
            "pane_ids": ["wH:p1"],
            "principal_pane_name_prefixes": {
                "fern": ["hirda-fern-hermes"],
                "nova": ["hirda-nova-hermes"],
            },
        }
    }
    router = CognitiveRouter(cfg, herdr, FakeRuntimes())

    plan = await router.plan(
        {
            "request_id": "COG-ANON-GENERIC",
            "prompt": "Return exactly GENERIC.",
            "capability": "fast_utility",
            "preferred_runtime": "hermes",
            "allow_fallback": False,
        }
    )

    assert len(plan["candidates"]) == 1
    assert plan["candidates"][0]["pane_id"] == "wH:p1"


@pytest.mark.asyncio
async def test_anonymous_routing_rejects_explicit_principal_owned_target():
    herdr = FakeHerdr()
    herdr.snapshot["panes"]["panes"].append(
        {
            "pane_id": "wH:pNova",
            "agent": "hermes",
            "agent_status": "idle",
            "agent_name": "hirda-nova-hermes",
        }
    )
    cfg = studio()
    cfg.cognitive_pane_routing_mode = "enforce"
    cfg.cognitive_runtime_bindings = {
        "hermes": {
            "machine_id": "openclaw",
            "backend_id": "hermes-runtime",
            "principal_pane_name_prefixes": {
                "nova": ["hirda-nova-hermes"],
            },
        }
    }
    router = CognitiveRouter(cfg, herdr, FakeRuntimes())

    plan = await router.plan(
        {
            "request_id": "COG-ANON-EXPLICIT-NOVA",
            "prompt": "Return exactly NO.",
            "capability": "fast_utility",
            "preferred_runtime": "hermes",
            "target_pane_id": "wH:pNova",
            "allow_fallback": False,
        }
    )

    assert plan["candidates"] == []


@pytest.mark.asyncio
async def test_anonymous_routing_omits_principal_owned_fallback_target():
    herdr = FakeHerdr()
    herdr.snapshot["panes"]["panes"].append(
        {
            "pane_id": "wC:pNova",
            "agent": "claude",
            "agent_status": "idle",
            "agent_name": "hirda-nova-claude",
        }
    )
    cfg = studio()
    cfg.cognitive_pane_routing_mode = "enforce"
    cfg.cognitive_runtime_bindings = {
        "hermes": {
            "machine_id": "openclaw",
            "backend_id": "hermes-runtime",
            "pane_ids": ["wH:p1"],
        },
        "claude": {
            "machine_id": "openclaw",
            "backend_id": "claude-runtime",
            "principal_pane_name_prefixes": {
                "nova": ["hirda-nova-claude"],
            },
        },
    }
    router = CognitiveRouter(cfg, herdr, FakeRuntimes())

    plan = await router.plan(
        {
            "request_id": "COG-ANON-FALLBACK-NOVA",
            "prompt": "Return exactly GENERIC.",
            "capability": "fast_utility",
            "target_pane_id": "wH:p1",
            "fallback_target_pane_id": "wC:pNova",
            "allow_fallback": True,
        }
    )

    assert len(plan["candidates"]) == 1
    assert plan["candidates"][0]["pane_id"] == "wH:p1"


@pytest.mark.asyncio
async def test_principal_routing_selects_distinct_hermes_panes_in_same_workspace():
    herdr = FakeHerdr()
    herdr.snapshot["panes"]["panes"].extend(
        [
            {
                "pane_id": "wH:pFern",
                "agent": "hermes",
                "agent_status": "idle",
                "agent_name": "hirda-fern-hermes",
            },
            {
                "pane_id": "wH:pNova",
                "agent": "hermes",
                "agent_status": "idle",
                "agent_name": "hirda-nova-hermes",
            },
        ]
    )
    cfg = studio()
    cfg.cognitive_pane_routing_mode = "enforce"
    cfg.cognitive_runtime_bindings = {
        "hermes": {
            "machine_id": "openclaw",
            "backend_id": "hermes-runtime",
            "principal_pane_name_prefixes": {
                "fern": ["hirda-fern-hermes"],
                "nova": ["hirda-nova-hermes"],
            },
        }
    }
    router = CognitiveRouter(cfg, herdr, FakeRuntimes())

    fern = await router.plan(
        {
            "request_id": "COG-FERN-PANE",
            "prompt": "Return exactly FERN.",
            "capability": "fast_utility",
            "preferred_runtime": "hermes",
            "allow_fallback": False,
            "principal_id": "fern",
        }
    )
    nova = await router.plan(
        {
            "request_id": "COG-NOVA-PANE",
            "prompt": "Return exactly NOVA.",
            "capability": "fast_utility",
            "preferred_runtime": "hermes",
            "allow_fallback": False,
            "principal_id": "nova",
        }
    )

    assert fern["candidates"][0]["pane_id"] == "wH:pFern"
    assert fern["candidates"][0]["principal_id"] == "fern"
    assert fern["candidates"][0]["pane_principal_owned"] is True
    assert nova["candidates"][0]["pane_id"] == "wH:pNova"
    assert nova["candidates"][0]["principal_id"] == "nova"
    assert nova["candidates"][0]["pane_principal_owned"] is True


@pytest.mark.asyncio
async def test_principal_routing_rejects_explicit_cross_principal_target():
    herdr = FakeHerdr()
    herdr.snapshot["panes"]["panes"].append(
        {
            "pane_id": "wH:pFern",
            "agent": "hermes",
            "agent_status": "idle",
            "agent_name": "hirda-fern-hermes",
        }
    )
    cfg = studio()
    cfg.cognitive_pane_routing_mode = "enforce"
    cfg.cognitive_runtime_bindings = {
        "hermes": {
            "machine_id": "openclaw",
            "backend_id": "hermes-runtime",
            "principal_pane_name_prefixes": {
                "fern": ["hirda-fern-hermes"],
                "nova": ["hirda-nova-hermes"],
            },
        }
    }
    router = CognitiveRouter(cfg, herdr, FakeRuntimes())

    plan = await router.plan(
        {
            "request_id": "COG-NOVA-CROSS-PANE",
            "prompt": "Return exactly NOVA.",
            "capability": "fast_utility",
            "preferred_runtime": "hermes",
            "target_pane_id": "wH:pFern",
            "allow_fallback": False,
            "principal_id": "nova",
        }
    )

    assert plan["candidates"] == []


@pytest.mark.asyncio
async def test_principal_routing_rejects_cross_principal_fallback_target():
    herdr = FakeHerdr()
    herdr.snapshot["panes"]["panes"].extend(
        [
            {
                "pane_id": "wH:pNova",
                "agent": "hermes",
                "agent_status": "idle",
                "agent_name": "hirda-nova-hermes",
            },
            {
                "pane_id": "wC:pFern",
                "agent": "claude",
                "agent_status": "idle",
                "agent_name": "hirda-fern-claude",
            },
        ]
    )
    cfg = studio()
    cfg.cognitive_pane_routing_mode = "enforce"
    cfg.cognitive_runtime_bindings = {
        "hermes": {
            "machine_id": "openclaw",
            "backend_id": "hermes-runtime",
            "principal_pane_name_prefixes": {
                "nova": ["hirda-nova-hermes"],
            },
        },
        "claude": {
            "machine_id": "openclaw",
            "backend_id": "claude-runtime",
            "principal_pane_name_prefixes": {
                "fern": ["hirda-fern-claude"],
                "nova": ["hirda-nova-claude"],
            },
        },
    }
    router = CognitiveRouter(cfg, herdr, FakeRuntimes())

    plan = await router.plan(
        {
            "request_id": "COG-NOVA-CROSS-FALLBACK",
            "prompt": "Return exactly NOVA.",
            "capability": "fast_utility",
            "target_pane_id": "wH:pNova",
            "fallback_target_pane_id": "wC:pFern",
            "allow_fallback": True,
            "principal_id": "nova",
        }
    )

    assert len(plan["candidates"]) == 1
    assert plan["candidates"][0]["pane_id"] == "wH:pNova"


@pytest.mark.asyncio
async def test_reserved_certification_pane_is_excluded_from_default_routing():
    herdr = FakeHerdr()
    herdr.snapshot["panes"]["panes"] = [
        pane for pane in herdr.snapshot["panes"]["panes"] if pane["agent"] != "hermes"
    ]
    herdr.snapshot["panes"]["panes"].append(
        {
            "pane_id": "wH:p9",
            "agent": "hermes",
            "agent_status": "done",
            "name": "hirda-live-certification",
        }
    )
    router = CognitiveRouter(studio(), herdr, FakeRuntimes())

    plan = await router.plan(
        {
            "request_id": "COG-RESERVED",
            "prompt": "Return exactly OK.",
            "capability": "fast_utility",
            "preferred_depth": "fast",
        }
    )

    assert plan["candidates"]
    assert plan["candidates"][0]["runtime"] == "claude"
    assert plan["candidates"][0]["pane_id"] == "wC:p1"


@pytest.mark.asyncio
async def test_label_only_certification_pane_is_excluded_from_default_routing():
    herdr = FakeHerdr()
    herdr.snapshot["panes"]["panes"] = [
        pane for pane in herdr.snapshot["panes"]["panes"] if pane["agent"] != "hermes"
    ]
    herdr.snapshot["panes"]["panes"].append(
        {
            "pane_id": "wH:p9",
            "agent": "hermes",
            "agent_status": "done",
            "label": "HIRDA-certification-test",
        }
    )
    router = CognitiveRouter(studio(), herdr, FakeRuntimes())

    plan = await router.plan(
        {
            "request_id": "COG-RESERVED-LABEL",
            "prompt": "Return exactly OK.",
            "capability": "fast_utility",
            "preferred_depth": "fast",
        }
    )

    assert plan["candidates"]
    assert plan["candidates"][0]["runtime"] == "claude"
    assert plan["candidates"][0]["pane_id"] == "wC:p1"


@pytest.mark.asyncio
async def test_target_pane_id_pins_reserved_certification_pane():
    herdr = FakeHerdr()
    herdr.snapshot["panes"]["panes"].append(
        {
            "pane_id": "wH:p9",
            "agent": "hermes",
            "agent_status": "done",
            "name": "hirda-certification-fast",
        }
    )
    router = CognitiveRouter(studio(), herdr, FakeRuntimes())

    result = await router.execute(
        {
            "request_id": "COG-PINNED-PANE",
            "prompt": "Return the dedicated pane result.",
            "capability": "fast_utility",
            "preferred_depth": "fast",
            "preferred_runtime": "hermes",
            "target_pane_id": "wH:p9",
            "allow_fallback": False,
        }
    )

    assert result.status == "success"
    assert result.runtime == "hermes"
    assert result.output == "RESULT:wH:p9"
    prompt_calls = [call for call in herdr.calls if call[0] == "herdr_prompt_agent"]
    assert prompt_calls
    assert prompt_calls[0][1]["pane_id"] == "wH:p9"


@pytest.mark.asyncio
async def test_exact_target_can_fail_over_to_exact_fallback_pane():
    herdr = FakeHerdr()
    herdr.snapshot["panes"]["panes"].extend(
        [
            {
                "pane_id": "wH:p9",
                "agent": "hermes",
                "agent_status": "working",
                "name": "hirda-live-certification",
            },
            {
                "pane_id": "wC:p9",
                "agent": "claude",
                "agent_status": "done",
                "name": "hirda-certification-claude",
            },
        ]
    )
    router = CognitiveRouter(studio(), herdr, FakeRuntimes())

    result = await router.execute(
        {
            "request_id": "COG-PINNED-FAILOVER",
            "prompt": "Return the dedicated fallback pane result.",
            "capability": "fast_utility",
            "preferred_depth": "fast",
            "preferred_runtime": "hermes",
            "target_pane_id": "wH:p9",
            "fallback_target_pane_id": "wC:p9",
            "allow_fallback": True,
        }
    )

    assert result.status == "success"
    assert result.runtime == "claude"
    assert result.fallback_used is True
    assert result.degraded is True
    assert result.output == "RESULT:wC:p9"
    assert result.attempts[0]["runtime"] == "hermes"
    assert result.attempts[0]["error"] == "runtime_busy"
    assert result.attempts[-1] == {
        "attempt_id": "COG-PINNED-FAILOVER:A2",
        "runtime": "claude",
        "pane_id": "wC:p9",
        "status": "success",
        "accepted": True,
    }
    prompt_calls = [call for call in herdr.calls if call[0] == "herdr_prompt_agent"]
    assert prompt_calls
    assert prompt_calls[0][1]["pane_id"] == "wC:p9"


def test_normalize_requires_primary_target_for_fallback_target():
    with pytest.raises(CognitiveRouterError, match="cognitive_fallback_pane_requires_target"):
        normalize_cognitive_request(
            {
                "prompt": "test",
                "fallback_target_pane_id": "wC:p9",
                "allow_fallback": True,
            }
        )


def test_normalize_rejects_unknown_capability():
    with pytest.raises(CognitiveRouterError, match="cognitive_capability_invalid"):
        normalize_cognitive_request({"prompt": "x", "capability": "unknown"})


@pytest.mark.asyncio
async def test_auto_rollover_pane_is_excluded_from_default_cognitive_routing():
    herdr = FakeHerdr()
    herdr.snapshot["panes"]["panes"] = [
        pane
        for pane in herdr.snapshot["panes"]["panes"]
        if pane["agent"] != "hermes"
    ]
    herdr.snapshot["panes"]["panes"].append(
        {
            "pane_id": "wH:pR",
            "agent": "hermes",
            "agent_status": "idle",
            "name": "hirda-auto-rollover-hermes",
        }
    )
    router = CognitiveRouter(studio(), herdr, FakeRuntimes())

    plan = await router.plan(
        {
            "request_id": "COG-ROLLOVER-RESERVED",
            "prompt": "Return exactly OK.",
            "capability": "fast_utility",
            "preferred_depth": "fast",
        }
    )

    assert plan["candidates"]
    assert plan["candidates"][0]["runtime"] == "claude"
    assert plan["candidates"][0]["pane_id"] == "wC:p1"


class FakeCapabilityRouter:
    def __init__(self, routes: dict[str, dict | None]):
        self.routes = routes
        self.calls: list[dict] = []

    def resolve_capability_route(
        self,
        capability,
        *,
        permission,
        machine_id=None,
        backend_id=None,
        model_id=None,
    ):
        self.calls.append(
            {
                "capability": capability,
                "permission": permission,
                "machine_id": machine_id,
                "backend_id": backend_id,
                "model_id": model_id,
            }
        )
        selected = self.routes.get(machine_id)
        return {
            "schema": "hirda-capability-route-v1",
            "capability": capability,
            "permission": permission,
            "selected": selected,
            "eligible": [selected] if isinstance(selected, dict) else [],
            "rejected": (
                []
                if isinstance(selected, dict)
                else [
                    {
                        "node_id": machine_id,
                        "reason": "node_unavailable",
                    }
                ]
            ),
            "eligible_count": 1 if isinstance(selected, dict) else 0,
            "rejected_count": 0 if isinstance(selected, dict) else 1,
            "no_route_reason": (
                None
                if isinstance(selected, dict)
                else "no_eligible_route"
            ),
        }


def routing_studio(mode: str) -> SimpleNamespace:
    cfg = studio()
    cfg.cognitive_node_routing_mode = mode
    cfg.cognitive_capability_routes = {
        "fast_utility": "chat",
        "reasoning_high": "reasoning",
    }
    cfg.cognitive_runtime_bindings = {
        "hermes": {
            "machine_id": "node-h",
            "backend_id": "runtime-h",
            "model_id": "model-h",
        },
        "claude": {
            "machine_id": "node-c",
            "backend_id": "runtime-c",
            "model_id": "model-c",
        },
    }
    return cfg


def selected_route(
    node_id: str,
    backend_id: str,
    model_id: str,
) -> dict:
    return {
        "node_id": node_id,
        "machine_id": node_id,
        "scope": "model",
        "capability": "chat",
        "backend_id": backend_id,
        "model_id": model_id,
        "permission": "execute",
        "health": {
            "state": "ready",
            "age_seconds": 1.0,
            "load_ratio_1m": 0.2,
            "memory_available_ratio": 0.6,
            "backend_state": "ready",
            "backend_latency_ms": 12.0,
        },
    }


@pytest.mark.asyncio
async def test_n8_off_mode_preserves_legacy_cognitive_routing():
    capability_router = FakeCapabilityRouter({})
    router = CognitiveRouter(
        routing_studio("off"),
        FakeHerdr(),
        FakeRuntimes(),
        capability_router=capability_router,
    )

    plan = await router.plan(
        {
            "request_id": "COG-N8-OFF",
            "prompt": "Return a short result.",
            "capability": "fast_utility",
        }
    )

    assert plan["candidates"][0]["runtime"] == "hermes"
    assert plan["infrastructure_routing"]["mode"] == "off"
    assert plan["infrastructure_routing"]["enabled"] is False
    assert capability_router.calls == []


@pytest.mark.asyncio
async def test_n8_shadow_observes_n7_failure_without_filtering_candidate():
    capability_router = FakeCapabilityRouter(
        {
            "node-h": None,
            "node-c": selected_route(
                "node-c",
                "runtime-c",
                "model-c",
            ),
        }
    )
    router = CognitiveRouter(
        routing_studio("shadow"),
        FakeHerdr(),
        FakeRuntimes(),
        capability_router=capability_router,
    )

    plan = await router.plan(
        {
            "request_id": "COG-N8-SHADOW",
            "prompt": "Return a short result.",
            "capability": "fast_utility",
        }
    )

    assert [row["runtime"] for row in plan["candidates"]][:2] == [
        "hermes",
        "claude",
    ]
    report = plan["infrastructure_routing"]
    assert report["mode"] == "shadow"
    assert report["enforced"] is False
    assert report["decisions"][0]["runtime"] == "hermes"
    assert report["decisions"][0]["eligible"] is False
    assert report["decisions"][0]["reason"] == "no_eligible_route"


@pytest.mark.asyncio
async def test_n8_enforce_filters_unavailable_primary_and_executes_fallback():
    capability_router = FakeCapabilityRouter(
        {
            "node-h": None,
            "node-c": selected_route(
                "node-c",
                "runtime-c",
                "model-c",
            ),
        }
    )
    router = CognitiveRouter(
        routing_studio("enforce"),
        FakeHerdr(),
        FakeRuntimes(),
        capability_router=capability_router,
    )

    result = await router.execute(
        {
            "request_id": "COG-N8-ENFORCE",
            "prompt": "Return the current result.",
            "capability": "fast_utility",
        }
    )

    assert result.status == "success"
    assert result.runtime == "claude"
    assert result.output == "CLAUDE_RESULT"
    assert result.identity_continuity is True
    assert result.semantic_authority_changed is False
    report = result.infrastructure_routing
    assert report["mode"] == "enforce"
    assert report["candidate_count_before"] == 2
    assert report["candidate_count_after"] == 1
    selected = [
        item["selected"]
        for item in report["decisions"]
        if item["runtime"] == "claude"
    ][0]
    assert selected["node_id"] == "node-c"


@pytest.mark.asyncio
async def test_n8_enforce_fails_closed_when_capability_mapping_missing():
    cfg = routing_studio("enforce")
    cfg.cognitive_capability_routes = {}
    capability_router = FakeCapabilityRouter({})
    router = CognitiveRouter(
        cfg,
        FakeHerdr(),
        FakeRuntimes(),
        capability_router=capability_router,
    )

    result = await router.execute(
        {
            "request_id": "COG-N8-UNMAPPED",
            "prompt": "Return a result.",
            "capability": "fast_utility",
        }
    )

    assert result.status == "failed"
    assert result.error == "no_healthy_cognitive_runtime"
    assert result.infrastructure_routing["candidate_count_after"] == 0
    assert {
        decision["reason"]
        for decision in result.infrastructure_routing["decisions"]
    } == {"cognitive_capability_route_unmapped"}
    assert capability_router.calls == []


@pytest.mark.asyncio
async def test_n8_enforce_fails_closed_when_runtime_binding_missing():
    cfg = routing_studio("enforce")
    cfg.cognitive_runtime_bindings.pop("hermes")
    cfg.cognitive_runtime_bindings.pop("claude")
    router = CognitiveRouter(
        cfg,
        FakeHerdr(),
        FakeRuntimes(),
        capability_router=FakeCapabilityRouter({}),
    )

    plan = await router.plan(
        {
            "request_id": "COG-N8-NOBIND",
            "prompt": "Return a result.",
            "capability": "fast_utility",
        }
    )

    assert plan["candidates"] == []
    assert {
        decision["reason"]
        for decision in plan["infrastructure_routing"]["decisions"]
    } == {"cognitive_runtime_binding_missing"}


@pytest.mark.asyncio
async def test_n8_calls_n7_with_explicit_runtime_lineage():
    route = selected_route(
        "node-h",
        "runtime-h",
        "model-h",
    )
    capability_router = FakeCapabilityRouter({"node-h": route})
    cfg = routing_studio("enforce")
    cfg.cognitive_runtime_bindings = {
        "hermes": cfg.cognitive_runtime_bindings["hermes"],
    }
    router = CognitiveRouter(
        cfg,
        FakeHerdr(),
        FakeRuntimes(),
        capability_router=capability_router,
    )

    plan = await router.plan(
        {
            "request_id": "COG-N8-BIND",
            "prompt": "Return a result.",
            "capability": "fast_utility",
            "preferred_runtime": "hermes",
            "allow_fallback": False,
        }
    )

    assert plan["candidates"][0]["runtime"] == "hermes"
    assert plan["candidates"][0]["infrastructure_route"] == route
    assert capability_router.calls == [
        {
            "capability": "chat",
            "permission": "execute",
            "machine_id": "node-h",
            "backend_id": "runtime-h",
            "model_id": "model-h",
        }
    ]


@pytest.mark.asyncio
async def test_n8_infrastructure_gate_does_not_change_semantic_lane():
    capability_router = FakeCapabilityRouter(
        {
            "node-c": {
                **selected_route(
                    "node-c",
                    "runtime-c",
                    "model-c",
                ),
                "capability": "reasoning",
            }
        }
    )
    cfg = routing_studio("enforce")
    cfg.cognitive_runtime_bindings = {
        "claude": cfg.cognitive_runtime_bindings["claude"],
    }
    router = CognitiveRouter(
        cfg,
        FakeHerdr(),
        FakeRuntimes(),
        capability_router=capability_router,
    )

    plan = await router.plan(
        {
            "request_id": "COG-N8-DEEP",
            "prompt": "Reason through this carefully.",
            "capability": "reasoning_high",
        }
    )

    assert plan["lane"] == "deep"
    assert plan["deterministic_lane"] == "deep"
    assert plan["semantic_authority_changed"] is False
    assert plan["infrastructure_routing"][
        "semantic_authority_changed"
    ] is False
    assert plan["candidates"][0]["runtime"] == "claude"


@pytest.mark.asyncio
async def test_controlled_team_supervisor_context_exhaustion_clears_once_and_retries():
    router = CognitiveRouter(studio(), FakeHerdr(), FakeRuntimes())
    request = normalize_cognitive_request(
        {
            "request_id": "COG-CTX-RECOVER",
            "prompt": "Review the completed team work.",
            "capability": "coding",
            "preferred_depth": "deep",
            "timeout_seconds": 30,
        }
    )
    calls = []
    resets = []

    async def fake_invoke(runtime_id, current_request, runtime_rows):
        calls.append((runtime_id, current_request.request_id))
        if len(calls) == 1:
            raise CognitiveRouterError("runtime_context_exhausted")
        return "RECOVERED_RESULT"

    async def fake_reset(
        runtime_id,
        runtime_rows,
        *,
        timeout_seconds,
        request=None,
    ):
        resets.append((runtime_id, timeout_seconds, request.principal_id if request else None))
        return {
            "ok": True,
            "runtime": runtime_id,
            "pane_id": "wC:p1",
            "status": "done",
            "session_rotated": True,
        }

    router._invoke_specific_runtime = fake_invoke
    router._reset_runtime_context = fake_reset
    attempts = []

    output = await router._invoke_controlled_team_supervisor(
        request,
        {},
        attempts,
        stage="supervisor_review",
    )

    assert output == "RECOVERED_RESULT"
    assert len(calls) == 2
    assert len(resets) == 1
    assert attempts == [
        {
            "stage": "supervisor_context_recovery",
            "for_stage": "supervisor_review",
            "runtime": "claude",
            "status": "success",
            "session_rotated": True,
        }
    ]


@pytest.mark.asyncio
async def test_reset_runtime_context_uses_clear_and_requires_session_rotation():
    class ClearClient(FakeClient):
        async def call_tool(self, name, arguments, client_name="test"):
            if name == "herdr_prompt_agent" and arguments.get("prompt") == "/clear":
                pane_id = arguments.get("pane_id")
                self.herdr.calls.append((name, dict(arguments), client_name))
                pane = self.herdr.find_pane(pane_id=pane_id)
                pane["agent_session"] = {"value": "session-after"}
                pane["agent_status"] = "done"
                return {"content": [{"type": "text", "text": '{"accepted":true}'}]}
            return await super().call_tool(name, arguments, client_name=client_name)

    class ClearHerdr(FakeHerdr):
        def __init__(self):
            super().__init__()
            pane = self.find_pane(pane_id="wC:p1")
            pane["agent_session"] = {"value": "session-before"}

        def client(self, **kwargs):
            return ClearClient(self)

    herdr = ClearHerdr()
    router = CognitiveRouter(studio(), herdr, FakeRuntimes())

    recovery = await router._reset_runtime_context(
        "claude",
        {},
        timeout_seconds=10,
    )

    assert recovery["ok"] is True
    assert recovery["session_rotated"] is True
    clear_calls = [
        call for call in herdr.calls
        if call[0] == "herdr_prompt_agent" and call[1].get("prompt") == "/clear"
    ]
    assert len(clear_calls) == 1
