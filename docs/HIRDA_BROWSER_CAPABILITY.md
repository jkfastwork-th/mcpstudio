# HIRDA Browser Capability

HIRDA uses OpenBrowser as its bounded DOM/CDP browser backend and Computer Use as the visual fallback plane.

## Routing contract

1. Agents call the HIRDA-prefixed OpenBrowser tools (`hirda__openbrowser__*`).
2. HIRDA applies managed-session permission policy, machine policy, Reflex, and JEV evidence before dispatch.
3. OpenBrowser executes only the bounded adapter action. The upstream raw `execute_code` tool is never exposed to lanes.
4. If a DOM/CDP action fails, HIRDA may escalate to session-isolated Computer Use/VNC.
5. An automatic fallback never claims that the original action completed.

The routing strategy identifier is:

`dom_cdp_first_visual_fallback`

## Permission boundary

Computer Use provisioning is an `execute` side effect.

- An OpenBrowser action already authorized as `execute` may automatically provision the visual fallback descriptor after a deterministic DOM/CDP failure.
- A failed `read` or `write` browser action cannot silently upgrade authority. HIRDA returns `fallback_required=true` and names `hirda__browser__visual_fallback` as the next tool. That explicit tool is classified as `execute` and must pass the normal policy/Reflex/JEV path separately.
- Remote machines cannot fall through to the local Computer Use provider.

## Session isolation and persistence

Each managed session receives a stable hashed OpenBrowser profile directory plus a per-session `storage_state.json`.

This preserves cookies/login state across OpenBrowser MCP process restarts while keeping sessions isolated from one another. MCP shutdown closes stdin first so OpenBrowser can run its finalization path before HIRDA falls back to process termination.

## Visual fallback payload

A visual fallback result reports:

- `action_completed: false`
- `fallback_required: true`
- `fallback_mode: computer_use_vnc`
- `managed_session_id`
- safe viewer metadata such as `viewer_url`, `websocket_path`, display number, and session CDP port

Raw VNC host/port values are not exposed.

## Certified behavior

The production certification covers:

- bounded OpenBrowser tool discovery
- raw `execute_code` suppression
- DOM navigation and form interaction
- cookie persistence across OpenBrowser process restart
- cookie isolation between managed sessions
- automatic execute-class DOM/CDP failure to VNC fallback
- read-class failure returning an execute-gated escalation hint without provisioning VNC
- MCP gateway `tools/list` exposure of `hirda__browser__visual_fallback`
