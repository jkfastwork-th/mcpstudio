# M6.2.3 Phase A — OpenAI Secure Tunnel → MCP Studio

This phase removes the direct OpenAI-tunnel bypass around MCP Studio without changing Serena itself.

## Before

`OpenAI Secure Tunnel → tunnel-client → 127.0.0.1:8001/mcp → Serena`

## After Phase A

`OpenAI Secure Tunnel → tunnel-client → 127.0.0.1:8100/ingress/openai/serena-8001 → MCP Studio → Serena :8001`

The new `/ingress/openai/{server_id}` route is local-only and returns 404 for non-loopback callers. It does not weaken the public OAuth/bearer route at `/mcp/{server_id}`.

The Phase-A identity scope is intentionally `transport`, so a fresh initialize creates a fresh logical Studio session instead of connector-wide reclaim. Explicit Session Manager/project pinning comes in Phase B.

## Safe order

1. Upgrade Studio to v0.9.5.
2. Enable `openai_local_ingress_enabled` in `config.yaml` and restart Studio.
3. Register the OpenAI ingress inventory row.
4. Run `certify-m6.2.3a-openai-ingress.sh` while the existing tunnel still points to 8001.
5. Only after local certification passes, run the cutover script.
6. Trigger a real ChatGPT/OpenAI-tunnel tool call and confirm the session has `last_tunnel_id=openai-serena` and `ingress_provider=openai`.
7. After real-client certification, change the systemd dependency from Serena to `mcp-studio.service` in a later hardening step.

Rollback is one file restore plus restart of `tunnel-client.service`; Serena and Cloudflare are not restarted.
