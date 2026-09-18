# MCP Studio Architecture — M6.2 Observability & SLO

M6.2 adds an observation layer above the M6.1 operations supervisor. The layer
is deliberately non-authoritative: it can sample, evaluate, and alert, but it
cannot restart services, kill agents, redispatch work, or mutate external
tunnels.

```text
ChatGPT / MCP clients
        │ OAuth + Streamable HTTP
        ▼
MCP Studio Gateway ── request outcome/latency ─┐
        │                                      │
        ▼                                      ▼
Serena / Herdr                         ObservabilityManager
        │                                      │
        ▼                                      ├─ availability samples
Workers / Queue ── queue + saturation ─────────┤
                                               ├─ SLO evaluator
M6.1 Operations ── orphan/detach events ───────┤
                                               ├─ alert dedupe/resolve
Restore drill reports ─────────────────────────┘
                                               │
                                               ▼
                                  SQLite schema v3 + Dashboard
```

## Data model

`mcp_request_metrics` stores only time, server id, HTTP/RPC method name, status
code, latency, coarse client class, and optional gateway-session id. It never
stores request bodies, authorization headers, bearer values, OAuth codes, or
refresh tokens.

`observability_samples` stores one low-cardinality system sample per configured
interval: upstream health, worker busy/total counts, queue depth, active work,
and open-alert count. Retention is configurable and cleaned automatically.

## SLO semantics

Availability is sample-based over the configured rolling window. MCP service
success is based on authenticated gateway requests; status codes below 500 are
counted as service successes so client/protocol 4xx responses do not consume
server error budget. Queue latency uses work `queued_at → started_at`. Reconnect
rate is the number of upstream gateway-session recoveries per 100 authenticated
MCP requests.

Objectives with insufficient data are `unknown`. Threshold breaches become
`degraded` or `down` and are reflected as deduplicated operational alerts.
When a metric recovers, the matching alert is resolved automatically.

## Error budgets

Availability and MCP success objectives expose an error-budget-remaining
percentage. A healthy objective has 100% remaining unless it has consumed part
of the allowed gap. Crossing the objective consumes the budget to zero and
marks the objective down.

## Restore signal

The weekly M6.1 restore drill remains non-destructive. M6.2 reads only the
latest JSON report under `backups/restore-drills/` and surfaces PASS/FAIL/UNKNOWN
in the report and dashboard. It never restores over the live database.

## Trust boundary

Observability is read-mostly and local. It may write metric samples, audit/event
records, and operational alerts. It has no external-action authority and does
not change the existing fail-closed production/test-mode boundary.


## SaaS UI refresh

This package includes a presentation-only SaaS dashboard refresh for the M6.2 production UI. Runtime, OAuth, execution, connectivity, session, operations, and SLO semantics are unchanged.

## M6.2.1 — Lease lifecycle integrity

Workspace binding is now an affinity hint, not proof of an active writer. The
write lease exists only while a worker is actively executing/manual-busy.
Scheduler leases are linked to `work_id`; degraded workers retain their lock
while a running work row still owns the workspace. This is the fencing rule
that prevents reconnect/recovery from opening a second writer.

A legacy idle lease is invalid and is reclaimed by Operations reconciliation.
An active worker cannot be rebound/released through the generic worker API while
running work still claims it; the work must finish, fail, cancel-before-dispatch,
or be explicitly detached first.


## M6.2.2 — Tunnel-aware session attribution

`gateway_sessions` is the ingress attribution boundary. A logical `studio_session` may survive reconnects and be reclaimed by a new transport, while each `gateway_session` records the ingress tunnel observed for that transport. This prevents a connector identity from being incorrectly treated as a tunnel identity. Attribution is advisory/observability-only and cannot grant access.

```text
ChatGPT transport A -> Cloudflare tunnel A -> gateway_session A -> studio_session X
ChatGPT transport B -> Cloudflare tunnel B -> gateway_session B -> studio_session X
```

The UI can therefore group A and B under separate tunnels while still showing that both reclaim the same logical Studio session.

## M6.2.3 Phase A — Unified ingress boundary

OpenAI Secure MCP Tunnel no longer needs to terminate directly at Serena. Studio exposes a loopback-only route (`/ingress/openai/{server_id}`) intended for the local `tunnel-client` process. Public and LAN callers are rejected at the route boundary. The existing `/mcp/{server_id}` OAuth/bearer behavior is unchanged.

Phase A intentionally uses transport-scoped logical sessions for OpenAI local ingress so separate initializes are not conflated into one connector-wide workspace. Phase B adds explicit Session Manager/project pinning and management tools.

## M6.2.3B — Durable managed sessions and Serena process isolation

Serena's active coding project is process-scoped, so a logical transport lock is
not a sufficient project-isolation boundary. M6.2.3B introduces a dedicated
Serena child process per running managed session. Each child binds only to
loopback and starts with an immutable `--project` selection.

The durable `sessions.managed_session_id` value is authoritative. Gateway
transports are ephemeral and store their current managed binding plus private
upstream URL/session id. On a reclaimed initialize, Studio restores the logical
pin before contacting Serena and initializes directly against the dedicated
instance. On later requests, a sibling transport whose binding differs from the
logical pin is reconciled before forwarding.

This separates three identities deliberately:

```text
ingress/tunnel identity -> gateway transport -> logical Studio session
                                             -> managed project session
                                             -> dedicated Serena process
```

Ingress attribution remains observability metadata and cannot grant project
access. Project paths must be in the managed workspace registry and beneath an
approved root.

## M6.2.3C — Production session cutover

Phase C turns managed project isolation into the production routing invariant.
The base Serena server remains available only for MCP bootstrap/discovery.
Coding/tool execution requires a durable managed-session pin and therefore a
dedicated Serena process. `mcpstudio_use_workspace` is the single-step control
operation used by ChatGPT and automation to create/resume and attach an approved
workspace. UI and MCP tools continue to use the same Session Manager backend.

## M6.2.4 — Session UX + lifecycle

M6.2.4 does not change the M6.2.3C isolation boundary. It adds durable session activity metadata and a lifecycle view above the same managed-session-to-dedicated-Serena mapping. Gateway attachment updates `last_used_at`/`use_count`; UI/API history is observational. Optional idle auto-stop is conservative and requires zero connected transports.
