# M6.2.3B — Managed Session + Serena Instance Pool

M6.2.3B turns a Studio logical session into an explicit project-isolation
boundary. Serena keeps its active coding project at the process level, so Studio
does **not** try to multiplex different projects through one Serena process.
Instead, every running managed session owns one loopback-only Serena child
process started with an immutable `--project` argument.

## Runtime model

```text
Cloudflare / OpenAI Secure Tunnel / LAN
                  |
                  v
          MCP Studio :8100
                  |
          Managed Session Manager
          /          |          \
       Nova       Earth-616    Oriverse
       PINNED       PINNED       PINNED
         |            |            |
   Serena :8210  Serena :8211  Serena :8212
         |            |            |
      project A    project B    project C
```

Ports in the configured pool are loopback-only internal implementation details.
Clients continue to use Studio on port 8100.

## Safety properties

- A project must be under one of `managed_session_workspace_roots`.
- A project can have only one running managed session in the Studio process.
- Creation and port allocation are serialized to close concurrent-create races.
- The child process uses the certified production Serena template:
  `serena start-mcp-server --transport streamable-http --port <port> --context chatgpt --project <path>`.
- Project switching (`activate_project` by default) is blocked for a pinned
  transport.
- When `managed_session_require_binding_for_tools=true`, ordinary Serena tool
  calls fail closed until the transport chooses or creates a managed session.
- Management tools are served locally by Studio and never forwarded to Serena.
- The logical Studio session is authoritative for the project pin. Reclaimed or
  sibling gateway transports converge on the same managed session before
  forwarding traffic.
- A reclaimed logical session initializes directly against its pinned Serena
  instance; it never briefly falls back to the neutral/base Serena process.
- Stopping/restarting a managed session is refused while a connected gateway is
  attached. Detach first.
- Studio shutdown terminates its child processes but preserves desired state;
  `managed_session_auto_restore` recreates them after Studio starts again.

## MCP management tools

When enabled, Studio augments `tools/list` with:

- `mcpstudio_list_workspaces`
- `mcpstudio_register_workspace`
- `mcpstudio_list_sessions`
- `mcpstudio_get_session`
- `mcpstudio_create_session`
- `mcpstudio_use_session`
- `mcpstudio_current_session`
- `mcpstudio_detach_session`
- `mcpstudio_close_session`

The UI and MCP tools use the same manager and database APIs.

## Rollout

1. Install v0.9.7 while M6.2.3A OpenAI ingress remains enabled.
2. Register at least two real workspace paths.
3. Run `enable-m6.2.3b-session-manager.py` to enable the pool and create a
   timestamped config backup.
4. Run `install-production.sh`. This renders systemd write carve-outs only for
   the approved project roots plus Serena's local state directories.
5. Verify `mcp-studio.service` is healthy.
6. Run `certify-m6.2.3b-session-manager.sh`.
7. Create/use sessions from the UI or ChatGPT.

The legacy/base Serena on 8001 remains available to Studio as a neutral
bootstrap upstream during this phase. Ingress clients should not connect to it
directly.
