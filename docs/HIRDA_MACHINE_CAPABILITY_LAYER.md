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

## Per-machine policy

Every machine may declare a hard permission ceiling with `read`, `write`, `execute`, `destructive`, and
`fail_closed_unknown`. Effective authority is the intersection of the managed-session policy and the target-machine
policy. A machine can therefore restrict a session further but can never grant authority the session does not already
have. Backend requests blocked by this ceiling return `MACHINE_PERMISSION_DENIED` (or
`MACHINE_PERMISSION_UNCLASSIFIED` for fail-closed unknown operations) before Reflex/JEV or physical backend dispatch.

The same machine policy protects Computer Use: descriptor/repair-target reads require machine `read`, while runtime
repair and interactive VNC require machine `execute`. This prevents GUI access from becoming a bypass around shell/file
policy. The registry snapshot and `GET /api/machines/{machine_id}/policy` expose the effective configured ceiling for
operators and UI surfaces.

Production defaults are deliberately asymmetric: openclaw permits read/write/execute but denies destructive actions;
JKFASTDEV permits read/write while execute/destructive remain disabled until explicitly approved in configuration.

## Machine enrollment / auto-onboarding

When `machine_enrollment_enabled` is on, HIRDA periodically reads the local Tailscale peer inventory and probes only
online peers inside the configured Tailnet CIDRs for the Machine Agent `/identity` endpoint. Discovery never registers a
new machine or grants authority. Unknown agents are written to an atomic, git-ignored local sidecar as `pending`.

An operator must explicitly approve each pending identity through the Studio UI, REST API, or MCP control tools. New
approvals default to `read=true`, `write=false`, `execute=false`, `destructive=false`, with unknown actions fail-closed.
The approval may add workspace mappings and deliberately widen the ceiling, but the UI never offers a destructive
profile. Rejected identities remain outside the runtime registry. Operator-declared `config.yaml` machine entries always
win over sidecar enrollment records on restart.

Control surfaces:

- REST `GET /api/machines/enrollments`
- REST `POST /api/machines/discover`
- REST `POST /api/machines/enrollments/{machine_id}/approve`
- REST `POST /api/machines/enrollments/{machine_id}/reject`
- MCP `mcpstudio_list_machine_enrollments`
- MCP `mcpstudio_discover_machines`
- MCP `mcpstudio_approve_machine`
- MCP `mcpstudio_reject_machine`

The Machine Agent advertises only machine identity, platform/architecture, approved capability classes, and provider
availability. It does not advertise filesystem contents, workspace paths, or credentials. The agent remains bound to its
Tailscale interface and accepts requests only from the configured HIRDA control-plane Tailnet IP.

## JKFASTDEV transport

The initial Windows node uses its Tailscale address and runs `scripts/hirda_machine_agent.py`. The listener binds only
to the machine's Tailscale IP and accepts requests only from the declared openclaw Tailscale IP. No public interface is
opened and no shared bearer secret is copied between machines. A limited current-user Startup launcher starts the agent on Windows logon.

Computer Use is intentionally not advertised for JKFASTDEV in this phase. A session bound to JKFASTDEV will therefore
reject Computer Use instead of opening openclaw's VNC desktop. Remote GUI transport can be added as a later provider
without changing the machine/session contract.
