# M6.2.3C — Production Session Cutover

M6.2.3C makes the managed-session isolation model from Phase B the production
default for Serena tool execution.

## Invariant

All external ingress terminates at MCP Studio. The shared Serena `:8001`
instance may be used only as a bootstrap/discovery upstream. A Serena
`tools/call` request is not allowed to execute on that shared upstream when
production cutover is enabled.

```text
OpenAI Secure Tunnel ─┐
Cloudflare / OAuth ───┼─> MCP Studio :8100
LAN / future ingress ─┘          │
                                 ├─ unbound: initialize + tool discovery/control only
                                 │
                                 └─ pinned managed session
                                      └─ dedicated Serena :82xx --project <workspace>
```

## User workflow

From UI, create/select an approved managed session and attach the client
transport. From ChatGPT, the shortcut tool is `mcpstudio_use_workspace`:

```text
Use workspace nova-oracle for this chat.
```

Studio creates or resumes the durable managed session for that workspace,
attaches the transport, and persists the project pin on the logical Studio
session. Reconnected sibling transports converge back to the same pin.

## Safety rules

- Unbound Serena `tools/call` requests fail closed.
- `activate_project` is blocked while a managed project is pinned.
- Defense in depth blocks any non-management tool call that would otherwise
  reach the shared/base Serena upstream during cutover.
- Managed sessions remain one project per Serena process.
- OpenAI Secure MCP Tunnel must target the loopback Studio ingress, never
  `127.0.0.1:8001/mcp` directly.

## Boot ordering

The OpenAI tunnel client is now an ingress to Studio, so its systemd ordering
must reference `mcp-studio.service`, not the legacy Serena service. Install the
Phase C drop-in with `scripts/install-m6.2.3c-tunnel-dependency.sh`. The tunnel
remains independently restartable; the drop-in only makes Studio the preferred
startup dependency.
