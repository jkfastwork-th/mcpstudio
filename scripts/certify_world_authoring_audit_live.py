from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from urllib.request import urlopen

BASE = "http://127.0.0.1:8100"
INGRESS = f"{BASE}/ingress/openai/serena-8001"


def post(payload: dict, session_id: str | None = None):
    with tempfile.TemporaryDirectory() as td:
        headers_path = Path(td) / "headers"
        body_path = Path(td) / "body"
        cmd = [
            "curl", "-fsS", "-D", str(headers_path), "-o", str(body_path),
            "-X", "POST", INGRESS,
            "-H", "Accept: application/json, text/event-stream",
            "-H", "Content-Type: application/json",
        ]
        if session_id:
            cmd += ["-H", f"Mcp-Session-Id: {session_id}"]
        cmd += ["--data", json.dumps(payload, separators=(",", ":"))]
        subprocess.run(cmd, check=True, timeout=60)
        header_text = headers_path.read_text()
        body = body_path.read_text()
    sid = None
    for line in header_text.splitlines():
        if line.lower().startswith("mcp-session-id:"):
            sid = line.split(":", 1)[1].strip()
    if not body.strip():
        return {}, sid
    lines = [line[5:].strip() for line in body.splitlines() if line.startswith("data:")]
    data = json.loads(lines[-1] if lines else body)
    return data, sid


init, sid = post({
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "world-authoring-audit-live-cert", "version": "1.0"},
    },
})
assert sid and sid.startswith("gws-"), sid

post({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
tools, _ = post({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, sid)
names = {tool["name"] for tool in tools["result"]["tools"]}
assert "mcpstudio_world_authoring_audit" in names
print("HIRDA_WORLD_AUTHORING_AUDIT_TOOL_EXPOSE_PASS")

call = {
    "jsonrpc": "2.0",
    "id": 3,
    "method": "tools/call",
    "params": {
        "name": "mcpstudio_world_authoring_audit",
        "arguments": {
            "workspace": "earth-616-pixi-world",
            "events": [
                {
                    "eventId": "live-audit-1",
                    "kind": "job_created",
                    "recordedAt": "2026-09-24T04:45:00Z",
                    "providerId": "live-cert",
                    "jobId": "job-live-1",
                    "requestId": "proposal-live-1",
                    "logicalId": "prop.live_lantern",
                    "details": {"progress": 0},
                },
                {
                    "eventId": "live-audit-2",
                    "kind": "asset_intake",
                    "recordedAt": "2026-09-24T04:45:01Z",
                    "providerId": "live-cert",
                    "jobId": "job-live-1",
                    "requestId": "proposal-live-1",
                    "logicalId": "prop.live_lantern",
                },
                {
                    "eventId": "live-audit-3",
                    "kind": "earth_validation_attached",
                    "recordedAt": "2026-09-24T04:45:02Z",
                    "jobId": "job-live-1",
                    "requestId": "proposal-live-1",
                    "logicalId": "prop.live_lantern",
                    "details": {
                        "validationId": "earth-live-validation-1",
                        "valid": True,
                        "mutationAuthorized": False,
                        "worldAuthority": "earth-616",
                    },
                },
                {
                    "eventId": "live-audit-4",
                    "kind": "asset_promoted",
                    "recordedAt": "2026-09-24T04:45:03Z",
                    "jobId": "job-live-1",
                    "requestId": "proposal-live-1",
                    "logicalId": "prop.live_lantern",
                    "details": {"validationId": "earth-live-validation-1"},
                },
            ],
        },
    },
}
result, _ = post(call, sid)
assert result["result"]["isError"] is False, result
payload = json.loads(result["result"]["content"][0]["text"])
assert payload["accepted"] == 4, payload
assert payload["audit_only"] is True
assert payload["world_authority_changed"] is False
assert payload["asset_promoted_by_hirda"] is False
print("HIRDA_WORLD_AUTHORING_AUDIT_CALL_PASS")

with urlopen(f"{BASE}/api/audit?limit=100", timeout=10) as resp:
    audit = json.loads(resp.read().decode())["audit"]

by_action = {}
for row in audit:
    by_action.setdefault(row["action"], []).append(row)

required = [
    "world_authoring.job_created",
    "world_authoring.asset_intake",
    "world_authoring.earth_validation_attached",
    "world_authoring.asset_promoted",
]
for action in required:
    assert any(row.get("target_id") == "earth-616-pixi-world" for row in by_action.get(action, [])), action

trace = "proposal-live-1:job-live-1:prop.live_lantern"
validation_rows = by_action["world_authoring.earth_validation_attached"]
promoted_rows = by_action["world_authoring.asset_promoted"]
assert any(
    row["data"].get("trace_id") == trace
    and row["data"].get("details", {}).get("validationId") == "earth-live-validation-1"
    and row["data"].get("world_authority_changed") is False
    for row in validation_rows
)
assert any(
    row["data"].get("trace_id") == trace
    and row["data"].get("details", {}).get("validationId") == "earth-live-validation-1"
    and row["data"].get("world_authority_changed") is False
    for row in promoted_rows
)
print("HIRDA_WORLD_AUTHORING_AUDIT_PERSISTENCE_PASS")
print("HIRDA_WORLD_AUTHORING_AUDIT_LIVE_CERT_PASS")
