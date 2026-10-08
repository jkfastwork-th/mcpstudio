# HIRDA Disk Maintenance Tool v0.1 — review required

## Purpose

A **local-only** maintenance API for OpenClaw. It is **not** a shell tool,
arbitrary path cleaner, or an exception that silently overrides the HIRDA
machine's `destructive=false` rule.

The dedicated `maintenance` permission is separate from shell destructive
authority and defaults to **false**. `disk_maintenance_enabled` also defaults
to **false**. Both must be explicitly enabled by the machine owner after
testing and reviewing the policy; this branch changes neither production
configuration nor the machine's running permissions.

## Fixed targets

- `codex.old_releases` — for each installed Codex package family, keep
  the newest **three** versions, the selected CLI executable, and any release
  referenced by current process command lines.
- `npm.cacache` — only `$HOME/.npm/_cacache`, when no npm process is
  installing/executing.
- `bun.download_cache` — only `$HOME/.bun/install/cache`, when no
  Bun package activity is detected.

**Never targeted:** `/home/alfred/mcp-studio/backups`, `data/*.sqlite3`,
worktrees, `.codex/sessions`, Nova content, running binaries, syslog, or
unlisted paths. Path input from the client is not accepted.

## API workflow

Routes require an **explicit owner bearer token header**
`X-HIRDA-OWNER-TOKEN`. Cookie sessions alone are not sufficient.

1. `GET /api/maintenance/disk/status` reports disabled/permission state.
2. `POST /api/maintenance/disk/plan` with
   `{"targets":["codex.old_releases"]}` returns a **dry-run** list,
   manifest fingerprint, random plan ID and estimated bytes.
3. Owner manually reviews exact candidates. `POST
   /api/maintenance/disk/approve` with the `plan_id` and `fingerprint`
   issues a **single-use** token; approval expires after 10 minutes.
4. `POST /api/maintenance/disk/apply` with `plan_id` and
   `approval_token` uses server-resident candidate paths only. It checks
   machine policy and flag, verifies every candidate's content fingerprint
   and live process references again, and then removes only these directories.
   Approval is consumed on first attempt, even on validation failure.
5. Audit trail: `maintenance.disk.plan`, `.approve`, `.apply_attempt`,
   `.apply_blocked`, `.applied`, and `.denied`.

On feature deployment, inspect and enable the separate local machine policy
`maintenance: true` and `studio.disk_maintenance_enabled: true` **only with
owner authorization**. Keep `destructive: false` for unrestricted
shell/native tool calls. These two settings are intentionally not changed
by the feature itself.

## Safety limits

- Filesystem metadata is rechecked before deletion; stale plans fail closed.
- Source code runs only in an isolated feature worktree until reviewed.
- A maintenance plan is in-memory and becomes invalid after a service restart.
- `shutil.rmtree.avoids_symlink_attacks` must be true; traversal of linked
  directories is prohibited and fixed parents cannot be symlinks.
- Refuse to infer safety from `expired` PStack status alone. The tool does
  **not** delete HIRDA worktrees or production databases.
- CPU/disk pressure still requires investigation; this tool does not repair
  the `syslog` flood.
- Before live certification, add integration tests running under the
  production FastAPI environment and test the authenticated API workflow;
  do not enable `apply` based only on unit tests.


## OpenClaw systemd permission issue (HTTP 409 during Apply)

If an approved plan is rejected by `apply` with
`cleanup interrupted; inspect audited completed targets`, inspect
the **local maintenance audit** before submitting any fresh plan.
The original approval is single-use. Never replay its token.

The production unit uses `ProtectHome=read-only` and
`ProtectSystem=strict`. The local Codex release directories are not
part of its existing `ReadWritePaths`, so the service can scan them
but cannot remove them even with an owner-approved maintenance policy.

Owner operator only, from a privileged terminal:

```bash
cd /home/alfred/mcp-studio
sudo install -D -m 0644 systemd/mcp-studio-disk-maintenance.conf \
  /etc/systemd/system/mcp-studio.service.d/50-disk-maintenance.conf
sudo systemctl daemon-reload
sudo systemctl restart mcp-studio.service
systemctl is-active mcp-studio.service
systemctl show mcp-studio.service -p ProtectHome -p ProtectSystem -p ReadWritePaths
```

Verify that both explicit `/home/alfred/.codex/packages/*/releases` paths
are present in `ReadWritePaths`; `ProtectHome` must remain
`read-only`, `ProtectSystem` must remain `strict`. The HIRDA
machine's global `destructive` permission stays `false`.
A service restart is required for systemd to apply the new mount rules.

Before any retry, re-create a **fresh** plan via the existing owner
terminal client. Do not reuse a previous approval token. The local
owner reviews the new candidate manifest and explicitly confirms
the fingerprint.

To roll back **only this exception** (owner only):

```bash
sudo rm /etc/systemd/system/mcp-studio.service.d/50-disk-maintenance.conf
sudo systemctl daemon-reload
sudo systemctl restart mcp-studio.service
```

Do not use `systemctl revert` here; this machine already has other
unrelated service drop-ins that must be preserved.
