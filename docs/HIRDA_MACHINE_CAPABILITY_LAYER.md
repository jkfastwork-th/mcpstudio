# HIRDA Machine Capability Layer

HIRDA treats machines as explicit control-plane entities rather than implicit properties of an agent lane.
A managed session remains pinned to one logical workspace and may additionally bind to one target machine.

## Machine registry

`studio.machine_registry` declares operator-approved machines. A record contains an id, OS/address metadata,
capabilities, provider bindings, and optional per-workspace path mappings. No arbitrary host discovery occurs.
The configured `machine_local_id` is the default for existing sessions that predate machine binding.

Provider modes currently supported:

- `desktop_commander.mode: local` — use HIRDA's local Desktop Commander backend.
- `desktop_commander.mode: agent` — use the bounded HIRDA machine agent over a private network.
- `computer_use.mode: local` — use HIRDA's existing session-isolated VNC/Chrome runtime.

Machine snapshots combine declared inventory with live provider health. Remote agent health is probed over its
configured endpoint. Local Desktop Commander readiness comes from the integration lifecycle; local Computer Use
readiness comes from `ComputerUseManager.status()`.

## Capability router

`MachineCapabilityRouter` sits between HIRDA's permission/session gates and the physical backend. Tool names do
not change when a session moves machines. For example `hirda__desktop_commander__read_file` is resolved using the
managed session's `metadata.machine_id`:

```text
lane / MCP client
  -> HIRDA gateway
  -> managed-session permission + Reflex/JEV gates
  -> MachineCapabilityRouter
       -> openclaw: local Desktop Commander
       -> JKFASTDEV: HIRDA machine agent -> Desktop Commander
```

The remote machine agent exposes only the same bounded 13 Desktop Commander file/process tools already approved by
HIRDA. It independently enforces workspace path confinement and per-session process ownership. This gives defense in
depth: a request must pass both HIRDA policy and the target machine agent's scope checks.

## Session machine binding

Machine binding is stored in the existing managed-session metadata and therefore requires no database schema migration.
Sessions without a binding resolve to `machine_local_id` for backward compatibility.

Control surfaces:

- MCP `mcpstudio_list_machines`
- MCP `mcpstudio_bind_machine`
- optional `machine` on `mcpstudio_create_session` and `mcpstudio_use_workspace`
- REST `GET /api/machines`
- REST `PUT /api/managed/sessions/{session_id}/machine`

Remote machines must declare a `workspace_map` for each logical HIRDA workspace they may access. Missing mappings fail
closed. A remote session never silently falls through to the local filesystem or local Computer Use runtime.

## JKFASTDEV transport

The initial Windows node uses its Tailscale address and runs `scripts/hirda_machine_agent.py`. The listener binds only
to the machine's Tailscale IP and accepts requests only from the declared openclaw Tailscale IP. No public interface is
opened and no shared bearer secret is copied between machines. A limited current-user scheduled task starts the agent on
Windows logon.

Computer Use is intentionally not advertised for JKFASTDEV in this phase. A session bound to JKFASTDEV will therefore
reject Computer Use instead of opening openclaw's VNC desktop. Remote GUI transport can be added as a later provider
without changing the machine/session contract.
