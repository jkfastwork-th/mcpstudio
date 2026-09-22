from types import SimpleNamespace

import pytest

from mcp_studio.cognitive_router import (
    CognitiveRouter,
    CognitiveRouterError,
    JevCognitiveJudgment,
    _extract_marked_result,
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

@pytest.mark.asyncio
async def test_result_marker_isolates_current_answer_from_pane_history():
    herdr = FakeHerdr()
    herdr.outputs["wH:p1"] = (
        "old pane transcript\n"
        "<<<HIRDA_COGNITIVE_RESULT:COG-MARKER>>>\n"
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
        "<<<HIRDA_COGNITIVE_RESULT:COG-ESCAPED>>>"
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
        "<<<HIRDA_COGNITIVE_RESULT:COG-EMPTY>>>\n"
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
    assert "<<<HIRDA_COGNITIVE_RESULT:COG-PROMPT>>>" in sent_prompt
    assert "<<<END_HIRDA_COGNITIVE_RESULT>>>" in sent_prompt
    # Must NOT present an empty marker pair for the runtime to copy.
    assert (
        "<<<HIRDA_COGNITIVE_RESULT:COG-PROMPT>>>\n"
        "<<<END_HIRDA_COGNITIVE_RESULT>>>"
    ) not in sent_prompt
    # Must NOT contain the literal placeholder
    assert "<final answer here>" not in sent_prompt


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
    assert "COG-REPAIR" in prompt_calls[1][1]["prompt"]


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
async def test_prompt_wait_barrier_avoids_idle_race_when_supported():
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
    assert len(wait_calls) == 1


@pytest.mark.asyncio
async def test_claude_wait_barrier_targets_done_state():
    class ClaudeWaitHerdr(FakeHerdr):
        def __init__(self):
            super().__init__()
            prompt_tool = dict(PROMPT_TOOL)
            prompt_schema = dict(prompt_tool["inputSchema"])
            prompt_props = dict(prompt_schema["properties"])
            prompt_props["wait"] = {"type": "boolean"}
            prompt_schema["properties"] = prompt_props
            prompt_tool["inputSchema"] = prompt_schema
            self.tools["herdr_prompt_agent"] = prompt_tool

    herdr = ClaudeWaitHerdr()
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
    assert result.attempts[1]["status"] == "success"


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
            "runtime": "hermes",
            "status": "failed",
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
async def test_plan_keeps_jev_advisory_and_nova_semantic_authority():
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
    assert plan["candidates"][0]["runtime"] == "hermes"


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
    assert result.attempts[-1] == {"runtime": "claude", "status": "success"}
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
