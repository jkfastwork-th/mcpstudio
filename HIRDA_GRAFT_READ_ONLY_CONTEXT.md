# HIRDA Graft Read-Only Context Plane

HIRDA integrates Graft as a workspace-scoped sidecar context accelerator.

## Authority boundary

- Graft: derived read-only context only.
- Serena: source verification and sole edit authority.
- Tests: promotion gate.
- Graft never receives source-edit, destructive, or Nova cognitive-memory write authority from this integration.
- Graft may update its own generated `graft/` graph/cache as part of context refresh; that is derived state, not source-edit authority.

Enabling Graft does not modify the managed Serena session permission profile. This preserves the certified architecture where Serena can still perform explicitly authorized edits while Graft remains context-only.

## Durable workspace state

HIRDA stores the Graft profile in managed-workspace metadata:

- enabled
- rollout_percent
- circuit_open
- circuit_reason
- mode = read_only
- write_authority = false
- destructive_authority = false
- cognitive_memory_write = false

The circuit survives transport reconnects and HIRDA process restarts because it is persisted in SQLite workspace metadata.

## MCP management tools

- `mcpstudio_graft_status`
- `mcpstudio_graft_configure`
- `mcpstudio_graft_query`
- `mcpstudio_graft_rollback`
- `mcpstudio_graft_rearm`

All accept an explicit workspace/session selector; when omitted they resolve to the currently attached managed session.

## REST API

- `GET /api/managed/workspaces/{workspace_key}/graft`
- `PATCH /api/managed/workspaces/{workspace_key}/graft`
- `POST /api/managed/workspaces/{workspace_key}/graft/query`
- `POST /api/managed/workspaces/{workspace_key}/graft/rollback`
- `POST /api/managed/workspaces/{workspace_key}/graft/rearm`

## Canary and rollback semantics

Rollout selection is deterministic from request id.

When Graft is disabled, outside the canary bucket, circuit-open, unavailable, throws, or rejects a tool result, HIRDA returns:

```json
{
  "mode": "serena-only",
  "fallback_required": true
}
```

A Graft exception or rejected tool result opens the durable circuit. The circuit does not rearm automatically.

## Supported Graft tools

- `graft_find_code`
- `graft_file_api`
- `graft_check_freshness`
- `graft_trace_calls`
- `graft_find_all`
- `graft_repo_map`

No Graft edit tool is exposed.

## Configuration

```yaml
studio:
  graft_enabled: true
  graft_cli_path: /data/graft-serena-lab/repos/target/dist/cli.js
  graft_node_executable: node
  graft_request_timeout_seconds: 30
  graft_default_rollout_percent: 100
```

## Verification

Unit tests use a deterministic fake MCP process. A separate real-CLI integration test calls the certified G7 multi-repo fixture through the HIRDA `GraftManager` and verifies native child scoping.
