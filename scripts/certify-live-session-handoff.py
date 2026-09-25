from __future__ import annotations

import json
import os
import sys
import uuid

import httpx


BASE = os.environ.get(
    "HIRDA_HANDOFF_CERT_URL",
    "http://127.0.0.1:8100/ingress/openai/serena-8001",
)
TIMEOUT = float(os.environ.get("HIRDA_HANDOFF_CERT_TIMEOUT", "20"))


def decode_response(response: httpx.Response) -> dict:
    response.raise_for_status()
    text = response.text
    if "text/event-stream" in response.headers.get("content-type", ""):
        payloads = [
            line[5:].strip()
            for line in text.splitlines()
            if line.startswith("data:")
        ]
        if not payloads:
            raise RuntimeError(f"SSE response had no data payload: {text[:500]}")
        text = payloads[-1]
    return json.loads(text) if text else {}


def tool_value(payload: dict) -> dict:
    result = payload.get("result") or {}
    if result.get("isError"):
        blocks = result.get("content") or []
        message = blocks[0].get("text") if blocks else repr(result)
        raise RuntimeError(f"MCP tool error: {message}")
    blocks = result.get("content") or []
    if not blocks:
        return result
    text = blocks[0].get("text")
    if not isinstance(text, str):
        return result
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return {"text": text}
    return value if isinstance(value, dict) else {"value": value}


class MCPTransport:
    def __init__(self, client: httpx.Client, conversation_id: str):
        self.client = client
        self.conversation_id = conversation_id
        self.session_id: str | None = None
        self.next_id = 1

    @property
    def headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "X-OpenAI-Session": self.conversation_id,
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        return headers

    def initialize(self) -> None:
        payload = {
            "jsonrpc": "2.0",
            "id": self.next_id,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {
                    "name": "hirda-live-handoff-cert",
                    "version": "1.0",
                },
            },
        }
        self.next_id += 1
        response = self.client.post(BASE, headers=self.headers, json=payload)
        decode_response(response)
        self.session_id = response.headers.get("mcp-session-id")
        if not self.session_id:
            raise RuntimeError("initialize returned no Mcp-Session-Id")
        response = self.client.post(
            BASE,
            headers=self.headers,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        if response.status_code not in (200, 202):
            raise RuntimeError(
                f"notifications/initialized failed HTTP {response.status_code}: {response.text[:500]}"
            )

    def call(self, name: str, arguments: dict | None = None) -> dict:
        payload = {
            "jsonrpc": "2.0",
            "id": self.next_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        }
        self.next_id += 1
        response = self.client.post(BASE, headers=self.headers, json=payload)
        return tool_value(decode_response(response))


def main() -> int:
    suffix = uuid.uuid4().hex[:8]
    source_conversation = f"hirda-handoff-source-{suffix}"
    target_conversation = f"hirda-handoff-target-{suffix}"
    session_selector = os.environ.get("HIRDA_HANDOFF_CERT_SESSION", "projects-root")

    with httpx.Client(timeout=TIMEOUT) as client:
        source = MCPTransport(client, source_conversation)
        target = MCPTransport(client, target_conversation)
        managed_id: str | None = None

        try:
            source.initialize()
            existing = source.call(
                "mcpstudio_get_session",
                {"session": session_selector},
            )
            original = existing.get("session") or existing
            attached = source.call(
                "mcpstudio_use_session",
                {"session": session_selector},
            )
            managed = attached["managed_session"]
            gateway = attached["gateway_session"]
            managed_id = managed["id"]
            source_upstream = gateway["upstream_session_id"]
            source_gateway_id = gateway["id"]

            prepared = source.call(
                "mcpstudio_handoff_session",
                {
                    "summary": "Live A to B certification: preserve HIRDA managed Serena session continuity.",
                    "reason": "live-certification",
                    "ttl_seconds": 600,
                    "context_usage_percent": 88,
                },
            )
            token = prepared["claim_token"]
            if prepared["ownership_transferred"] is not False:
                raise AssertionError("prepare transferred ownership before claim")

            target.initialize()
            claimed = target.call("mcpstudio_accept_handoff", {"token": token})
            target_gateway = claimed["gateway_session"]

            assert claimed["ownership_transferred"] is True
            assert claimed["claim_consumed"] is True
            assert claimed["source_gateway_closed"] is True
            assert claimed["upstream_session_adopted"] is True
            assert target_gateway["managed_session_id"] == managed_id
            assert target_gateway["upstream_session_id"] == source_upstream
            assert target_gateway["id"] != source_gateway_id

            current = target.call("mcpstudio_current_session")
            assert current["managed_session"]["id"] == managed_id

            source_closed = False
            try:
                source.call("mcpstudio_current_session")
            except (httpx.HTTPStatusError, RuntimeError):
                source_closed = True
            if not source_closed:
                raise AssertionError("source transport remained usable after ownership transfer")

            # This Serena call proves the adopted upstream session is alive and
            # usable by the target transport without a second initialize.
            serena = target.call("initial_instructions")
            if "Serena" not in str(serena):
                raise AssertionError("target did not receive a Serena response after handoff")

            replay_failed = False
            try:
                target.call("mcpstudio_accept_handoff", {"token": token})
            except RuntimeError as exc:
                replay_failed = "claimed" in str(exc)
            if not replay_failed:
                raise AssertionError("single-use handoff token could be replayed")

            print("HIRDA_HANDOFF_LIVE_PASS")
            print(
                json.dumps(
                    {
                        "managed_session_id": managed_id,
                        "source_gateway_id": source_gateway_id,
                        "target_gateway_id": target_gateway["id"],
                        "adopted_upstream_session_id": source_upstream,
                        "source_conversation": source_conversation,
                        "target_conversation": target_conversation,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        finally:
            if managed_id and original.get("desired_state") == "stopped":
                cleanup = target if target.session_id else source
                try:
                    cleanup.call("mcpstudio_close_session", {"session": managed_id})
                except Exception as exc:
                    print(f"cleanup warning: {exc}", file=sys.stderr)


def test_live_session_handoff() -> None:
    assert main() == 0


if __name__ == "__main__":
    raise SystemExit(main())
