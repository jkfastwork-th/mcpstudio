from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, TypeVar

T = TypeVar("T")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _workspace_key(value: str) -> str:
    """Normalize workspace paths so aliases/trailing slashes share one lease key."""
    import os
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("workspace must not be empty")
    expanded = os.path.expanduser(raw)
    return os.path.abspath(os.path.normpath(expanded)) if os.path.isabs(expanded) else os.path.normpath(expanded)


class LeaseConflict(RuntimeError):
    def __init__(self, workspace: str, worker_id: str):
        self.workspace = workspace
        self.worker_id = worker_id
        super().__init__(f"workspace {workspace!r} already has a write lease held by {worker_id}")


class Database:
    def __init__(self, path: str):
        self.path = Path(path).expanduser().resolve()
        self._lock = asyncio.Lock()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    async def _run(self, fn: Callable[[], T]) -> T:
        async with self._lock:
            return await asyncio.to_thread(fn)

    async def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

        def op() -> None:
            with self._connect() as db:
                db.executescript(
                    """
                    PRAGMA journal_mode=WAL;
                    CREATE TABLE IF NOT EXISTS events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        created_at TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        severity TEXT NOT NULL,
                        server_id TEXT,
                        message TEXT NOT NULL,
                        data_json TEXT NOT NULL DEFAULT '{}'
                    );
                    CREATE TABLE IF NOT EXISTS sessions (
                        id TEXT PRIMARY KEY,
                        client_id TEXT NOT NULL,
                        client_type TEXT NOT NULL,
                        server_id TEXT NOT NULL,
                        workspace TEXT,
                        pane TEXT,
                        status TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        last_seen_at TEXT NOT NULL,
                        metadata_json TEXT NOT NULL DEFAULT '{}'
                    );
                    CREATE TABLE IF NOT EXISTS gateway_sessions (
                        id TEXT PRIMARY KEY,
                        studio_session_id TEXT NOT NULL,
                        client_id TEXT NOT NULL,
                        client_type TEXT NOT NULL,
                        server_id TEXT NOT NULL,
                        upstream_session_id TEXT,
                        status TEXT NOT NULL DEFAULT 'connected',
                        generation INTEGER NOT NULL DEFAULT 1,
                        reconnect_count INTEGER NOT NULL DEFAULT 0,
                        protocol_version TEXT,
                        init_payload_json TEXT NOT NULL DEFAULT '{}',
                        metadata_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL,
                        last_seen_at TEXT NOT NULL,
                        last_reconnected_at TEXT,
                        closed_at TEXT,
                        error TEXT,
                        initial_tunnel_id TEXT,
                        last_tunnel_id TEXT,
                        ingress_host TEXT,
                        ingress_path TEXT,
                        ingress_provider TEXT,
                        attribution_method TEXT,
                        attribution_confidence TEXT,
                        tunnel_switch_count INTEGER NOT NULL DEFAULT 0
                    );
                    CREATE TABLE IF NOT EXISTS managed_workspaces (
                        key TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        project_path TEXT NOT NULL UNIQUE,
                        enabled INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        metadata_json TEXT NOT NULL DEFAULT '{}'
                    );
                    CREATE TABLE IF NOT EXISTS managed_sessions (
                        id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        workspace_key TEXT NOT NULL,
                        project_path TEXT NOT NULL,
                        server_id TEXT NOT NULL,
                        port INTEGER NOT NULL UNIQUE,
                        endpoint TEXT,
                        pid INTEGER,
                        status TEXT NOT NULL DEFAULT 'stopped',
                        desired_state TEXT NOT NULL DEFAULT 'running',
                        generation INTEGER NOT NULL DEFAULT 0,
                        error TEXT,
                        log_path TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        last_started_at TEXT,
                        last_stopped_at TEXT,
                        metadata_json TEXT NOT NULL DEFAULT '{}',
                        FOREIGN KEY(workspace_key) REFERENCES managed_workspaces(key)
                    );
                    CREATE TABLE IF NOT EXISTS workers (
                        id TEXT PRIMARY KEY,
                        ordinal INTEGER NOT NULL UNIQUE,
                        server_id TEXT NOT NULL,
                        state TEXT NOT NULL DEFAULT 'idle',
                        workspace TEXT,
                        pane TEXT,
                        agent TEXT,
                        owner_session_id TEXT,
                        lease_mode TEXT NOT NULL DEFAULT 'none',
                        work_label TEXT,
                        enabled INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        last_heartbeat_at TEXT,
                        metadata_json TEXT NOT NULL DEFAULT '{}'
                    );
                    CREATE TABLE IF NOT EXISTS workspace_leases (
                        workspace TEXT PRIMARY KEY,
                        worker_id TEXT NOT NULL,
                        session_id TEXT,
                        work_id TEXT,
                        acquired_at TEXT NOT NULL,
                        refreshed_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS work_items (
                        id TEXT PRIMARY KEY,
                        label TEXT NOT NULL,
                        workspace TEXT NOT NULL,
                        priority INTEGER NOT NULL DEFAULT 50,
                        state TEXT NOT NULL DEFAULT 'queued',
                        lease_mode TEXT NOT NULL DEFAULT 'write',
                        session_id TEXT,
                        requested_pane TEXT,
                        requested_agent TEXT,
                        pane TEXT,
                        agent TEXT,
                        server_id TEXT NOT NULL,
                        worker_id TEXT,
                        created_at TEXT NOT NULL,
                        queued_at TEXT NOT NULL,
                        started_at TEXT,
                        finished_at TEXT,
                        updated_at TEXT NOT NULL,
                        error TEXT,
                        result_json TEXT NOT NULL DEFAULT '{}',
                        metadata_json TEXT NOT NULL DEFAULT '{}',
                        dispatch_mode TEXT NOT NULL DEFAULT 'manual',
                        instruction TEXT,
                        execution_state TEXT NOT NULL DEFAULT 'none',
                        dispatch_attempts INTEGER NOT NULL DEFAULT 0,
                        recovery_count INTEGER NOT NULL DEFAULT 0,
                        dispatched_at TEXT,
                        last_execution_poll_at TEXT,
                        last_agent_status TEXT,
                        failure_class TEXT,
                        next_retry_at TEXT,
                        execution_json TEXT NOT NULL DEFAULT '{}'
                    );
                    CREATE TABLE IF NOT EXISTS connectivity_tunnels (
                        id TEXT PRIMARY KEY,
                        provider TEXT NOT NULL,
                        name TEXT NOT NULL,
                        endpoint TEXT,
                        origin TEXT,
                        health_url TEXT,
                        enabled INTEGER NOT NULL DEFAULT 1,
                        managed INTEGER NOT NULL DEFAULT 0,
                        autostart INTEGER NOT NULL DEFAULT 0,
                        auto_reconnect INTEGER NOT NULL DEFAULT 1,
                        desired_state TEXT NOT NULL DEFAULT 'stopped',
                        status TEXT NOT NULL DEFAULT 'unknown',
                        pid INTEGER,
                        command_json TEXT NOT NULL DEFAULT '[]',
                        tunnel_name TEXT,
                        config_file TEXT,
                        executable TEXT NOT NULL DEFAULT 'cloudflared',
                        log_path TEXT,
                        last_checked_at TEXT,
                        last_healthy_at TEXT,
                        started_at TEXT,
                        stopped_at TEXT,
                        restart_count INTEGER NOT NULL DEFAULT 0,
                        error TEXT,
                        metadata_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS oauth_clients (
                        client_id TEXT PRIMARY KEY,
                        client_secret_hash TEXT,
                        client_name TEXT NOT NULL,
                        redirect_uris_json TEXT NOT NULL,
                        scopes_json TEXT NOT NULL,
                        token_endpoint_auth_method TEXT NOT NULL DEFAULT 'none',
                        enabled INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS oauth_authorization_codes (
                        code_hash TEXT PRIMARY KEY,
                        client_id TEXT NOT NULL,
                        redirect_uri TEXT NOT NULL,
                        scope TEXT NOT NULL,
                        code_challenge TEXT NOT NULL,
                        code_challenge_method TEXT NOT NULL,
                        resource TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        expires_at TEXT NOT NULL,
                        used_at TEXT
                    );
                    CREATE TABLE IF NOT EXISTS oauth_refresh_tokens (
                        token_hash TEXT PRIMARY KEY,
                        client_id TEXT NOT NULL,
                        scope TEXT NOT NULL,
                        resource TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        expires_at TEXT NOT NULL,
                        used_at TEXT,
                        revoked_at TEXT,
                        rotated_from TEXT
                    );
                    CREATE TABLE IF NOT EXISTS schema_migrations (
                        version INTEGER PRIMARY KEY,
                        name TEXT NOT NULL,
                        applied_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS audit_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        created_at TEXT NOT NULL,
                        actor TEXT NOT NULL,
                        action TEXT NOT NULL,
                        target_type TEXT,
                        target_id TEXT,
                        outcome TEXT NOT NULL,
                        request_id TEXT,
                        data_json TEXT NOT NULL DEFAULT '{}'
                    );
                    CREATE TABLE IF NOT EXISTS operational_alerts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        dedupe_key TEXT NOT NULL UNIQUE,
                        kind TEXT NOT NULL,
                        severity TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'open',
                        message TEXT NOT NULL,
                        first_seen_at TEXT NOT NULL,
                        last_seen_at TEXT NOT NULL,
                        occurrence_count INTEGER NOT NULL DEFAULT 1,
                        acknowledged_at TEXT,
                        acknowledged_by TEXT,
                        resolved_at TEXT,
                        data_json TEXT NOT NULL DEFAULT '{}'
                    );
                    CREATE TABLE IF NOT EXISTS mcp_request_metrics (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        created_at TEXT NOT NULL,
                        server_id TEXT NOT NULL,
                        http_method TEXT NOT NULL,
                        rpc_method TEXT,
                        status_code INTEGER NOT NULL,
                        latency_ms REAL NOT NULL,
                        outcome TEXT NOT NULL,
                        client_class TEXT,
                        gateway_session_id TEXT
                    );
                    CREATE TABLE IF NOT EXISTS observability_samples (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        created_at TEXT NOT NULL,
                        upstream_ok INTEGER NOT NULL,
                        upstream_status TEXT NOT NULL,
                        worker_busy INTEGER NOT NULL,
                        worker_total INTEGER NOT NULL,
                        queue_depth INTEGER NOT NULL,
                        active_work INTEGER NOT NULL,
                        open_alerts INTEGER NOT NULL,
                        data_json TEXT NOT NULL DEFAULT '{}'
                    );
                    CREATE INDEX IF NOT EXISTS idx_oauth_codes_client ON oauth_authorization_codes(client_id, expires_at);
                    CREATE INDEX IF NOT EXISTS idx_oauth_refresh_client ON oauth_refresh_tokens(client_id, expires_at);
                    CREATE INDEX IF NOT EXISTS idx_audit_created_at ON audit_log(created_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action, created_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_alerts_status ON operational_alerts(status, severity, last_seen_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_mcp_request_created ON mcp_request_metrics(created_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_mcp_request_server ON mcp_request_metrics(server_id, created_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_observability_created ON observability_samples(created_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_managed_workspaces_path ON managed_workspaces(project_path);
                    CREATE INDEX IF NOT EXISTS idx_managed_sessions_status ON managed_sessions(status, desired_state);
                    CREATE INDEX IF NOT EXISTS idx_managed_sessions_workspace ON managed_sessions(workspace_key, desired_state);
                    CREATE INDEX IF NOT EXISTS idx_tunnels_status ON connectivity_tunnels(status, enabled);
                    CREATE INDEX IF NOT EXISTS idx_tunnels_provider ON connectivity_tunnels(provider);
                    CREATE INDEX IF NOT EXISTS idx_events_created_at ON events(created_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);
                    CREATE INDEX IF NOT EXISTS idx_gateway_sessions_status ON gateway_sessions(status, last_seen_at);
                    CREATE INDEX IF NOT EXISTS idx_gateway_sessions_client ON gateway_sessions(client_id, client_type, server_id);
                    CREATE INDEX IF NOT EXISTS idx_gateway_sessions_studio ON gateway_sessions(studio_session_id);
                    CREATE INDEX IF NOT EXISTS idx_workers_state ON workers(state);
                    CREATE INDEX IF NOT EXISTS idx_workers_workspace ON workers(workspace);
                    CREATE INDEX IF NOT EXISTS idx_leases_worker ON workspace_leases(worker_id);
                    CREATE INDEX IF NOT EXISTS idx_work_state_priority ON work_items(state, priority DESC, queued_at ASC);
                    CREATE INDEX IF NOT EXISTS idx_work_workspace ON work_items(workspace, state);
                    CREATE INDEX IF NOT EXISTS idx_work_worker ON work_items(worker_id, state);
                    """
                )

                # Forward-only, idempotent M3 migration for databases created by
                # v0.1-v0.3. SQLite does not support ADD COLUMN IF NOT EXISTS on
                # all supported versions, so inspect the table first.
                existing = {row[1] for row in db.execute("PRAGMA table_info(work_items)").fetchall()}
                additions = {
                    "dispatch_mode": "TEXT NOT NULL DEFAULT 'manual'",
                    "instruction": "TEXT",
                    "execution_state": "TEXT NOT NULL DEFAULT 'none'",
                    "dispatch_attempts": "INTEGER NOT NULL DEFAULT 0",
                    "recovery_count": "INTEGER NOT NULL DEFAULT 0",
                    "dispatched_at": "TEXT",
                    "last_execution_poll_at": "TEXT",
                    "last_agent_status": "TEXT",
                    "failure_class": "TEXT",
                    "next_retry_at": "TEXT",
                    "execution_json": "TEXT NOT NULL DEFAULT '{}'",
                    "cancel_requested_at": "TEXT",
                    "detached_at": "TEXT",
                    "detached_reason": "TEXT",
                }
                for name, ddl in additions.items():
                    if name not in existing:
                        db.execute(f"ALTER TABLE work_items ADD COLUMN {name} {ddl}")
                db.execute(
                    "CREATE INDEX IF NOT EXISTS idx_work_execution_state ON work_items(execution_state, state)"
                )
                now = _now()
                db.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version, name, applied_at) VALUES(1, 'legacy-baseline-through-m6.0', ?)",
                    (now,),
                )
                db.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version, name, applied_at) VALUES(2, 'm6.1-operations-hardening', ?)",
                    (now,),
                )
                db.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version, name, applied_at) VALUES(3, 'm6.2-observability-slo', ?)",
                    (now,),
                )
                lease_columns = {row[1] for row in db.execute("PRAGMA table_info(workspace_leases)").fetchall()}
                if "work_id" not in lease_columns:
                    db.execute("ALTER TABLE workspace_leases ADD COLUMN work_id TEXT")
                db.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version, name, applied_at) VALUES(4, 'm6.2.1-lease-lifecycle-integrity', ?)",
                    (now,),
                )
                gateway_columns = {row[1] for row in db.execute("PRAGMA table_info(gateway_sessions)").fetchall()}
                gateway_additions = {
                    "initial_tunnel_id": "TEXT",
                    "last_tunnel_id": "TEXT",
                    "ingress_host": "TEXT",
                    "ingress_path": "TEXT",
                    "ingress_provider": "TEXT",
                    "attribution_method": "TEXT",
                    "attribution_confidence": "TEXT",
                    "tunnel_switch_count": "INTEGER NOT NULL DEFAULT 0",
                }
                for name, ddl in gateway_additions.items():
                    if name not in gateway_columns:
                        db.execute(f"ALTER TABLE gateway_sessions ADD COLUMN {name} {ddl}")
                db.execute("CREATE INDEX IF NOT EXISTS idx_gateway_sessions_tunnel ON gateway_sessions(last_tunnel_id, status, last_seen_at)")
                db.execute("CREATE INDEX IF NOT EXISTS idx_gateway_sessions_initial_tunnel ON gateway_sessions(initial_tunnel_id, created_at)")
                db.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version, name, applied_at) VALUES(5, 'm6.2.2-tunnel-aware-session-attribution', ?)",
                    (now,),
                )
                session_columns = {row[1] for row in db.execute("PRAGMA table_info(sessions)").fetchall()}
                if "managed_session_id" not in session_columns:
                    db.execute("ALTER TABLE sessions ADD COLUMN managed_session_id TEXT")
                gateway_columns = {row[1] for row in db.execute("PRAGMA table_info(gateway_sessions)").fetchall()}
                gateway_phase_b = {
                    "managed_session_id": "TEXT",
                    "upstream_url": "TEXT",
                }
                for name, ddl in gateway_phase_b.items():
                    if name not in gateway_columns:
                        db.execute(f"ALTER TABLE gateway_sessions ADD COLUMN {name} {ddl}")
                db.execute("CREATE INDEX IF NOT EXISTS idx_gateway_sessions_managed ON gateway_sessions(managed_session_id, status, last_seen_at)")
                db.execute("CREATE INDEX IF NOT EXISTS idx_sessions_managed ON sessions(managed_session_id, status, last_seen_at)")
                db.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version, name, applied_at) VALUES(6, 'm6.2.3b-managed-session-project-isolation', ?)",
                    (now,),
                )
                managed_columns = {row[1] for row in db.execute("PRAGMA table_info(managed_sessions)").fetchall()}
                managed_phase_624 = {
                    "last_used_at": "TEXT",
                    "use_count": "INTEGER NOT NULL DEFAULT 0",
                }
                for name, ddl in managed_phase_624.items():
                    if name not in managed_columns:
                        db.execute(f"ALTER TABLE managed_sessions ADD COLUMN {name} {ddl}")
                db.execute(
                    "CREATE INDEX IF NOT EXISTS idx_managed_sessions_last_used ON managed_sessions(last_used_at DESC)"
                )
                db.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version, name, applied_at) VALUES(7, 'm6.2.4-session-ux-lifecycle', ?)",
                    (now,),
                )

        await self._run(op)

    async def add_event(
        self,
        kind: str,
        message: str,
        *,
        severity: str = "info",
        server_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        def op() -> None:
            with self._connect() as db:
                db.execute(
                    "INSERT INTO events(created_at, kind, severity, server_id, message, data_json) VALUES(?,?,?,?,?,?)",
                    (_now(), kind, severity, server_id, message, json.dumps(data or {}, ensure_ascii=False)),
                )

        await self._run(op)

    async def recent_events(self, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 500))

        def op() -> list[dict[str, Any]]:
            with self._connect() as db:
                rows = db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
                return [{**dict(row), "data": json.loads(row["data_json"] or "{}")} for row in rows]

        return await self._run(op)

    async def add_audit(
        self, action: str, *, actor: str = "system", target_type: str | None = None,
        target_id: str | None = None, outcome: str = "success", request_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        def op() -> None:
            with self._connect() as db:
                db.execute(
                    """INSERT INTO audit_log(created_at, actor, action, target_type, target_id, outcome, request_id, data_json)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (_now(), actor, action, target_type, target_id, outcome, request_id,
                     json.dumps(data or {}, ensure_ascii=False)),
                )
        await self._run(op)

    async def list_audit(self, limit: int = 200, action: str | None = None) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 1000))
        def op() -> list[dict[str, Any]]:
            with self._connect() as db:
                if action:
                    rows = db.execute(
                        "SELECT * FROM audit_log WHERE action=? ORDER BY id DESC LIMIT ?", (action, limit)
                    ).fetchall()
                else:
                    rows = db.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
                out = []
                for row in rows:
                    item = dict(row)
                    item["data"] = json.loads(item.pop("data_json") or "{}")
                    out.append(item)
                return out
        return await self._run(op)

    async def open_alert(
        self, dedupe_key: str, kind: str, message: str, *, severity: str = "warning",
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = _now()
        def op() -> dict[str, Any]:
            with self._connect() as db:
                db.execute(
                    """INSERT INTO operational_alerts
                       (dedupe_key, kind, severity, status, message, first_seen_at, last_seen_at, occurrence_count, data_json)
                       VALUES(?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(dedupe_key) DO UPDATE SET
                         kind=excluded.kind, severity=excluded.severity, status='open', message=excluded.message,
                         last_seen_at=excluded.last_seen_at, occurrence_count=operational_alerts.occurrence_count+1,
                         acknowledged_at=NULL, acknowledged_by=NULL, resolved_at=NULL, data_json=excluded.data_json""",
                    (dedupe_key, kind, severity, "open", message, now, now, 1,
                     json.dumps(data or {}, ensure_ascii=False)),
                )
                row = db.execute("SELECT * FROM operational_alerts WHERE dedupe_key=?", (dedupe_key,)).fetchone()
                item = dict(row)
                item["data"] = json.loads(item.pop("data_json") or "{}")
                return item
        return await self._run(op)

    async def acknowledge_alert(self, alert_id: int, actor: str, note: str | None = None) -> dict[str, Any]:
        now = _now()
        def op() -> dict[str, Any]:
            with self._connect() as db:
                row = db.execute("SELECT * FROM operational_alerts WHERE id=?", (alert_id,)).fetchone()
                if row is None:
                    raise KeyError(alert_id)
                data = json.loads(row["data_json"] or "{}")
                if note:
                    data["ack_note"] = note
                db.execute(
                    """UPDATE operational_alerts SET status='acknowledged', acknowledged_at=?, acknowledged_by=?,
                       data_json=? WHERE id=?""",
                    (now, actor, json.dumps(data, ensure_ascii=False), alert_id),
                )
                updated = db.execute("SELECT * FROM operational_alerts WHERE id=?", (alert_id,)).fetchone()
                item = dict(updated)
                item["data"] = json.loads(item.pop("data_json") or "{}")
                return item
        return await self._run(op)

    async def resolve_alert(self, dedupe_key: str) -> bool:
        now = _now()
        def op() -> bool:
            with self._connect() as db:
                cur = db.execute(
                    "UPDATE operational_alerts SET status='resolved', resolved_at=? WHERE dedupe_key=? AND status!='resolved'",
                    (now, dedupe_key),
                )
                return cur.rowcount > 0
        return await self._run(op)

    async def list_alerts(self, status: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 1000))
        def op() -> list[dict[str, Any]]:
            with self._connect() as db:
                if status:
                    rows = db.execute(
                        "SELECT * FROM operational_alerts WHERE status=? ORDER BY id DESC LIMIT ?", (status, limit)
                    ).fetchall()
                else:
                    rows = db.execute("SELECT * FROM operational_alerts ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
                out=[]
                for row in rows:
                    item=dict(row); item["data"]=json.loads(item.pop("data_json") or "{}"); out.append(item)
                return out
        return await self._run(op)

    async def schema_status(self) -> dict[str, Any]:
        def op() -> dict[str, Any]:
            with self._connect() as db:
                rows = db.execute("SELECT version, name, applied_at FROM schema_migrations ORDER BY version").fetchall()
                integrity = db.execute("PRAGMA quick_check").fetchone()[0]
                return {
                    "current_version": int(rows[-1]["version"]) if rows else 0,
                    "expected_version": 7,
                    "integrity": integrity,
                    "migrations": [dict(r) for r in rows],
                }
        return await self._run(op)

    async def create_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        session_id = f"studio-{uuid.uuid4().hex[:16]}"
        now = _now()

        def op() -> None:
            with self._connect() as db:
                db.execute(
                    """INSERT INTO sessions
                    (id, client_id, client_type, server_id, workspace, pane, status, created_at, last_seen_at, metadata_json)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        session_id,
                        payload["client_id"],
                        payload.get("client_type", "chatgpt"),
                        payload["server_id"],
                        payload.get("workspace"),
                        payload.get("pane"),
                        "connected",
                        now,
                        now,
                        json.dumps(payload.get("metadata", {}), ensure_ascii=False),
                    ),
                )

        await self._run(op)
        return await self.get_session(session_id)

    async def get_session(self, session_id: str) -> dict[str, Any]:
        def op() -> dict[str, Any]:
            with self._connect() as db:
                row = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
                if row is None:
                    raise KeyError(session_id)
                item = dict(row)
                item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
                return item

        return await self._run(op)

    async def list_sessions(self, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 500))

        def op() -> list[dict[str, Any]]:
            with self._connect() as db:
                rows = db.execute("SELECT * FROM sessions ORDER BY last_seen_at DESC LIMIT ?", (limit,)).fetchall()
                out: list[dict[str, Any]] = []
                for row in rows:
                    item = dict(row)
                    item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
                    out.append(item)
                return out

        return await self._run(op)

    async def heartbeat_session(self, session_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        current = await self.get_session(session_id)
        metadata = current.get("metadata", {})
        if patch.get("metadata") is not None:
            metadata.update(patch["metadata"])
        workspace = patch.get("workspace") if patch.get("workspace") is not None else current.get("workspace")
        pane = patch.get("pane") if patch.get("pane") is not None else current.get("pane")

        def op() -> None:
            with self._connect() as db:
                cur = db.execute(
                    "UPDATE sessions SET workspace=?, pane=?, status='connected', last_seen_at=?, metadata_json=? WHERE id=?",
                    (workspace, pane, _now(), json.dumps(metadata, ensure_ascii=False), session_id),
                )
                if cur.rowcount == 0:
                    raise KeyError(session_id)

        await self._run(op)
        return await self.get_session(session_id)

    async def disconnect_session(self, session_id: str) -> dict[str, Any]:
        def op() -> None:
            with self._connect() as db:
                cur = db.execute(
                    "UPDATE sessions SET status='disconnected', last_seen_at=? WHERE id=?", (_now(), session_id)
                )
                if cur.rowcount == 0:
                    raise KeyError(session_id)

        await self._run(op)
        return await self.get_session(session_id)

    @staticmethod
    def _gateway_session_item(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["init_payload"] = json.loads(item.pop("init_payload_json") or "{}")
        item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
        return item

    async def create_gateway_session(
        self,
        *,
        studio_session_id: str,
        client_id: str,
        client_type: str,
        server_id: str,
        upstream_session_id: str | None,
        protocol_version: str | None,
        init_payload: dict[str, Any],
        metadata: dict[str, Any] | None = None,
        ingress: dict[str, Any] | None = None,
        managed_session_id: str | None = None,
        upstream_url: str | None = None,
    ) -> dict[str, Any]:
        gateway_id = f"gws-{uuid.uuid4().hex}"
        now = _now()

        def op() -> None:
            with self._connect() as db:
                db.execute(
                    """INSERT INTO gateway_sessions
                    (id, studio_session_id, client_id, client_type, server_id,
                     upstream_session_id, status, generation, reconnect_count,
                     protocol_version, init_payload_json, metadata_json,
                     created_at, last_seen_at, initial_tunnel_id, last_tunnel_id,
                     ingress_host, ingress_path, ingress_provider, attribution_method,
                     attribution_confidence, tunnel_switch_count, managed_session_id, upstream_url)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        gateway_id, studio_session_id, client_id, client_type, server_id,
                        upstream_session_id, "connected", 1, 0, protocol_version,
                        json.dumps(init_payload or {}, ensure_ascii=False),
                        json.dumps(metadata or {}, ensure_ascii=False), now, now,
                        (ingress or {}).get("tunnel_id"), (ingress or {}).get("tunnel_id"),
                        (ingress or {}).get("host"), (ingress or {}).get("path"),
                        (ingress or {}).get("provider"), (ingress or {}).get("method"),
                        (ingress or {}).get("confidence"), 0, managed_session_id, upstream_url,
                    ),
                )

        await self._run(op)
        return await self.get_gateway_session(gateway_id)

    async def get_gateway_session(self, gateway_id: str) -> dict[str, Any]:
        def op() -> dict[str, Any]:
            with self._connect() as db:
                row = db.execute("SELECT * FROM gateway_sessions WHERE id=?", (gateway_id,)).fetchone()
                if row is None:
                    raise KeyError(gateway_id)
                return self._gateway_session_item(row)
        return await self._run(op)

    async def list_gateway_sessions(self, limit: int = 200, tunnel_id: str | None = None) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 1000))
        def op() -> list[dict[str, Any]]:
            with self._connect() as db:
                if tunnel_id is None:
                    rows = db.execute(
                        "SELECT * FROM gateway_sessions ORDER BY last_seen_at DESC LIMIT ?", (limit,)
                    ).fetchall()
                elif tunnel_id == "__unattributed__":
                    rows = db.execute(
                        "SELECT * FROM gateway_sessions WHERE last_tunnel_id IS NULL ORDER BY last_seen_at DESC LIMIT ?", (limit,)
                    ).fetchall()
                else:
                    rows = db.execute(
                        "SELECT * FROM gateway_sessions WHERE last_tunnel_id=? ORDER BY last_seen_at DESC LIMIT ?",
                        (tunnel_id, limit),
                    ).fetchall()
                return [self._gateway_session_item(row) for row in rows]
        return await self._run(op)

    async def update_gateway_session_ingress(self, gateway_id: str, ingress: dict[str, Any]) -> dict[str, Any]:
        """Backfill/update observed ingress without treating attribution as a security boundary."""
        now = _now()
        def op() -> None:
            with self._connect() as db:
                row = db.execute("SELECT * FROM gateway_sessions WHERE id=?", (gateway_id,)).fetchone()
                if row is None:
                    raise KeyError(gateway_id)
                old = row["last_tunnel_id"]
                new = ingress.get("tunnel_id")
                switched = bool(old and new and old != new)
                initial = row["initial_tunnel_id"] or new
                db.execute(
                    """UPDATE gateway_sessions SET initial_tunnel_id=?, last_tunnel_id=?, ingress_host=?,
                       ingress_path=?, ingress_provider=?, attribution_method=?, attribution_confidence=?,
                       tunnel_switch_count=tunnel_switch_count+?, last_seen_at=? WHERE id=?""",
                    (initial, new, ingress.get("host"), ingress.get("path"), ingress.get("provider"),
                     ingress.get("method"), ingress.get("confidence"), int(switched), now, gateway_id),
                )
        await self._run(op)
        return await self.get_gateway_session(gateway_id)

    async def tunnel_session_summary(self) -> dict[str, Any]:
        def op() -> dict[str, Any]:
            with self._connect() as db:
                rows = db.execute(
                    """SELECT last_tunnel_id, status, COUNT(*) AS n, COALESCE(SUM(reconnect_count),0) AS reconnects
                       FROM gateway_sessions GROUP BY last_tunnel_id, status"""
                ).fetchall()
                by_tunnel: dict[str, Any] = {}
                for row in rows:
                    key = row["last_tunnel_id"] or "__unattributed__"
                    item = by_tunnel.setdefault(key, {"total": 0, "counts": {}, "reconnects": 0})
                    item["total"] += int(row["n"] or 0)
                    item["counts"][row["status"]] = int(row["n"] or 0)
                    item["reconnects"] += int(row["reconnects"] or 0)
                return {"by_tunnel": by_tunnel}
        return await self._run(op)

    async def touch_gateway_session(self, gateway_id: str, *, error: str | None = None) -> dict[str, Any]:
        now = _now()
        def op() -> None:
            with self._connect() as db:
                cur = db.execute(
                    "UPDATE gateway_sessions SET status='connected', last_seen_at=?, error=? WHERE id=?",
                    (now, error, gateway_id),
                )
                if cur.rowcount == 0:
                    raise KeyError(gateway_id)
        await self._run(op)
        return await self.get_gateway_session(gateway_id)

    async def reconnect_gateway_session(
        self,
        gateway_id: str,
        *,
        upstream_session_id: str | None,
        error: str | None = None,
    ) -> dict[str, Any]:
        now = _now()
        def op() -> None:
            with self._connect() as db:
                cur = db.execute(
                    """UPDATE gateway_sessions
                       SET upstream_session_id=?, status='connected', generation=generation+1,
                           reconnect_count=reconnect_count+1, last_seen_at=?,
                           last_reconnected_at=?, error=? WHERE id=?""",
                    (upstream_session_id, now, now, error, gateway_id),
                )
                if cur.rowcount == 0:
                    raise KeyError(gateway_id)
        await self._run(op)
        return await self.get_gateway_session(gateway_id)

    async def update_gateway_upstream_session(
        self, gateway_id: str, upstream_session_id: str | None
    ) -> dict[str, Any]:
        now = _now()
        def op() -> None:
            with self._connect() as db:
                cur = db.execute(
                    "UPDATE gateway_sessions SET upstream_session_id=?, last_seen_at=? WHERE id=?",
                    (upstream_session_id, now, gateway_id),
                )
                if cur.rowcount == 0:
                    raise KeyError(gateway_id)
        await self._run(op)
        return await self.get_gateway_session(gateway_id)

    async def close_gateway_session(self, gateway_id: str, *, error: str | None = None) -> dict[str, Any]:
        now = _now()
        def op() -> None:
            with self._connect() as db:
                cur = db.execute(
                    """UPDATE gateway_sessions SET status='closed', last_seen_at=?, closed_at=?, error=?
                       WHERE id=?""",
                    (now, now, error, gateway_id),
                )
                if cur.rowcount == 0:
                    raise KeyError(gateway_id)
        await self._run(op)
        return await self.get_gateway_session(gateway_id)

    async def mark_stale_gateway_sessions(self, stale_seconds: int) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=stale_seconds)).isoformat()
        def op() -> int:
            with self._connect() as db:
                cur = db.execute(
                    """UPDATE gateway_sessions SET status='stale'
                       WHERE status='connected' AND last_seen_at < ?""",
                    (cutoff,),
                )
                return int(cur.rowcount)
        return await self._run(op)

    async def gateway_session_summary(self) -> dict[str, Any]:
        def op() -> dict[str, Any]:
            with self._connect() as db:
                rows = db.execute(
                    "SELECT status, COUNT(*) n FROM gateway_sessions GROUP BY status"
                ).fetchall()
                counts = {"connected": 0, "stale": 0, "closed": 0}
                for row in rows:
                    counts[row["status"]] = row["n"]
                reconnects = db.execute(
                    "SELECT COALESCE(SUM(reconnect_count),0) n FROM gateway_sessions"
                ).fetchone()["n"]
                return {
                    "total": sum(counts.values()),
                    "counts": counts,
                    "reconnects": int(reconnects or 0),
                }
        return await self._run(op)

    async def ensure_workers(self, count: int, server_id: str) -> None:
        now = _now()

        def op() -> None:
            with self._connect() as db:
                for ordinal in range(1, count + 1):
                    worker_id = f"worker-{ordinal}"
                    db.execute(
                        """INSERT OR IGNORE INTO workers
                        (id, ordinal, server_id, state, lease_mode, enabled, created_at, updated_at, metadata_json)
                        VALUES(?,?,?,?,?,?,?,?,?)""",
                        (worker_id, ordinal, server_id, "idle", "none", 1, now, now, "{}"),
                    )
                    db.execute(
                        "UPDATE workers SET server_id=?, enabled=1, updated_at=? WHERE id=?",
                        (server_id, now, worker_id),
                    )
                db.execute("UPDATE workers SET enabled=0, updated_at=? WHERE ordinal > ?", (now, count))

        await self._run(op)

    @staticmethod
    def _worker_item(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["enabled"] = bool(item["enabled"])
        item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
        return item

    async def list_workers(self, enabled_only: bool = True) -> list[dict[str, Any]]:
        def op() -> list[dict[str, Any]]:
            with self._connect() as db:
                sql = "SELECT * FROM workers"
                if enabled_only:
                    sql += " WHERE enabled=1"
                sql += " ORDER BY ordinal"
                return [self._worker_item(row) for row in db.execute(sql).fetchall()]

        return await self._run(op)

    async def get_worker(self, worker_id: str) -> dict[str, Any]:
        def op() -> dict[str, Any]:
            with self._connect() as db:
                row = db.execute("SELECT * FROM workers WHERE id=? AND enabled=1", (worker_id,)).fetchone()
                if row is None:
                    raise KeyError(worker_id)
                return self._worker_item(row)

        return await self._run(op)

    async def bind_worker(self, worker_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        now = _now()

        def op() -> None:
            with self._connect() as db:
                row = db.execute("SELECT * FROM workers WHERE id=? AND enabled=1", (worker_id,)).fetchone()
                if row is None:
                    raise KeyError(worker_id)
                running = db.execute(
                    "SELECT id FROM work_items WHERE worker_id=? AND state='running' LIMIT 1", (worker_id,)
                ).fetchone()
                if row["state"] in {"busy", "degraded"} or running is not None:
                    raise ValueError(f"worker {worker_id} has active work and cannot be rebound; detach/finish the work first")
                workspace = _workspace_key(payload["workspace"])
                lease_mode = payload.get("lease_mode", "write")

                # M6.2.1: binding expresses workspace affinity / lease intent only.
                # An idle worker is not a writer and must never pin a workspace.
                # The write lease is acquired atomically when the worker becomes
                # busy (manual flow) or when the scheduler assigns real work.
                db.execute("DELETE FROM workspace_leases WHERE worker_id=?", (worker_id,))

                metadata = json.loads(row["metadata_json"] or "{}")
                metadata.update(payload.get("metadata") or {})
                db.execute(
                    """UPDATE workers SET workspace=?, pane=?, agent=?, owner_session_id=?, lease_mode=?,
                       state='idle', work_label=NULL, updated_at=?, last_heartbeat_at=?, metadata_json=? WHERE id=?""",
                    (
                        workspace,
                        payload.get("pane"),
                        payload.get("agent"),
                        payload.get("session_id"),
                        lease_mode,
                        now,
                        now,
                        json.dumps(metadata, ensure_ascii=False),
                        worker_id,
                    ),
                )

        await self._run(op)
        return await self.get_worker(worker_id)

    async def release_worker(self, worker_id: str) -> dict[str, Any]:
        now = _now()

        def op() -> None:
            with self._connect() as db:
                row = db.execute("SELECT id FROM workers WHERE id=? AND enabled=1", (worker_id,)).fetchone()
                if row is None:
                    raise KeyError(worker_id)
                running = db.execute(
                    "SELECT id FROM work_items WHERE worker_id=? AND state='running' LIMIT 1", (worker_id,)
                ).fetchone()
                if running is not None:
                    raise ValueError(f"worker {worker_id} owns running work {running['id']}; detach/finish the work before release")
                db.execute("DELETE FROM workspace_leases WHERE worker_id=?", (worker_id,))
                db.execute(
                    """UPDATE workers SET state='idle', workspace=NULL, pane=NULL, agent=NULL,
                    owner_session_id=NULL, lease_mode='none', work_label=NULL, updated_at=?,
                    last_heartbeat_at=NULL, metadata_json='{}' WHERE id=?""",
                    (now, worker_id),
                )

        await self._run(op)
        return await self.get_worker(worker_id)

    async def heartbeat_worker(self, worker_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        now = _now()

        def op() -> None:
            with self._connect() as db:
                row = db.execute("SELECT * FROM workers WHERE id=? AND enabled=1", (worker_id,)).fetchone()
                if row is None:
                    raise KeyError(worker_id)
                metadata = json.loads(row["metadata_json"] or "{}")
                if patch.get("metadata") is not None:
                    metadata.update(patch["metadata"])
                state = patch.get("state") or row["state"]
                if state == "busy" and not row["workspace"]:
                    raise ValueError("worker must be bound to a workspace before entering busy state")
                pane = patch.get("pane") if patch.get("pane") is not None else row["pane"]
                agent = patch.get("agent") if patch.get("agent") is not None else row["agent"]
                work_label = patch.get("work_label") if patch.get("work_label") is not None else row["work_label"]
                running = db.execute(
                    "SELECT id FROM work_items WHERE worker_id=? AND state='running' LIMIT 1", (worker_id,)
                ).fetchone()
                if state in {"idle", "dead"} and running is not None:
                    raise ValueError(f"worker {worker_id} owns running work {running['id']}; detach/finish the work before {state}")
                lease_mode = row["lease_mode"]
                if state in {"idle", "dead"}:
                    db.execute("DELETE FROM workspace_leases WHERE worker_id=?", (worker_id,))
                    if state == "dead":
                        lease_mode = "none"
                elif state in {"busy", "degraded"} and row["workspace"] and row["lease_mode"] == "write":
                    lease = db.execute("SELECT worker_id FROM workspace_leases WHERE workspace=?", (row["workspace"],)).fetchone()
                    if lease is not None and lease["worker_id"] != worker_id:
                        raise LeaseConflict(row["workspace"], lease["worker_id"])
                    db.execute(
                        """INSERT INTO workspace_leases(workspace, worker_id, session_id, work_id, acquired_at, refreshed_at)
                           VALUES(?,?,?,?,?,?)
                           ON CONFLICT(workspace) DO UPDATE SET worker_id=excluded.worker_id,
                             session_id=excluded.session_id, work_id=excluded.work_id, refreshed_at=excluded.refreshed_at""",
                        (row["workspace"], worker_id, row["owner_session_id"], None, now, now),
                    )
                db.execute(
                    """UPDATE workers SET state=?, pane=?, agent=?, lease_mode=?, work_label=?, updated_at=?,
                    last_heartbeat_at=?, metadata_json=? WHERE id=?""",
                    (state, pane, agent, lease_mode, work_label, now, now, json.dumps(metadata, ensure_ascii=False), worker_id),
                )

        await self._run(op)
        return await self.get_worker(worker_id)

    async def set_worker_state(self, worker_id: str, state: str, work_label: str | None = None) -> dict[str, Any]:
        now = _now()

        def op() -> None:
            with self._connect() as db:
                row = db.execute("SELECT * FROM workers WHERE id=? AND enabled=1", (worker_id,)).fetchone()
                if row is None:
                    raise KeyError(worker_id)
                if state == "busy" and not row["workspace"]:
                    raise ValueError("worker must be bound to a workspace before entering busy state")
                running = db.execute(
                    "SELECT id FROM work_items WHERE worker_id=? AND state='running' LIMIT 1", (worker_id,)
                ).fetchone()
                if state in {"idle", "dead"} and running is not None:
                    raise ValueError(f"worker {worker_id} owns running work {running['id']}; detach/finish the work before {state}")
                if state == "busy" and row["lease_mode"] == "write":
                    lease = db.execute("SELECT worker_id FROM workspace_leases WHERE workspace=?", (row["workspace"],)).fetchone()
                    if lease is not None and lease["worker_id"] != worker_id:
                        raise LeaseConflict(row["workspace"], lease["worker_id"])
                    db.execute(
                        """INSERT INTO workspace_leases(workspace, worker_id, session_id, work_id, acquired_at, refreshed_at)
                           VALUES(?,?,?,?,?,?)
                           ON CONFLICT(workspace) DO UPDATE SET worker_id=excluded.worker_id,
                             session_id=excluded.session_id, work_id=excluded.work_id, refreshed_at=excluded.refreshed_at""",
                        (row["workspace"], worker_id, row["owner_session_id"], None, now, now),
                    )
                elif state in {"idle", "dead"}:
                    db.execute("DELETE FROM workspace_leases WHERE worker_id=?", (worker_id,))
                if state == "dead":
                    db.execute(
                        "UPDATE workers SET state=?, lease_mode='none', work_label=?, updated_at=?, last_heartbeat_at=? WHERE id=?",
                        (state, work_label, now, now, worker_id),
                    )
                else:
                    db.execute(
                        "UPDATE workers SET state=?, work_label=?, updated_at=?, last_heartbeat_at=? WHERE id=?",
                        (state, work_label, now, now, worker_id),
                    )

        await self._run(op)
        return await self.get_worker(worker_id)

    async def list_workspace_leases(self) -> list[dict[str, Any]]:
        def op() -> list[dict[str, Any]]:
            with self._connect() as db:
                rows = db.execute("SELECT * FROM workspace_leases ORDER BY workspace").fetchall()
                return [dict(row) for row in rows]

        return await self._run(op)

    async def mark_stale_busy_workers(self, timeout_seconds: int) -> list[dict[str, Any]]:
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=timeout_seconds)).isoformat()
        now = _now()

        def op() -> list[dict[str, Any]]:
            with self._connect() as db:
                rows = db.execute(
                    """SELECT * FROM workers WHERE enabled=1 AND state='busy'
                    AND (last_heartbeat_at IS NULL OR last_heartbeat_at < ?)""",
                    (cutoff,),
                ).fetchall()
                stale = [self._worker_item(row) for row in rows]
                for worker in stale:
                    db.execute("DELETE FROM workspace_leases WHERE worker_id=?", (worker["id"],))
                    db.execute(
                        """UPDATE workers SET state='dead', lease_mode='none', updated_at=? WHERE id=?""",
                        (now, worker["id"]),
                    )
                return stale

        return await self._run(op)


    @staticmethod
    def _work_item(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
        item["result"] = json.loads(item.pop("result_json") or "{}")
        execution_raw = item.pop("execution_json", "{}")
        item["execution"] = json.loads(execution_raw or "{}")
        return item

    async def create_work(self, payload: dict[str, Any], default_server_id: str) -> dict[str, Any]:
        work_id = f"work-{uuid.uuid4().hex[:16]}"
        now = _now()
        workspace = _workspace_key(payload["workspace"])

        def op() -> None:
            with self._connect() as db:
                db.execute(
                    """INSERT INTO work_items
                    (id, label, workspace, priority, state, lease_mode, session_id,
                     requested_pane, requested_agent, server_id, created_at, queued_at,
                     updated_at, result_json, metadata_json, dispatch_mode, instruction,
                     execution_state, execution_json)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        work_id,
                        payload["label"].strip(),
                        workspace,
                        int(payload.get("priority", 50)),
                        "queued",
                        payload.get("lease_mode", "write"),
                        payload.get("session_id"),
                        payload.get("pane"),
                        payload.get("agent"),
                        payload.get("server_id") or default_server_id,
                        now,
                        now,
                        now,
                        "{}",
                        json.dumps(payload.get("metadata") or {}, ensure_ascii=False),
                        payload.get("dispatch_mode", "manual"),
                        payload.get("instruction"),
                        "none",
                        "{}",
                    ),
                )

        await self._run(op)
        return await self.get_work(work_id)

    async def get_work(self, work_id: str) -> dict[str, Any]:
        def op() -> dict[str, Any]:
            with self._connect() as db:
                row = db.execute("SELECT * FROM work_items WHERE id=?", (work_id,)).fetchone()
                if row is None:
                    raise KeyError(work_id)
                return self._work_item(row)

        return await self._run(op)

    async def list_work(self, *, state: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 1000))

        def op() -> list[dict[str, Any]]:
            with self._connect() as db:
                if state:
                    rows = db.execute(
                        """SELECT * FROM work_items WHERE state=?
                        ORDER BY CASE WHEN state='queued' THEN priority END DESC,
                                 CASE WHEN state='queued' THEN queued_at END ASC,
                                 created_at DESC LIMIT ?""",
                        (state, limit),
                    ).fetchall()
                else:
                    rows = db.execute(
                        """SELECT * FROM work_items
                        ORDER BY CASE WHEN state='running' THEN 0 WHEN state='queued' THEN 1 ELSE 2 END,
                                 CASE WHEN state='queued' THEN priority END DESC,
                                 CASE WHEN state='queued' THEN queued_at END ASC,
                                 created_at DESC LIMIT ?""",
                        (limit,),
                    ).fetchall()
                return [self._work_item(row) for row in rows]

        return await self._run(op)

    async def work_summary(self) -> dict[str, Any]:
        def op() -> dict[str, Any]:
            with self._connect() as db:
                rows = db.execute("SELECT state, COUNT(*) AS n FROM work_items GROUP BY state").fetchall()
                counts = {state: 0 for state in ("queued", "running", "completed", "failed", "cancelled", "detached")}
                for row in rows:
                    counts[row["state"]] = row["n"]
                queued = db.execute(
                    "SELECT MIN(queued_at) AS oldest FROM work_items WHERE state='queued'"
                ).fetchone()
                return {
                    "counts": counts,
                    "active": counts["running"],
                    "queue": counts["queued"],
                    "oldest_queued_at": queued["oldest"] if queued else None,
                }

        return await self._run(op)

    async def queued_work(self, limit: int = 1000) -> list[dict[str, Any]]:
        return await self.list_work(state="queued", limit=limit)

    async def idle_unbound_workers(self) -> list[dict[str, Any]]:
        def op() -> list[dict[str, Any]]:
            with self._connect() as db:
                rows = db.execute(
                    """SELECT * FROM workers
                    WHERE enabled=1 AND state='idle' AND workspace IS NULL
                    ORDER BY ordinal"""
                ).fetchall()
                return [self._worker_item(row) for row in rows]

        return await self._run(op)

    async def assign_work(
        self,
        work_id: str,
        worker_id: str,
        *,
        pane: str | None = None,
        agent: str | None = None,
    ) -> dict[str, Any]:
        now = _now()

        def op() -> None:
            with self._connect() as db:
                work = db.execute("SELECT * FROM work_items WHERE id=?", (work_id,)).fetchone()
                if work is None:
                    raise KeyError(work_id)
                if work["state"] != "queued":
                    raise ValueError(f"work {work_id} is not queued")
                worker = db.execute(
                    "SELECT * FROM workers WHERE id=? AND enabled=1", (worker_id,)
                ).fetchone()
                if worker is None:
                    raise KeyError(worker_id)
                if worker["state"] != "idle" or worker["workspace"] is not None:
                    raise ValueError(f"worker {worker_id} is not idle/unbound")

                workspace = work["workspace"]
                lease_mode = work["lease_mode"]
                if lease_mode == "write":
                    lease = db.execute(
                        "SELECT worker_id FROM workspace_leases WHERE workspace=?", (workspace,)
                    ).fetchone()
                    if lease is not None and lease["worker_id"] != worker_id:
                        raise LeaseConflict(workspace, lease["worker_id"])
                    db.execute(
                        """INSERT INTO workspace_leases(workspace, worker_id, session_id, work_id, acquired_at, refreshed_at)
                        VALUES(?,?,?,?,?,?)
                        ON CONFLICT(workspace) DO UPDATE SET
                          worker_id=excluded.worker_id,
                          session_id=excluded.session_id,
                          work_id=excluded.work_id,
                          refreshed_at=excluded.refreshed_at""",
                        (workspace, worker_id, work["session_id"], work_id, now, now),
                    )

                worker_meta = json.loads(worker["metadata_json"] or "{}")
                worker_meta.update({"managed_by": "scheduler", "work_id": work_id})
                selected_pane = work["requested_pane"] or pane
                selected_agent = work["requested_agent"] or agent
                db.execute(
                    """UPDATE workers SET state='busy', workspace=?, pane=?, agent=?, owner_session_id=?,
                       lease_mode=?, work_label=?, updated_at=?, last_heartbeat_at=?, metadata_json=? WHERE id=?""",
                    (
                        workspace,
                        selected_pane,
                        selected_agent,
                        work["session_id"],
                        lease_mode,
                        work["label"],
                        now,
                        now,
                        json.dumps(worker_meta, ensure_ascii=False),
                        worker_id,
                    ),
                )
                execution_state = "assigned" if work["dispatch_mode"] == "herdr" else "none"
                db.execute(
                    """UPDATE work_items SET state='running', worker_id=?, pane=?, agent=?,
                       started_at=?, updated_at=?, execution_state=? WHERE id=?""",
                    (worker_id, selected_pane, selected_agent, now, now, execution_state, work_id),
                )

        await self._run(op)
        return await self.get_work(work_id)

    async def heartbeat_running_work(self) -> None:
        now = _now()

        def op() -> None:
            with self._connect() as db:
                rows = db.execute(
                    """SELECT w.id AS work_id, w.workspace, w.worker_id, wk.lease_mode
                    FROM work_items w JOIN workers wk ON wk.id=w.worker_id
                    WHERE w.state='running' AND wk.enabled=1 AND wk.state='busy'"""
                ).fetchall()
                for row in rows:
                    db.execute(
                        "UPDATE workers SET last_heartbeat_at=?, updated_at=? WHERE id=?",
                        (now, now, row["worker_id"]),
                    )
                    if row["lease_mode"] == "write":
                        db.execute(
                            "UPDATE workspace_leases SET refreshed_at=? WHERE workspace=? AND worker_id=?",
                            (now, row["workspace"], row["worker_id"]),
                        )

        await self._run(op)

    async def finish_work(
        self,
        work_id: str,
        *,
        state: str,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        if state not in {"completed", "failed", "cancelled"}:
            raise ValueError("terminal state required")
        now = _now()

        def op() -> None:
            with self._connect() as db:
                work = db.execute("SELECT * FROM work_items WHERE id=?", (work_id,)).fetchone()
                if work is None:
                    raise KeyError(work_id)
                if work["state"] in {"completed", "failed", "cancelled", "detached"}:
                    raise ValueError(f"work {work_id} is already {work['state']}")
                if state in {"completed", "failed"} and work["state"] != "running":
                    raise ValueError(f"work {work_id} must be running before it can become {state}")
                worker_id = work["worker_id"]
                if worker_id:
                    db.execute("DELETE FROM workspace_leases WHERE worker_id=?", (worker_id,))
                    db.execute(
                        """UPDATE workers SET state='idle', workspace=NULL, pane=NULL, agent=NULL,
                        owner_session_id=NULL, lease_mode='none', work_label=NULL, updated_at=?,
                        last_heartbeat_at=NULL, metadata_json='{}' WHERE id=?""",
                        (now, worker_id),
                    )
                execution_state = {"completed": "completed", "failed": "failed", "cancelled": "cancelled"}[state]
                db.execute(
                    """UPDATE work_items SET state=?, finished_at=?, updated_at=?, error=?,
                       result_json=?, execution_state=?, last_execution_poll_at=? WHERE id=?""",
                    (state, now, now, error, json.dumps(result or {}, ensure_ascii=False),
                     execution_state, now, work_id),
                )

        await self._run(op)
        return await self.get_work(work_id)

    async def running_work(self, limit: int = 1000) -> list[dict[str, Any]]:
        return await self.list_work(state="running", limit=limit)

    async def update_work_execution(
        self,
        work_id: str,
        *,
        execution_state: str | None = None,
        pane: str | None = None,
        agent: str | None = None,
        last_agent_status: str | None = None,
        failure_class: str | None = None,
        execution_patch: dict[str, Any] | None = None,
        increment_dispatch: bool = False,
        increment_recovery: bool = False,
        mark_dispatched: bool = False,
    ) -> dict[str, Any]:
        now = _now()

        def op() -> None:
            with self._connect() as db:
                row = db.execute("SELECT * FROM work_items WHERE id=?", (work_id,)).fetchone()
                if row is None:
                    raise KeyError(work_id)
                if row["state"] != "running":
                    raise ValueError(f"work {work_id} is not running")
                execution = json.loads(row["execution_json"] or "{}")
                if execution_patch:
                    execution.update(execution_patch)
                next_state = execution_state if execution_state is not None else row["execution_state"]
                next_pane = pane if pane is not None else row["pane"]
                next_agent = agent if agent is not None else row["agent"]
                dispatch_attempts = int(row["dispatch_attempts"] or 0) + (1 if increment_dispatch else 0)
                recovery_count = int(row["recovery_count"] or 0) + (1 if increment_recovery else 0)
                dispatched_at = now if mark_dispatched else row["dispatched_at"]
                db.execute(
                    """UPDATE work_items SET execution_state=?, pane=?, agent=?,
                       dispatch_attempts=?, recovery_count=?, dispatched_at=?,
                       last_execution_poll_at=?, last_agent_status=?, failure_class=?,
                       execution_json=?, updated_at=? WHERE id=?""",
                    (next_state, next_pane, next_agent, dispatch_attempts, recovery_count,
                     dispatched_at, now, last_agent_status, failure_class,
                     json.dumps(execution, ensure_ascii=False), now, work_id),
                )
                if row["worker_id"]:
                    db.execute(
                        """UPDATE workers SET pane=?, agent=?, state='busy',
                           last_heartbeat_at=?, updated_at=? WHERE id=?""",
                        (next_pane, next_agent, now, now, row["worker_id"]),
                    )
                    if row["lease_mode"] == "write":
                        db.execute(
                            "UPDATE workspace_leases SET refreshed_at=? WHERE workspace=? AND worker_id=?",
                            (now, row["workspace"], row["worker_id"]),
                        )

        await self._run(op)
        return await self.get_work(work_id)

    async def mark_worker_degraded_for_work(self, work_id: str) -> None:
        now = _now()

        def op() -> None:
            with self._connect() as db:
                row = db.execute("SELECT worker_id FROM work_items WHERE id=?", (work_id,)).fetchone()
                if row is None:
                    raise KeyError(work_id)
                if row["worker_id"]:
                    db.execute(
                        "UPDATE workers SET state='degraded', updated_at=?, last_heartbeat_at=? WHERE id=?",
                        (now, now, row["worker_id"]),
                    )

        await self._run(op)

    @staticmethod
    def _tunnel_item(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["enabled"] = bool(item["enabled"])
        item["managed"] = bool(item["managed"])
        item["autostart"] = bool(item["autostart"])
        item["auto_reconnect"] = bool(item["auto_reconnect"])
        item["command"] = json.loads(item.pop("command_json") or "[]")
        item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
        return item

    async def sync_tunnels(self, configs: list[Any]) -> None:
        """Upsert config-defined tunnel inventory without clobbering runtime state."""
        now = _now()

        def op() -> None:
            with self._connect() as db:
                for cfg in configs:
                    provider = str(cfg.provider).lower()
                    existing = db.execute(
                        "SELECT id, desired_state FROM connectivity_tunnels WHERE id=?", (cfg.id,)
                    ).fetchone()
                    desired = (
                        existing["desired_state"]
                        if existing is not None
                        else ("running" if (provider in {"local", "direct", "openai", "external"} or cfg.autostart) else "stopped")
                    )
                    db.execute(
                        """INSERT INTO connectivity_tunnels
                        (id, provider, name, endpoint, origin, health_url, enabled, managed,
                         autostart, auto_reconnect, desired_state, tunnel_name, config_file,
                         executable, metadata_json, created_at, updated_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(id) DO UPDATE SET
                          provider=excluded.provider, name=excluded.name,
                          endpoint=COALESCE(excluded.endpoint, connectivity_tunnels.endpoint),
                          origin=excluded.origin, health_url=excluded.health_url,
                          enabled=excluded.enabled, managed=excluded.managed,
                          autostart=excluded.autostart, auto_reconnect=excluded.auto_reconnect,
                          tunnel_name=excluded.tunnel_name, config_file=excluded.config_file,
                          executable=excluded.executable, metadata_json=excluded.metadata_json,
                          updated_at=excluded.updated_at""",
                        (
                            cfg.id, provider, cfg.name, cfg.endpoint, cfg.origin, cfg.health_url,
                            int(cfg.enabled), int(cfg.managed), int(cfg.autostart), int(cfg.auto_reconnect),
                            desired, cfg.tunnel_name, cfg.config_file, cfg.executable,
                            json.dumps(cfg.metadata or {}, ensure_ascii=False), now, now,
                        ),
                    )

        await self._run(op)

    async def upsert_tunnel(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = _now()
        tunnel_id = payload["id"]

        def op() -> None:
            with self._connect() as db:
                existing = db.execute("SELECT * FROM connectivity_tunnels WHERE id=?", (tunnel_id,)).fetchone()
                created = existing["created_at"] if existing else now
                desired = payload.get("desired_state")
                if desired is None:
                    if existing:
                        desired = existing["desired_state"]
                    else:
                        provider = str(payload.get("provider", "local")).lower()
                        desired = "running" if provider in {"local", "direct", "openai", "external"} else "stopped"
                db.execute(
                    """INSERT INTO connectivity_tunnels
                    (id, provider, name, endpoint, origin, health_url, enabled, managed,
                     autostart, auto_reconnect, desired_state, tunnel_name, config_file,
                     executable, metadata_json, created_at, updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET
                      provider=excluded.provider, name=excluded.name, endpoint=excluded.endpoint,
                      origin=excluded.origin, health_url=excluded.health_url, enabled=excluded.enabled,
                      managed=excluded.managed, autostart=excluded.autostart,
                      auto_reconnect=excluded.auto_reconnect, desired_state=excluded.desired_state,
                      tunnel_name=excluded.tunnel_name, config_file=excluded.config_file,
                      executable=excluded.executable, metadata_json=excluded.metadata_json,
                      updated_at=excluded.updated_at""",
                    (
                        tunnel_id, str(payload.get("provider", "local")).lower(), payload["name"],
                        payload.get("endpoint"), payload.get("origin"), payload.get("health_url"),
                        int(payload.get("enabled", True)), int(payload.get("managed", False)),
                        int(payload.get("autostart", False)), int(payload.get("auto_reconnect", True)),
                        desired, payload.get("tunnel_name"), payload.get("config_file"),
                        payload.get("executable") or "cloudflared",
                        json.dumps(payload.get("metadata") or {}, ensure_ascii=False), created, now,
                    ),
                )

        await self._run(op)
        return await self.get_tunnel(tunnel_id)

    async def get_tunnel(self, tunnel_id: str) -> dict[str, Any]:
        def op() -> dict[str, Any]:
            with self._connect() as db:
                row = db.execute("SELECT * FROM connectivity_tunnels WHERE id=?", (tunnel_id,)).fetchone()
                if row is None:
                    raise KeyError(tunnel_id)
                return self._tunnel_item(row)
        return await self._run(op)

    async def list_tunnels(self) -> list[dict[str, Any]]:
        def op() -> list[dict[str, Any]]:
            with self._connect() as db:
                rows = db.execute("SELECT * FROM connectivity_tunnels ORDER BY id").fetchall()
                return [self._tunnel_item(row) for row in rows]
        return await self._run(op)

    async def update_tunnel_runtime(self, tunnel_id: str, **patch: Any) -> dict[str, Any]:
        allowed = {
            "endpoint", "desired_state", "status", "pid", "log_path", "last_checked_at",
            "last_healthy_at", "started_at", "stopped_at", "error",
        }
        now = _now()

        def op() -> None:
            with self._connect() as db:
                row = db.execute("SELECT * FROM connectivity_tunnels WHERE id=?", (tunnel_id,)).fetchone()
                if row is None:
                    raise KeyError(tunnel_id)
                fields: list[str] = []
                values: list[Any] = []
                for key in allowed:
                    if key in patch:
                        fields.append(f"{key}=?")
                        values.append(patch[key])
                if "command" in patch:
                    fields.append("command_json=?")
                    values.append(json.dumps(patch["command"] or [], ensure_ascii=False))
                if patch.get("increment_restart"):
                    fields.append("restart_count=restart_count+1")
                fields.append("updated_at=?")
                values.append(now)
                values.append(tunnel_id)
                db.execute(f"UPDATE connectivity_tunnels SET {', '.join(fields)} WHERE id=?", values)

        await self._run(op)
        return await self.get_tunnel(tunnel_id)

    async def delete_tunnel(self, tunnel_id: str) -> None:
        def op() -> None:
            with self._connect() as db:
                cur = db.execute("DELETE FROM connectivity_tunnels WHERE id=?", (tunnel_id,))
                if cur.rowcount == 0:
                    raise KeyError(tunnel_id)
        await self._run(op)

    async def mark_stale_sessions(self, stale_seconds: int) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=stale_seconds)).isoformat()

        def op() -> int:
            with self._connect() as db:
                cur = db.execute(
                    "UPDATE sessions SET status='stale' WHERE status='connected' AND last_seen_at < ?",
                    (cutoff,),
                )
                return int(cur.rowcount)
        return await self._run(op)

    async def reclaim_session(self, payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """Reconnect a logical Studio session by stable client identity."""
        now = _now()
        client_id = payload["client_id"]
        client_type = payload.get("client_type", "chatgpt")
        server_id = payload["server_id"]

        def op() -> tuple[str | None, bool]:
            with self._connect() as db:
                row = db.execute(
                    """SELECT * FROM sessions
                       WHERE client_id=? AND client_type=? AND server_id=?
                       ORDER BY last_seen_at DESC LIMIT 1""",
                    (client_id, client_type, server_id),
                ).fetchone()
                if row is None:
                    return None, False
                metadata = json.loads(row["metadata_json"] or "{}")
                metadata.update(payload.get("metadata") or {})
                metadata["reclaim_count"] = int(metadata.get("reclaim_count") or 0) + 1
                metadata["last_reclaimed_at"] = now
                workspace = payload.get("workspace") if payload.get("workspace") is not None else row["workspace"]
                pane = payload.get("pane") if payload.get("pane") is not None else row["pane"]
                db.execute(
                    """UPDATE sessions SET workspace=?, pane=?, status='connected', last_seen_at=?,
                       metadata_json=? WHERE id=?""",
                    (workspace, pane, now, json.dumps(metadata, ensure_ascii=False), row["id"]),
                )
                return row["id"], True

        session_id, reclaimed = await self._run(op)
        if session_id is None:
            return await self.create_session(payload), False
        return await self.get_session(session_id), reclaimed

    async def session_summary(self) -> dict[str, Any]:
        def op() -> dict[str, Any]:
            with self._connect() as db:
                rows = db.execute("SELECT status, COUNT(*) n FROM sessions GROUP BY status").fetchall()
                counts = {"connected": 0, "stale": 0, "disconnected": 0}
                for row in rows:
                    counts[row["status"]] = row["n"]
                return {"total": sum(counts.values()), "counts": counts}
        return await self._run(op)

    @staticmethod
    def _oauth_client_item(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["redirect_uris"] = json.loads(item.pop("redirect_uris_json") or "[]")
        item["scopes"] = json.loads(item.pop("scopes_json") or "[]")
        item["enabled"] = bool(item["enabled"])
        return item

    async def create_oauth_client(
        self, *, client_id: str, client_secret_hash: str | None, client_name: str,
        redirect_uris: list[str], scopes: list[str], token_endpoint_auth_method: str
    ) -> dict[str, Any]:
        now = _now()
        def op() -> None:
            with self._connect() as db:
                db.execute(
                    """INSERT INTO oauth_clients
                    (client_id, client_secret_hash, client_name, redirect_uris_json, scopes_json,
                     token_endpoint_auth_method, enabled, created_at, updated_at)
                    VALUES(?,?,?,?,?,?,1,?,?)""",
                    (client_id, client_secret_hash, client_name, json.dumps(redirect_uris),
                     json.dumps(scopes), token_endpoint_auth_method, now, now),
                )
        await self._run(op)
        return await self.get_oauth_client(client_id)

    async def get_oauth_client(self, client_id: str) -> dict[str, Any]:
        def op() -> dict[str, Any]:
            with self._connect() as db:
                row = db.execute("SELECT * FROM oauth_clients WHERE client_id=?", (client_id,)).fetchone()
                if row is None:
                    raise KeyError(client_id)
                return self._oauth_client_item(row)
        return await self._run(op)

    async def list_oauth_clients(self) -> list[dict[str, Any]]:
        def op() -> list[dict[str, Any]]:
            with self._connect() as db:
                rows = db.execute("SELECT * FROM oauth_clients ORDER BY created_at DESC").fetchall()
                return [self._oauth_client_item(row) for row in rows]
        return await self._run(op)

    async def create_oauth_authorization_code(
        self, *, code_hash: str, client_id: str, redirect_uri: str, scope: str,
        code_challenge: str, code_challenge_method: str, resource: str, lifetime_seconds: int
    ) -> None:
        now = datetime.now(timezone.utc)
        expires = (now + timedelta(seconds=lifetime_seconds)).isoformat()
        def op() -> None:
            with self._connect() as db:
                db.execute(
                    """INSERT INTO oauth_authorization_codes
                    (code_hash, client_id, redirect_uri, scope, code_challenge, code_challenge_method,
                     resource, created_at, expires_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (code_hash, client_id, redirect_uri, scope, code_challenge, code_challenge_method,
                     resource, now.isoformat(), expires),
                )
        await self._run(op)

    async def consume_oauth_authorization_code(self, code_hash: str) -> dict[str, Any]:
        now = _now()
        def op() -> dict[str, Any]:
            with self._connect() as db:
                row = db.execute(
                    """SELECT * FROM oauth_authorization_codes WHERE code_hash=? AND used_at IS NULL
                       AND expires_at > ?""", (code_hash, now)
                ).fetchone()
                if row is None:
                    raise KeyError(code_hash)
                db.execute("UPDATE oauth_authorization_codes SET used_at=? WHERE code_hash=?", (now, code_hash))
                return dict(row)
        return await self._run(op)

    async def create_oauth_refresh_token(
        self, *, token_hash: str, client_id: str, scope: str, resource: str,
        lifetime_seconds: int, rotated_from: str | None = None
    ) -> None:
        now = datetime.now(timezone.utc)
        expires = (now + timedelta(seconds=lifetime_seconds)).isoformat()
        def op() -> None:
            with self._connect() as db:
                db.execute(
                    """INSERT INTO oauth_refresh_tokens
                    (token_hash, client_id, scope, resource, created_at, expires_at, rotated_from)
                    VALUES(?,?,?,?,?,?,?)""",
                    (token_hash, client_id, scope, resource, now.isoformat(), expires, rotated_from),
                )
        await self._run(op)

    async def consume_oauth_refresh_token(self, token_hash: str, client_id: str) -> dict[str, Any]:
        now = _now()
        def op() -> dict[str, Any]:
            with self._connect() as db:
                row = db.execute(
                    """SELECT * FROM oauth_refresh_tokens WHERE token_hash=? AND client_id=?
                       AND used_at IS NULL AND revoked_at IS NULL AND expires_at > ?""",
                    (token_hash, client_id, now),
                ).fetchone()
                if row is None:
                    raise KeyError(token_hash)
                db.execute("UPDATE oauth_refresh_tokens SET used_at=? WHERE token_hash=?", (now, token_hash))
                return dict(row)
        return await self._run(op)

    async def oauth_runtime_summary(self) -> dict[str, Any]:
        """Safe OAuth counters for certification; never returns token hashes."""
        def op() -> dict[str, Any]:
            with self._connect() as db:
                clients = int(db.execute("SELECT COUNT(*) n FROM oauth_clients WHERE enabled=1").fetchone()["n"] or 0)
                codes = db.execute(
                    "SELECT COUNT(*) total, SUM(CASE WHEN used_at IS NOT NULL THEN 1 ELSE 0 END) used FROM oauth_authorization_codes"
                ).fetchone()
                refresh = db.execute(
                    """SELECT COUNT(*) total,
                              SUM(CASE WHEN used_at IS NULL AND revoked_at IS NULL AND expires_at > ? THEN 1 ELSE 0 END) active,
                              SUM(CASE WHEN used_at IS NOT NULL THEN 1 ELSE 0 END) rotated
                       FROM oauth_refresh_tokens""",
                    (_now(),),
                ).fetchone()
                return {
                    "enabled_clients": clients,
                    "authorization_codes_total": int(codes["total"] or 0),
                    "authorization_codes_consumed": int(codes["used"] or 0),
                    "refresh_tokens_total": int(refresh["total"] or 0),
                    "refresh_tokens_active": int(refresh["active"] or 0),
                    "refresh_tokens_rotated": int(refresh["rotated"] or 0),
                }
        return await self._run(op)

    async def runtime_metrics(self) -> dict[str, Any]:
        """Aggregate lightweight M4 execution telemetry from persistent work rows."""
        def duration_ms(start: str | None, end: str | None) -> float | None:
            if not start or not end:
                return None
            try:
                a = datetime.fromisoformat(start)
                b = datetime.fromisoformat(end)
                return max(0.0, (b - a).total_seconds() * 1000.0)
            except Exception:
                return None

        def op() -> dict[str, Any]:
            with self._connect() as db:
                rows = db.execute(
                    """SELECT id, state, worker_id, started_at, finished_at, queued_at,
                              dispatch_attempts, recovery_count, failure_class
                       FROM work_items ORDER BY created_at DESC LIMIT 5000"""
                ).fetchall()
                items = [dict(row) for row in rows]
                terminal = [x for x in items if x["state"] in {"completed", "failed"}]
                completed = [x for x in terminal if x["state"] == "completed"]
                failed = [x for x in terminal if x["state"] == "failed"]
                runtimes = [v for x in terminal if (v := duration_ms(x.get("started_at"), x.get("finished_at"))) is not None]
                queue_waits = [v for x in items if (v := duration_ms(x.get("queued_at"), x.get("started_at"))) is not None]
                per_worker: dict[str, dict[str, Any]] = {}
                for item in items:
                    wid = item.get("worker_id")
                    if not wid:
                        continue
                    stat = per_worker.setdefault(wid, {
                        "worker_id": wid, "total": 0, "running": 0, "completed": 0,
                        "failed": 0, "cancelled": 0, "detached": 0, "dispatch_attempts": 0,
                        "recoveries": 0, "runtime_ms": [],
                    })
                    stat["total"] += 1
                    if item["state"] in {"running", "completed", "failed", "cancelled", "detached"}:
                        stat[item["state"]] += 1
                    stat["dispatch_attempts"] += int(item.get("dispatch_attempts") or 0)
                    stat["recoveries"] += int(item.get("recovery_count") or 0)
                    value = duration_ms(item.get("started_at"), item.get("finished_at"))
                    if value is not None:
                        stat["runtime_ms"].append(value)
                normalized = []
                for wid in sorted(per_worker):
                    stat = per_worker[wid]
                    vals = stat.pop("runtime_ms")
                    denom = stat["completed"] + stat["failed"]
                    stat["success_rate"] = round((stat["completed"] / denom * 100.0), 2) if denom else None
                    stat["avg_runtime_ms"] = round(sum(vals) / len(vals), 2) if vals else None
                    normalized.append(stat)
                denom = len(terminal)
                return {
                    "sample_size": len(items),
                    "completed": len(completed),
                    "failed": len(failed),
                    "success_rate": round((len(completed) / denom * 100.0), 2) if denom else None,
                    "avg_runtime_ms": round(sum(runtimes) / len(runtimes), 2) if runtimes else None,
                    "avg_queue_wait_ms": round(sum(queue_waits) / len(queue_waits), 2) if queue_waits else None,
                    "dispatch_attempts": sum(int(x.get("dispatch_attempts") or 0) for x in items),
                    "recoveries": sum(int(x.get("recovery_count") or 0) for x in items),
                    "per_worker": normalized,
                }

        return await self._run(op)

    async def record_mcp_request(
        self,
        *,
        server_id: str,
        http_method: str,
        rpc_method: str | None,
        status_code: int,
        latency_ms: float,
        client_class: str | None = None,
        gateway_session_id: str | None = None,
    ) -> None:
        outcome = "success" if int(status_code) < 500 else "failure"
        def op() -> None:
            with self._connect() as db:
                db.execute(
                    """INSERT INTO mcp_request_metrics
                       (created_at, server_id, http_method, rpc_method, status_code, latency_ms,
                        outcome, client_class, gateway_session_id)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (_now(), server_id, http_method, rpc_method, int(status_code), float(latency_ms),
                     outcome, client_class, gateway_session_id),
                )
        await self._run(op)

    async def record_observability_sample(self, sample: dict[str, Any]) -> None:
        known = {
            "upstream_ok", "upstream_status", "worker_busy", "worker_total",
            "queue_depth", "active_work", "open_alerts",
        }
        extra = {k: v for k, v in sample.items() if k not in known}
        def op() -> None:
            with self._connect() as db:
                db.execute(
                    """INSERT INTO observability_samples
                       (created_at, upstream_ok, upstream_status, worker_busy, worker_total,
                        queue_depth, active_work, open_alerts, data_json)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        _now(), 1 if sample.get("upstream_ok") else 0,
                        str(sample.get("upstream_status") or "unknown"),
                        int(sample.get("worker_busy") or 0), int(sample.get("worker_total") or 0),
                        int(sample.get("queue_depth") or 0), int(sample.get("active_work") or 0),
                        int(sample.get("open_alerts") or 0), json.dumps(extra, ensure_ascii=False),
                    ),
                )
        await self._run(op)

    async def cleanup_observability(self, retention_days: int) -> dict[str, int]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        def op() -> dict[str, int]:
            with self._connect() as db:
                a = db.execute("DELETE FROM observability_samples WHERE created_at < ?", (cutoff,)).rowcount
                b = db.execute("DELETE FROM mcp_request_metrics WHERE created_at < ?", (cutoff,)).rowcount
                return {"samples": int(a), "mcp_requests": int(b)}
        return await self._run(op)

    async def recent_health_summary(self) -> dict[str, Any]:
        """Return the latest persisted server.status transition for each server."""
        def op() -> dict[str, Any]:
            with self._connect() as db:
                rows = db.execute(
                    """SELECT e.* FROM events e
                       JOIN (
                         SELECT server_id, MAX(id) max_id FROM events
                         WHERE kind='server.status' AND server_id IS NOT NULL
                         GROUP BY server_id
                       ) x ON x.max_id=e.id
                       ORDER BY e.server_id"""
                ).fetchall()
                states: dict[str, str] = {}
                for row in rows:
                    data = json.loads(row["data_json"] or "{}")
                    states[str(row["server_id"])] = str(data.get("to") or "unknown")
                if not states:
                    return {"overall": "unknown", "all_healthy": False, "servers": {}}
                values = list(states.values())
                if any(x == "down" for x in values):
                    overall = "down"
                elif any(x != "healthy" for x in values):
                    overall = "degraded"
                else:
                    overall = "healthy"
                return {"overall": overall, "all_healthy": all(x == "healthy" for x in values), "servers": states}
        return await self._run(op)

    async def observability_report(
        self,
        *,
        window_minutes: int,
        availability_target: float,
        mcp_success_target: float,
        queue_p95_limit_ms: float,
        worker_saturation_warn_percent: float,
        reconnect_rate_warn_per_100: float,
        oauth_refresh_failures_max: int,
        orphan_events_max: int,
    ) -> dict[str, Any]:
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=window_minutes)).isoformat()

        def percentile(values: list[float], p: float) -> float | None:
            if not values:
                return None
            vals = sorted(values)
            if len(vals) == 1:
                return vals[0]
            pos = (len(vals) - 1) * p
            lo = int(pos)
            hi = min(lo + 1, len(vals) - 1)
            frac = pos - lo
            return vals[lo] * (1.0 - frac) + vals[hi] * frac

        def budget_remaining(observed: float | None, target: float) -> float | None:
            if observed is None:
                return None
            allowed = max(0.000001, 100.0 - target)
            consumed = max(0.0, target - observed)
            return round(max(0.0, 100.0 - (consumed / allowed * 100.0)), 2)

        def op() -> dict[str, Any]:
            with self._connect() as db:
                samples = [dict(r) for r in db.execute(
                    "SELECT * FROM observability_samples WHERE created_at >= ? ORDER BY id",
                    (cutoff,),
                ).fetchall()]
                sample_n = len(samples)
                healthy_n = sum(int(r["upstream_ok"] or 0) for r in samples)
                availability = round(healthy_n / sample_n * 100.0, 3) if sample_n else None

                req = db.execute(
                    """SELECT COUNT(*) total,
                              SUM(CASE WHEN status_code < 500 THEN 1 ELSE 0 END) ok,
                              AVG(latency_ms) avg_latency
                       FROM mcp_request_metrics WHERE created_at >= ?""",
                    (cutoff,),
                ).fetchone()
                request_total = int(req["total"] or 0)
                request_ok = int(req["ok"] or 0)
                mcp_success = round(request_ok / request_total * 100.0, 3) if request_total else None

                recent_requests = [dict(r) for r in db.execute(
                    """SELECT status_code, latency_ms, rpc_method, created_at
                       FROM mcp_request_metrics WHERE created_at >= ? ORDER BY id DESC LIMIT 10000""",
                    (cutoff,),
                ).fetchall()]
                request_latencies = [float(r["latency_ms"]) for r in recent_requests]

                works = [dict(r) for r in db.execute(
                    """SELECT queued_at, started_at FROM work_items
                       WHERE started_at IS NOT NULL AND queued_at >= ? ORDER BY created_at DESC LIMIT 5000""",
                    (cutoff,),
                ).fetchall()]
                queue_waits: list[float] = []
                for row in works:
                    try:
                        q = datetime.fromisoformat(row["queued_at"])
                        st = datetime.fromisoformat(row["started_at"])
                        queue_waits.append(max(0.0, (st - q).total_seconds() * 1000.0))
                    except Exception:
                        pass
                queue_p95 = percentile(queue_waits, 0.95)

                saturations = [
                    (float(r["worker_busy"]) / float(r["worker_total"]) * 100.0)
                    for r in samples if int(r["worker_total"] or 0) > 0
                ]
                worker_avg = round(sum(saturations) / len(saturations), 2) if saturations else None
                worker_peak = round(max(saturations), 2) if saturations else None
                worker_current = round(saturations[-1], 2) if saturations else None

                reconnects = int(db.execute(
                    "SELECT COUNT(*) n FROM events WHERE kind='gateway.session.reconnected' AND created_at >= ?",
                    (cutoff,),
                ).fetchone()["n"] or 0)
                reconnect_rate = round(reconnects / request_total * 100.0, 3) if request_total else 0.0

                oauth_refresh_failures = int(db.execute(
                    """SELECT COUNT(*) n FROM events
                       WHERE kind='oauth.token.failed' AND created_at >= ?
                         AND json_extract(data_json, '$.grant_type')='refresh_token'""",
                    (cutoff,),
                ).fetchone()["n"] or 0)
                orphan_events = int(db.execute(
                    """SELECT COUNT(*) n FROM events
                       WHERE created_at >= ? AND kind IN (
                         'operations.work_detached','operations.worker_released','operations.lease_released'
                       )""",
                    (cutoff,),
                ).fetchone()["n"] or 0)
                open_alerts = int(db.execute(
                    "SELECT COUNT(*) n FROM operational_alerts WHERE status='open'"
                ).fetchone()["n"] or 0)

                def objective(value: Any, target: Any, *, state: str, message: str, samples_n: int | None = None, budget: float | None = None) -> dict[str, Any]:
                    out = {"value": value, "target": target, "state": state, "message": message}
                    if samples_n is not None:
                        out["samples"] = samples_n
                    if budget is not None:
                        out["error_budget_remaining_percent"] = budget
                    return out

                availability_state = "unknown" if sample_n < 2 else ("healthy" if availability is not None and availability >= availability_target else "down")
                mcp_state = "unknown" if request_total < 1 else ("healthy" if mcp_success is not None and mcp_success >= mcp_success_target else "down")
                queue_state = "unknown" if queue_p95 is None else ("healthy" if queue_p95 <= queue_p95_limit_ms else "degraded")
                sat_state = "unknown" if worker_current is None else ("healthy" if worker_current <= worker_saturation_warn_percent else "degraded")
                rec_state = "unknown" if request_total < 1 else ("healthy" if reconnect_rate <= reconnect_rate_warn_per_100 else "degraded")
                oauth_state = "healthy" if oauth_refresh_failures <= oauth_refresh_failures_max else "down"
                orphan_state = "healthy" if orphan_events <= orphan_events_max else "degraded"

                objectives = {
                    "availability": objective(
                        availability, availability_target, state=availability_state,
                        message=f"Upstream availability {availability if availability is not None else 'unknown'}% / target {availability_target}%",
                        samples_n=sample_n, budget=budget_remaining(availability, availability_target),
                    ),
                    "mcp_success": objective(
                        mcp_success, mcp_success_target, state=mcp_state,
                        message=f"Authenticated MCP service success {mcp_success if mcp_success is not None else 'unknown'}% / target {mcp_success_target}%",
                        samples_n=request_total, budget=budget_remaining(mcp_success, mcp_success_target),
                    ),
                    "queue_p95_ms": objective(
                        round(queue_p95, 2) if queue_p95 is not None else None, queue_p95_limit_ms,
                        state=queue_state, message=f"P95 queue wait {round(queue_p95,2) if queue_p95 is not None else 'unknown'} ms / limit {queue_p95_limit_ms} ms",
                        samples_n=len(queue_waits),
                    ),
                    "worker_saturation": objective(
                        worker_current, worker_saturation_warn_percent, state=sat_state,
                        message=f"Worker saturation {worker_current if worker_current is not None else 'unknown'}% / warning {worker_saturation_warn_percent}%",
                        samples_n=len(saturations),
                    ),
                    "reconnect_rate": objective(
                        reconnect_rate, reconnect_rate_warn_per_100, state=rec_state,
                        message=f"Reconnects {reconnect_rate} per 100 MCP requests / warning {reconnect_rate_warn_per_100}",
                        samples_n=request_total,
                    ),
                    "oauth_refresh_failures": objective(
                        oauth_refresh_failures, oauth_refresh_failures_max, state=oauth_state,
                        message=f"OAuth refresh failures {oauth_refresh_failures} / max {oauth_refresh_failures_max}",
                    ),
                    "orphan_events": objective(
                        orphan_events, orphan_events_max, state=orphan_state,
                        message=f"Orphan/detach reconciliation events {orphan_events} / max {orphan_events_max}",
                    ),
                }
                return {
                    "window_minutes": window_minutes,
                    "window_started_at": cutoff,
                    "generated_at": _now(),
                    "objectives": objectives,
                    "metrics": {
                        "availability_percent": availability,
                        "mcp_requests": request_total,
                        "mcp_successful": request_ok,
                        "mcp_success_percent": mcp_success,
                        "mcp_avg_latency_ms": round(float(req["avg_latency"]), 2) if req["avg_latency"] is not None else None,
                        "mcp_p95_latency_ms": round(percentile(request_latencies, 0.95), 2) if request_latencies else None,
                        "queue_p95_ms": round(queue_p95, 2) if queue_p95 is not None else None,
                        "worker_saturation_current_percent": worker_current,
                        "worker_saturation_avg_percent": worker_avg,
                        "worker_saturation_peak_percent": worker_peak,
                        "reconnects": reconnects,
                        "reconnect_rate_per_100_requests": reconnect_rate,
                        "oauth_refresh_failures": oauth_refresh_failures,
                        "orphan_events": orphan_events,
                        "open_alerts": open_alerts,
                        "sample_count": sample_n,
                    },
                }
        return await self._run(op)

    async def request_cancel_work(self, work_id: str) -> tuple[dict[str, Any], str]:
        """Request cancellation without claiming an already-sent upstream prompt stopped.

        Returns (work, disposition), where disposition is either ``cancelled``
        (safe local cancellation before upstream dispatch) or ``cancel_pending``
        (upstream side effect may already be running).
        """
        now = _now()
        disposition = "cancelled"

        def op() -> str:
            with self._connect() as db:
                row = db.execute("SELECT * FROM work_items WHERE id=?", (work_id,)).fetchone()
                if row is None:
                    raise KeyError(work_id)
                if row["state"] in {"completed", "failed", "cancelled", "detached"}:
                    raise ValueError(f"work {work_id} is already {row['state']}")
                dispatched = bool(row["dispatch_attempts"] or row["dispatched_at"])
                uncertain_states = {
                    "dispatching", "waiting_agent", "reconnecting", "recovering",
                    "stalled", "dispatch_uncertain", "cancel_pending",
                }
                upstream_may_exist = dispatched or row["execution_state"] in uncertain_states
                if not upstream_may_exist:
                    worker_id = row["worker_id"]
                    if worker_id:
                        db.execute("DELETE FROM workspace_leases WHERE worker_id=?", (worker_id,))
                        db.execute(
                            """UPDATE workers SET state='idle', workspace=NULL, pane=NULL, agent=NULL,
                               owner_session_id=NULL, lease_mode='none', work_label=NULL, updated_at=?,
                               last_heartbeat_at=NULL, metadata_json='{}' WHERE id=?""",
                            (now, worker_id),
                        )
                    db.execute(
                        """UPDATE work_items SET state='cancelled', finished_at=?, updated_at=?,
                           execution_state='cancelled', cancel_requested_at=?, last_execution_poll_at=? WHERE id=?""",
                        (now, now, now, now, work_id),
                    )
                    return "cancelled"
                db.execute(
                    """UPDATE work_items SET execution_state='cancel_pending', cancel_requested_at=?,
                       updated_at=?, error=COALESCE(error, 'Cancellation requested; upstream execution may continue')
                       WHERE id=?""",
                    (now, now, work_id),
                )
                return "cancel_pending"

        disposition = await self._run(op)
        return await self.get_work(work_id), disposition

    async def cancel_work(self, work_id: str) -> dict[str, Any]:
        # Backward-compatible API surface; callers that care about uncertainty
        # should use request_cancel_work() and inspect the disposition.
        item, _ = await self.request_cancel_work(work_id)
        return item

    async def detach_work(self, work_id: str, *, reason: str) -> dict[str, Any]:
        """Release Studio tracking/lease while explicitly preserving uncertainty upstream."""
        now = _now()
        def op() -> None:
            with self._connect() as db:
                row = db.execute("SELECT * FROM work_items WHERE id=?", (work_id,)).fetchone()
                if row is None:
                    raise KeyError(work_id)
                if row["state"] in {"completed", "failed", "cancelled", "detached"}:
                    raise ValueError(f"work {work_id} is already {row['state']}")
                worker_id = row["worker_id"]
                if worker_id:
                    db.execute("DELETE FROM workspace_leases WHERE worker_id=?", (worker_id,))
                    db.execute(
                        """UPDATE workers SET state='idle', workspace=NULL, pane=NULL, agent=NULL,
                           owner_session_id=NULL, lease_mode='none', work_label=NULL, updated_at=?,
                           last_heartbeat_at=NULL, metadata_json='{}' WHERE id=?""",
                        (now, worker_id),
                    )
                db.execute(
                    """UPDATE work_items SET state='detached', execution_state='detached', detached_at=?,
                       detached_reason=?, finished_at=?, updated_at=?, last_execution_poll_at=? WHERE id=?""",
                    (now, reason, now, now, now, work_id),
                )
        await self._run(op)
        return await self.get_work(work_id)

    async def reconcile_operations(self, cancel_pending_detach_seconds: int) -> list[dict[str, Any]]:
        """Conservative orphan reconciliation used by M6.1 OperationsManager."""
        now_dt = datetime.now(timezone.utc)
        cutoff = (now_dt - timedelta(seconds=cancel_pending_detach_seconds)).isoformat()
        now = now_dt.isoformat()

        def op() -> list[dict[str, Any]]:
            actions: list[dict[str, Any]] = []
            with self._connect() as db:
                # 1) Cancel-pending items eventually detach; Studio does not
                # claim the remote prompt was cancelled.
                rows = db.execute(
                    """SELECT * FROM work_items WHERE state='running' AND execution_state='cancel_pending'
                       AND cancel_requested_at IS NOT NULL AND cancel_requested_at < ?""",
                    (cutoff,),
                ).fetchall()
                for row in rows:
                    wid = row["worker_id"]
                    if wid:
                        db.execute("DELETE FROM workspace_leases WHERE worker_id=?", (wid,))
                        db.execute(
                            """UPDATE workers SET state='idle', workspace=NULL, pane=NULL, agent=NULL,
                               owner_session_id=NULL, lease_mode='none', work_label=NULL, updated_at=?,
                               last_heartbeat_at=NULL, metadata_json='{}' WHERE id=?""",
                            (now, wid),
                        )
                    db.execute(
                        """UPDATE work_items SET state='detached', execution_state='detached',
                           detached_at=?, detached_reason='cancel_pending_timeout', finished_at=?, updated_at=?
                           WHERE id=?""",
                        (now, now, now, row["id"]),
                    )
                    actions.append({"kind": "work_detached", "work_id": row["id"], "reason": "cancel_pending_timeout", "workspace": row["workspace"]})

                # 2) A running work whose worker is missing/dead cannot safely be
                # treated as cancelled. Detach local tracking and free any stale lease.
                rows = db.execute(
                    """SELECT w.* FROM work_items w LEFT JOIN workers wk ON wk.id=w.worker_id
                       WHERE w.state='running' AND (w.worker_id IS NULL OR wk.id IS NULL OR wk.state='dead')"""
                ).fetchall()
                for row in rows:
                    db.execute("DELETE FROM workspace_leases WHERE worker_id=?", (row["worker_id"],))
                    db.execute(
                        """UPDATE work_items SET state='detached', execution_state='detached', detached_at=?,
                           detached_reason='orphaned_worker', finished_at=?, updated_at=? WHERE id=?""",
                        (now, now, now, row["id"]),
                    )
                    actions.append({"kind": "work_detached", "work_id": row["id"], "reason": "orphaned_worker", "workspace": row["workspace"]})

                # 3) Busy workers with no active work are local orphans and can be
                # safely released because there is no work row claiming ownership.
                rows = db.execute(
                    """SELECT wk.* FROM workers wk LEFT JOIN work_items w
                       ON w.worker_id=wk.id AND w.state='running'
                       WHERE wk.state='busy' AND w.id IS NULL"""
                ).fetchall()
                for row in rows:
                    db.execute("DELETE FROM workspace_leases WHERE worker_id=?", (row["id"],))
                    db.execute(
                        """UPDATE workers SET state='idle', workspace=NULL, pane=NULL, agent=NULL,
                           owner_session_id=NULL, lease_mode='none', work_label=NULL, updated_at=?,
                           last_heartbeat_at=NULL, metadata_json='{}' WHERE id=?""",
                        (now, row["id"]),
                    )
                    actions.append({"kind": "worker_released", "worker_id": row["id"], "reason": "orphaned_busy_worker"})

                # 4) Lease validity is based on ownership, not merely worker=busy.
                # A degraded worker may still own an in-flight upstream action, so
                # releasing its lease would allow two writers into one workspace.
                # Conversely an idle worker must never pin a write lease in M6.2.1.
                rows = db.execute(
                    """SELECT l.*, wk.state AS worker_state, wk.workspace AS worker_workspace,
                              wk.lease_mode AS worker_lease_mode, w.id AS running_work_id
                       FROM workspace_leases l
                       LEFT JOIN workers wk ON wk.id=l.worker_id
                       LEFT JOIN work_items w ON w.worker_id=l.worker_id AND w.state='running' AND w.workspace=l.workspace
                       WHERE wk.id IS NULL
                          OR wk.workspace IS NULL
                          OR wk.workspace!=l.workspace
                          OR wk.lease_mode!='write'
                          OR wk.state IN ('idle','dead')
                          OR (wk.state IN ('busy','degraded') AND l.work_id IS NOT NULL AND w.id IS NULL)"""
                ).fetchall()
                for row in rows:
                    db.execute("DELETE FROM workspace_leases WHERE workspace=?", (row["workspace"],))
                    actions.append({"kind": "lease_released", "workspace": row["workspace"], "worker_id": row["worker_id"], "reason": "orphaned_or_idle_lease"})
            return actions

        return await self._run(op)

    async def cleanup_ephemera(self, retention_days: int, audit_retention_days: int) -> dict[str, int]:
        now = datetime.now(timezone.utc)
        eph_cutoff = (now - timedelta(days=retention_days)).isoformat()
        audit_cutoff = (now - timedelta(days=audit_retention_days)).isoformat()
        def op() -> dict[str, int]:
            with self._connect() as db:
                counts: dict[str, int] = {}
                cur = db.execute(
                    "DELETE FROM oauth_authorization_codes WHERE expires_at < ? AND (used_at IS NOT NULL OR created_at < ?)",
                    (_now(), eph_cutoff),
                ); counts["oauth_codes"] = cur.rowcount
                cur = db.execute(
                    "DELETE FROM oauth_refresh_tokens WHERE expires_at < ? OR (used_at IS NOT NULL AND created_at < ?)",
                    (_now(), eph_cutoff),
                ); counts["oauth_refresh_tokens"] = cur.rowcount
                cur = db.execute(
                    "DELETE FROM gateway_sessions WHERE status IN ('closed','stale') AND last_seen_at < ?",
                    (eph_cutoff,),
                ); counts["gateway_sessions"] = cur.rowcount
                cur = db.execute(
                    "DELETE FROM sessions WHERE status IN ('stale','disconnected') AND last_seen_at < ?",
                    (eph_cutoff,),
                ); counts["sessions"] = cur.rowcount
                cur = db.execute("DELETE FROM audit_log WHERE created_at < ?", (audit_cutoff,))
                counts["audit"] = cur.rowcount
                return counts
        return await self._run(op)

    async def operations_summary(self) -> dict[str, Any]:
        def op() -> dict[str, Any]:
            with self._connect() as db:
                pending = int(db.execute(
                    "SELECT COUNT(*) n FROM work_items WHERE state='running' AND execution_state='cancel_pending'"
                ).fetchone()["n"] or 0)
                detached = int(db.execute("SELECT COUNT(*) n FROM work_items WHERE state='detached'").fetchone()["n"] or 0)
                alerts = int(db.execute("SELECT COUNT(*) n FROM operational_alerts WHERE status='open'").fetchone()["n"] or 0)
                audit = int(db.execute("SELECT COUNT(*) n FROM audit_log").fetchone()["n"] or 0)
                return {"cancel_pending": pending, "detached": detached, "open_alerts": alerts, "audit_rows": audit}
        return await self._run(op)

    # ------------------------------------------------------------------
    # M6.2.3B managed session / project isolation

    @staticmethod
    def _managed_workspace_item(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["enabled"] = bool(item.get("enabled"))
        item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
        return item

    @staticmethod
    def _managed_session_item(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
        return item

    async def upsert_managed_workspace(
        self, *, key: str, name: str, project_path: str, metadata: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        now = _now()
        def op() -> None:
            with self._connect() as db:
                db.execute(
                    """INSERT INTO managed_workspaces(key, name, project_path, enabled, created_at, updated_at, metadata_json)
                       VALUES(?,?,?,1,?,?,?)
                       ON CONFLICT(key) DO UPDATE SET name=excluded.name, project_path=excluded.project_path,
                         enabled=1, updated_at=excluded.updated_at, metadata_json=excluded.metadata_json""",
                    (key, name, project_path, now, now, json.dumps(metadata or {}, ensure_ascii=False)),
                )
        await self._run(op)
        return await self.get_managed_workspace(key)

    async def get_managed_workspace(self, key: str) -> dict[str, Any]:
        def op() -> dict[str, Any]:
            with self._connect() as db:
                row = db.execute("SELECT * FROM managed_workspaces WHERE key=?", (key,)).fetchone()
                if row is None:
                    raise KeyError(key)
                return self._managed_workspace_item(row)
        return await self._run(op)

    async def list_managed_workspaces(self) -> list[dict[str, Any]]:
        def op() -> list[dict[str, Any]]:
            with self._connect() as db:
                rows = db.execute("SELECT * FROM managed_workspaces WHERE enabled=1 ORDER BY name, key").fetchall()
                return [self._managed_workspace_item(row) for row in rows]
        return await self._run(op)

    async def find_managed_workspace_by_project(self, project_path: str) -> dict[str, Any] | None:
        def op() -> dict[str, Any] | None:
            with self._connect() as db:
                row = db.execute(
                    "SELECT * FROM managed_workspaces WHERE project_path=? AND enabled=1",
                    (project_path,),
                ).fetchone()
                return self._managed_workspace_item(row) if row is not None else None
        return await self._run(op)

    async def create_managed_session(
        self, *, name: str, workspace_key: str, project_path: str, server_id: str, port: int,
        desired_state: str = "running", metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        session_id = f"ms-{uuid.uuid4().hex[:16]}"
        now = _now()
        def op() -> None:
            with self._connect() as db:
                db.execute(
                    """INSERT INTO managed_sessions
                       (id, name, workspace_key, project_path, server_id, port, status, desired_state,
                        generation, created_at, updated_at, metadata_json)
                       VALUES(?,?,?,?,?,?, 'stopped', ?, 0, ?, ?, ?)""",
                    (session_id, name, workspace_key, project_path, server_id, int(port), desired_state,
                     now, now, json.dumps(metadata or {}, ensure_ascii=False)),
                )
        await self._run(op)
        return await self.get_managed_session(session_id)

    async def get_managed_session(self, session_id: str) -> dict[str, Any]:
        def op() -> dict[str, Any]:
            with self._connect() as db:
                row = db.execute("SELECT * FROM managed_sessions WHERE id=?", (session_id,)).fetchone()
                if row is None:
                    raise KeyError(session_id)
                return self._managed_session_item(row)
        return await self._run(op)

    async def list_managed_sessions(self, limit: int = 500) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 1000))
        def op() -> list[dict[str, Any]]:
            with self._connect() as db:
                rows = db.execute(
                    "SELECT * FROM managed_sessions ORDER BY updated_at DESC LIMIT ?", (limit,)
                ).fetchall()
                return [self._managed_session_item(row) for row in rows]
        return await self._run(op)

    async def find_active_managed_session_by_project(self, project_path: str) -> dict[str, Any] | None:
        def op() -> dict[str, Any] | None:
            with self._connect() as db:
                row = db.execute(
                    """SELECT * FROM managed_sessions WHERE project_path=? AND desired_state='running'
                       ORDER BY updated_at DESC LIMIT 1""",
                    (project_path,),
                ).fetchone()
                return self._managed_session_item(row) if row is not None else None
        return await self._run(op)

    async def find_managed_session_by_project(self, project_path: str) -> dict[str, Any] | None:
        def op() -> dict[str, Any] | None:
            with self._connect() as db:
                row = db.execute(
                    """SELECT * FROM managed_sessions WHERE project_path=?
                       ORDER BY CASE WHEN desired_state='running' THEN 0 ELSE 1 END, updated_at DESC LIMIT 1""",
                    (project_path,),
                ).fetchone()
                return self._managed_session_item(row) if row is not None else None
        return await self._run(op)

    async def set_managed_session_desired_state(self, session_id: str, desired_state: str) -> dict[str, Any]:
        if desired_state not in {"running", "stopped"}:
            raise ValueError("desired_state must be running or stopped")
        now = _now()
        def op() -> None:
            with self._connect() as db:
                cur = db.execute(
                    "UPDATE managed_sessions SET desired_state=?, updated_at=? WHERE id=?",
                    (desired_state, now, session_id),
                )
                if cur.rowcount == 0:
                    raise KeyError(session_id)
        await self._run(op)
        return await self.get_managed_session(session_id)

    async def update_managed_session_runtime(
        self, session_id: str, *, status: str | None = None, pid: int | None | object = ...,
        error: str | None | object = ..., endpoint: str | None | object = ...,
        log_path: str | None | object = ..., port: int | None = None,
        increment_generation: bool = False,
    ) -> dict[str, Any]:
        now = _now()
        def op() -> None:
            with self._connect() as db:
                row = db.execute("SELECT * FROM managed_sessions WHERE id=?", (session_id,)).fetchone()
                if row is None:
                    raise KeyError(session_id)
                fields = ["updated_at=?"]
                values: list[Any] = [now]
                if status is not None:
                    fields.append("status=?"); values.append(status)
                    if status in {"starting", "ready"}:
                        fields.append("last_started_at=?"); values.append(now)
                    elif status == "stopped":
                        fields.append("last_stopped_at=?"); values.append(now)
                if pid is not ...:
                    fields.append("pid=?"); values.append(pid)
                if error is not ...:
                    fields.append("error=?"); values.append(error)
                if endpoint is not ...:
                    fields.append("endpoint=?"); values.append(endpoint)
                if log_path is not ...:
                    fields.append("log_path=?"); values.append(log_path)
                if port is not None:
                    fields.append("port=?"); values.append(int(port))
                if increment_generation:
                    fields.append("generation=generation+1")
                values.append(session_id)
                db.execute(f"UPDATE managed_sessions SET {', '.join(fields)} WHERE id=?", values)
        await self._run(op)
        return await self.get_managed_session(session_id)

    async def update_managed_session_metadata(
        self, session_id: str, metadata: dict[str, Any]
    ) -> dict[str, Any]:
        now = _now()
        def op() -> None:
            with self._connect() as db:
                row = db.execute("SELECT id FROM managed_sessions WHERE id=?", (session_id,)).fetchone()
                if row is None:
                    raise KeyError(session_id)
                db.execute(
                    "UPDATE managed_sessions SET metadata_json=?, updated_at=? WHERE id=?",
                    (json.dumps(metadata or {}, ensure_ascii=False), now, session_id),
                )
        await self._run(op)
        return await self.get_managed_session(session_id)

    async def bind_gateway_managed_session(
        self, gateway_id: str, *, managed_session_id: str | None, upstream_url: str,
        upstream_session_id: str | None,
    ) -> dict[str, Any]:
        now = _now()
        def op() -> None:
            with self._connect() as db:
                row = db.execute("SELECT studio_session_id FROM gateway_sessions WHERE id=?", (gateway_id,)).fetchone()
                if row is None:
                    raise KeyError(gateway_id)
                db.execute(
                    """UPDATE gateway_sessions SET managed_session_id=?, upstream_url=?, upstream_session_id=?,
                       generation=generation+1, last_seen_at=?, error=NULL WHERE id=?""",
                    (managed_session_id, upstream_url, upstream_session_id, now, gateway_id),
                )
                db.execute(
                    "UPDATE sessions SET managed_session_id=?, last_seen_at=? WHERE id=?",
                    (managed_session_id, now, row["studio_session_id"]),
                )
        await self._run(op)
        return await self.get_gateway_session(gateway_id)

    async def rename_managed_session(self, session_id: str, name: str) -> dict[str, Any]:
        clean = str(name or "").strip()[:240]
        if not clean:
            raise ValueError("managed session name is required")
        now = _now()
        def op() -> None:
            with self._connect() as db:
                cur = db.execute(
                    "UPDATE managed_sessions SET name=?, updated_at=? WHERE id=?",
                    (clean, now, session_id),
                )
                if cur.rowcount == 0:
                    raise KeyError(session_id)
        await self._run(op)
        return await self.get_managed_session(session_id)

    async def touch_managed_session(self, session_id: str) -> dict[str, Any]:
        now = _now()
        def op() -> None:
            with self._connect() as db:
                cur = db.execute(
                    "UPDATE managed_sessions SET last_used_at=?, use_count=COALESCE(use_count,0)+1, updated_at=? WHERE id=?",
                    (now, now, session_id),
                )
                if cur.rowcount == 0:
                    raise KeyError(session_id)
        await self._run(op)
        return await self.get_managed_session(session_id)

    async def managed_session_overview(self, limit: int = 500) -> list[dict[str, Any]]:
        sessions = await self.list_managed_sessions(limit=limit)
        gateways = await self.list_gateway_sessions(1000)
        by_managed: dict[str, list[dict[str, Any]]] = {}
        for gateway in gateways:
            managed_id = gateway.get("managed_session_id")
            if managed_id:
                by_managed.setdefault(str(managed_id), []).append(gateway)
        out: list[dict[str, Any]] = []
        for item in sessions:
            linked = by_managed.get(item["id"], [])
            connected = [g for g in linked if g.get("status") == "connected"]
            providers = sorted({str(g.get("ingress_provider") or "direct") for g in connected})
            last_seen = max((str(g.get("last_seen_at") or "") for g in linked), default="") or None
            lifecycle = "active" if connected else ("idle" if item.get("status") == "ready" else item.get("status") or "unknown")
            out.append({
                **item,
                "lifecycle_state": lifecycle,
                "connected_transports": len(connected),
                "transport_history_count": len(linked),
                "ingress_providers": providers,
                "last_transport_seen_at": last_seen,
            })
        out.sort(key=lambda x: (0 if x.get("lifecycle_state") == "active" else 1 if x.get("lifecycle_state") == "idle" else 2, x.get("name") or ""))
        return out

    async def managed_session_history(self, session_id: str, limit: int = 100) -> dict[str, Any]:
        session = await self.get_managed_session(session_id)
        limit = max(1, min(int(limit), 500))
        def op() -> list[dict[str, Any]]:
            with self._connect() as db:
                pattern = f'%"managed_session_id": "{session_id}"%'
                rows = db.execute(
                    """SELECT * FROM audit_log
                       WHERE (target_type='managed_session' AND target_id=?) OR data_json LIKE ?
                       ORDER BY id DESC LIMIT ?""",
                    (session_id, pattern, limit),
                ).fetchall()
                items = []
                for row in rows:
                    item = dict(row)
                    item["data"] = json.loads(item.pop("data_json") or "{}")
                    items.append(item)
                return items
        audits = await self._run(op)
        gateways = await self.list_gateway_sessions_for_managed_session(session_id)
        return {"session": session, "audit": audits, "transports": gateways[:limit]}

    async def managed_cutover_summary(self) -> dict[str, Any]:
        def op() -> dict[str, Any]:
            with self._connect() as db:
                gw = db.execute(
                    """SELECT COUNT(*) total,
                              SUM(CASE WHEN managed_session_id IS NOT NULL THEN 1 ELSE 0 END) bound,
                              SUM(CASE WHEN managed_session_id IS NULL THEN 1 ELSE 0 END) unbound
                       FROM gateway_sessions WHERE status='connected'"""
                ).fetchone()
                logical = db.execute(
                    """SELECT COUNT(*) total,
                              SUM(CASE WHEN managed_session_id IS NOT NULL THEN 1 ELSE 0 END) pinned
                       FROM sessions WHERE status IN ('connected','active')"""
                ).fetchone()
                return {
                    "connected_transports": int(gw["total"] or 0),
                    "bound_transports": int(gw["bound"] or 0),
                    "unbound_transports": int(gw["unbound"] or 0),
                    "active_logical_sessions": int(logical["total"] or 0),
                    "pinned_logical_sessions": int(logical["pinned"] or 0),
                }
        return await self._run(op)

    async def count_connected_gateways_for_managed_session(self, managed_session_id: str) -> int:
        def op() -> int:
            with self._connect() as db:
                row = db.execute(
                    "SELECT COUNT(*) n FROM gateway_sessions WHERE managed_session_id=? AND status='connected'",
                    (managed_session_id,),
                ).fetchone()
                return int(row["n"] or 0)
        return await self._run(op)

    async def list_gateway_sessions_for_managed_session(self, managed_session_id: str) -> list[dict[str, Any]]:
        def op() -> list[dict[str, Any]]:
            with self._connect() as db:
                rows = db.execute(
                    "SELECT * FROM gateway_sessions WHERE managed_session_id=? ORDER BY last_seen_at DESC",
                    (managed_session_id,),
                ).fetchall()
                return [self._gateway_session_item(row) for row in rows]
        return await self._run(op)
