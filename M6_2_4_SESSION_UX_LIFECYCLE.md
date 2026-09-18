# M6.2.4 — Session UX + Lifecycle Polish

M6.2.4 keeps the M6.2.3C routing/isolation model unchanged and improves how durable managed sessions are operated.

## Goals

- active-first managed session view
- friendly lifecycle states: `active`, `idle`, `stopped`, `error`
- connected transport count and ingress providers on each managed session
- persistent last-use timestamp and use counter
- safe resume of stopped sessions
- rename without changing project pin or Serena instance identity
- per-session lifecycle + transport history
- ChatGPT tools for rename/history
- optional idle auto-stop (disabled by default)

## Safety invariants

- project pin remains authoritative
- one managed session still owns one dedicated Serena process
- `activate_project` remains blocked for pinned sessions
- no ingress bypasses MCP Studio after M6.2.3C cutover
- idle auto-stop never stops a session with a connected gateway transport
- lifecycle telemetry failure must not break routing

## New API

- `POST /api/managed/sessions/{id}/rename`
- `POST /api/managed/sessions/{id}/resume`
- `GET /api/managed/sessions/{id}/history`

`GET /api/managed/sessions` now includes:

- `lifecycle_state`
- `connected_transports`
- `transport_history_count`
- `ingress_providers`
- `last_transport_seen_at`
- `last_used_at`
- `use_count`

## New ChatGPT tools

- `mcpstudio_rename_session`
- `mcpstudio_session_history`

Existing `mcpstudio_use_workspace` and `mcpstudio_use_session` already auto-resume a stopped managed session.

## Optional idle auto-stop

```yaml
studio:
  managed_session_idle_stop_seconds: 0
  managed_session_history_limit: 100
```

`0` disables automatic idle stopping. When enabled, only `ready` sessions with zero connected transports are eligible.
