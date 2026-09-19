# HIRDA — Agent Runtime & Control Plane

**One control plane. Many agents.**

HIRDA is the product identity succeeding MCP Studio. During M1, user-facing branding transitions to HIRDA while infrastructure identifiers remain backward-compatible. See `HIRDA_M1_IDENTITY_PRODUCT_BOUNDARY.md` for the naming and migration boundary.

Current implementation lineage: **MCP Studio v0.9.9 — M6.2.4 Session UX + Lifecycle Polish**.

This release builds on the certified M6.2.3C production cutover. Routing, project pinning and Serena process isolation are unchanged; M6.2.4 makes durable managed sessions easier to operate.

## What changed

- active-first Managed Sessions UI
- lifecycle states: active / idle / stopped / error
- transport count + ingress provider summary per managed session
- last-used timestamp and use counter
- Resume, Rename and History actions in the UI
- stopped sessions hidden by default behind **Show stopped**
- per-session lifecycle/audit and transport history
- ChatGPT tools `mcpstudio_rename_session` and `mcpstudio_session_history`
- optional idle auto-stop, disabled by default
- schema v7
- compatibility fixes for prior M6.2.3 certification scripts
- HIRDA-native Graft read-only context plane with per-workspace enable/disable, deterministic rollout, circuit breaker and Serena-only fallback signaling
- ChatGPT controls `mcpstudio_graft_status`, `mcpstudio_graft_configure`, `mcpstudio_graft_query`, `mcpstudio_graft_rollback`, and `mcpstudio_graft_rearm`

See `M6_2_4_SESSION_UX_LIFECYCLE.md` for the lifecycle model.

## HIRDA Graft context plane

Graft is integrated as a **sidecar read-only context accelerator**, not as an editor. It can discover, trace, grep, map, inspect file APIs and check freshness; Serena remains source of truth and the sole edit authority under the existing managed-session lease/permission policy.

Graft state is durable per workspace. A failure/rejection opens a latched circuit and returns `fallback_required=true` with `mode=serena-only`. Rearming is always explicit.

The Graft sidecar authority profile is fixed:

```text
read context            yes
execute Graft queries   yes
source edit authority   no
destructive authority   no
cognitive-memory write  no
```

Enabling Graft does **not** remove Serena's existing write authority. See `HIRDA_GRAFT_READ_ONLY_CONTEXT.md`.

## Upgrade

```bash
cd /home/alfred/mcp-studio

tar -xzf /home/alfred/mcp-studio-m6.2.4-v0.9.9.tar.gz \
  -C /home/alfred/mcp-studio \
  --strip-components=1

./.venv/bin/python -m pip install -e '.[dev]'
./.venv/bin/python -m pytest -q

sudo systemctl restart mcp-studio.service
sudo systemctl restart tunnel-client.service
```

Expected test result:

```text
100 passed
```

## Verify runtime

```bash
curl -s http://127.0.0.1:8100/api/status | jq '.studio.version, .operations.schema'
```

Expected runtime version:

```text
0.9.9-m6.2.4
```

Schema should be version 7.

## Certification

First confirm the already-certified cutover still passes:

```bash
export M623C_WORKSPACE_A=nova-oracle
export M623C_WORKSPACE_B=earth-616
./scripts/certify-m6.2.3c-production-cutover.sh
```

Then run:

```bash
./scripts/certify-m6.2.4-session-lifecycle.sh
```

Target:

```text
M6_2_4_RUNTIME_PASS
M6_2_4_SCHEMA_V7_PASS
M6_2_4_SESSION_MANAGER_PASS
M6_2_4_LIFECYCLE_VIEW_PASS
M6_2_4_HISTORY_RENAME_PASS
M6_2_4_UI_LIFECYCLE_PASS

=============================================
M6_2_4_SESSION_UX_LIFECYCLE_PASS
=============================================
```

## Optional idle stop

The default is deliberately conservative:

```yaml
studio:
  managed_session_idle_stop_seconds: 0
  managed_session_history_limit: 100
```

Leave `managed_session_idle_stop_seconds: 0` until you intentionally want idle managed Serena processes to stop automatically.

## 0.9.10 / M6.2.5 — ChatGPT control-tool exposure

The ChatGPT-facing MCP catalog now prioritizes `mcpstudio_use_workspace`, `mcpstudio_create_session`, and `mcpstudio_use_session`. Tool discovery is resilient to multi-event SSE responses and can fall back to Studio-local control tools if Serena discovery is temporarily unavailable. See `M6_2_5_CHATGPT_CONTROL_TOOLS.md`.
