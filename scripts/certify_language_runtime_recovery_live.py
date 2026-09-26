from __future__ import annotations

import asyncio
import json
import os

import httpx


URL = "http://127.0.0.1:8100/mcp/serena-8001"


def decode(response: httpx.Response) -> dict:
    if "text/event-stream" in response.headers.get("content-type", ""):
        for line in response.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
    return response.json()


async def main() -> None:
    token = os.environ.get("MCP_STUDIO_GATEWAY_TOKEN", "")
    if not token:
        raise SystemExit("MCP_STUDIO_GATEWAY_TOKEN is unavailable")
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "X-MCP-Studio-Client-Id": "hirda-lsp-live-cert",
        "X-MCP-Studio-Client-Type": "certification",
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        init = await client.post(URL, headers=headers, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "hirda-lsp-live-cert", "version": "1"},
            },
        })
        init.raise_for_status()
        session_id = init.headers.get("mcp-session-id")
        if not session_id:
            raise SystemExit("gateway returned no MCP session id")
        headers["Mcp-Session-Id"] = session_id
        await client.post(URL, headers=headers, json={
            "jsonrpc": "2.0", "method": "notifications/initialized"
        })
        cert = await client.post(URL, headers=headers, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {
                "name": "mcpstudio_certify_language_runtime_recovery",
                "arguments": {
                    "session": "ms-6614e2b121dd4aa0",
                    "confirm": "FAULT_INJECTION",
                },
            },
        })
        if cert.status_code >= 400:
            print("CERT_STATUS", cert.status_code)
            print(cert.text)
            raise SystemExit(3)
        payload = decode(cert)
        result = payload.get("result", {})
        blocks = result.get("content", [])
        text = next((x.get("text") for x in blocks if x.get("type") == "text"), "")
        print(text)


if __name__ == "__main__":
    asyncio.run(main())
