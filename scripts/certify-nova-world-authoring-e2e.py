#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.request import urlopen

import uvicorn
from fastapi import FastAPI

from mcp_studio.db import Database
from mcp_studio.gateway import GatewaySessionManager
from mcp_studio.world_authoring import WorldAuthoringManager

EARTH_ROOT = Path(os.environ.get("HIRDA_E2E_EARTH_ROOT", "/data/earth-616")).resolve()
NOVA_ROOT = Path(
    os.environ.get(
        "HIRDA_E2E_NOVA_ROOT",
        "/home/alfred/ghq/github.com/jkfastdevth/nova-oracle",
    )
).resolve()
EARTH_DB = Path(
    os.environ.get(
        "HIRDA_E2E_EARTH_DB",
        str(EARTH_ROOT / "data" / "runtime" / "earth616.db"),
    )
).resolve()
EARTH_PORT = int(os.environ.get("HIRDA_E2E_EARTH_PORT", "18816"))
HIRDA_PORT = int(os.environ.get("HIRDA_E2E_HIRDA_PORT", "18100"))
WORKSPACE = os.environ.get("HIRDA_E2E_WORKSPACE", "earth-616-live")


class WorkspacePool:
    enabled = True

    async def list_workspaces(self):
        return [
            {
                "key": WORKSPACE,
                "name": "Earth-616 production",
                "project_path": str(EARTH_ROOT),
                "enabled": True,
            }
        ]


def wait_json(url: str, timeout: float = 20.0) -> dict:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=2) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if isinstance(payload, dict):
                return payload
        except Exception as exc:
            last_error = exc
            time.sleep(0.2)
    raise RuntimeError(f"timed out waiting for {url}: {last_error}")


def main() -> int:
    sys.path.insert(0, str(EARTH_ROOT / "backend"))
    from earth616.server import make_server

    earth = make_server(
        db_path=EARTH_DB,
        frontend_dir=EARTH_ROOT / "frontend",
        port=EARTH_PORT,
        world_pixel_dist_dir=EARTH_ROOT / "pixel-world" / "dist",
    )
    earth_thread = threading.Thread(
        target=earth.serve_forever,
        name="earth616-e2e",
        daemon=True,
    )
    earth_thread.start()

    with tempfile.TemporaryDirectory(prefix="hirda-nova-earth-e2e-") as td:
        audit_db_path = Path(td) / "hirda-e2e.sqlite3"
        audit_db = Database(str(audit_db_path))
        asyncio.run(audit_db.init())
        pool = WorkspacePool()
        authoring = WorldAuthoringManager(
            pool,
            timeout_seconds=20,
            validator_url=(
                f"http://127.0.0.1:{EARTH_PORT}/api/world-authoring/validate"
            ),
        )
        gateway = GatewaySessionManager(
            SimpleNamespace(),
            audit_db,
            managed_sessions=pool,
            world_authoring=authoring,
        )

        app = FastAPI()

        @app.get("/health")
        async def health():
            return {"ok": True}

        @app.post("/api/world-authoring/preview")
        async def preview(payload: dict):
            return await gateway.world_authoring_preview(
                workspace=str(payload.get("workspace") or ""),
                proposal=dict(payload.get("proposal") or {}),
                providers=[
                    dict(item)
                    for item in (payload.get("providers") or [])
                    if isinstance(item, dict)
                ],
                actor="nova/http",
            )

        @app.post("/api/world-authoring/promote")
        async def promote(payload: dict):
            return await gateway.world_authoring_promote(
                workspace=str(payload.get("workspace") or ""),
                proposal=dict(payload.get("proposal") or {}),
                commands=[
                    dict(item)
                    for item in (payload.get("commands") or [])
                    if isinstance(item, dict)
                ],
                validation_id=str(payload.get("validationId") or ""),
                rationale=str(payload.get("rationale") or ""),
                actor="nova/http",
            )

        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=HIRDA_PORT,
                log_level="warning",
                access_log=False,
                lifespan="off",
            )
        )
        hirda_thread = threading.Thread(
            target=server.run,
            name="hirda-e2e",
            daemon=True,
        )
        hirda_thread.start()

        try:
            wait_json(f"http://127.0.0.1:{EARTH_PORT}/api/world")
            wait_json(f"http://127.0.0.1:{HIRDA_PORT}/health")

            sys.path.insert(0, str(NOVA_ROOT))
            from portable_oracle.world_authoring import WorldAuthoringClient

            client = WorldAuthoringClient(
                NOVA_ROOT,
                base_url=f"http://127.0.0.1:{HIRDA_PORT}",
                workspace=WORKSPACE,
                timeout_sec=20,
            )
            result = client.submit_nova_decision(
                {
                    "kind": "decorate_area",
                    "intent": "Nova twilight lantern reading corner",
                    "targetArea": {"x": 690, "y": 420, "width": 58, "height": 42},
                    "tags": ["lantern", "bench", "sakura"],
                    "rationale": "หนูอยากทำมุมอ่านหนังสือเล็ก ๆ ที่มีแสงโคมอุ่นและนั่งพักได้",
                },
                recorded_at="2026-09-24T12:15:00Z",
                decision_ref="nova-earth-live-e2e-20260924",
            )

            if result.get("status") not in {"promoted", "already_promoted"}:
                raise AssertionError(result)

            proposal = result["proposal"]
            proposal_id = proposal["proposalId"]
            validation_id = result["validation"]["validationId"]
            promotion = result["promotion"]
            if promotion.get("promoted_by") != "earth-616":
                raise AssertionError(promotion)
            if promotion.get("hirda_world_authority") is not False:
                raise AssertionError(promotion)

            world = wait_json(f"http://127.0.0.1:{EARTH_PORT}/api/world")
            decorations = world.get("authoringDecorations")
            if not isinstance(decorations, list):
                raise AssertionError("Earth projection has no authoringDecorations")
            matching = [
                item
                for item in decorations
                if isinstance(item, dict) and item.get("proposalId") == proposal_id
            ]
            if len(matching) != 1:
                raise AssertionError(
                    f"expected one canonical decoration for {proposal_id}, got {len(matching)}"
                )
            canonical = matching[0]
            if canonical.get("validationId") != validation_id:
                raise AssertionError(canonical)
            if canonical.get("authoredBy") != "nova":
                raise AssertionError(canonical)

            with sqlite3.connect(audit_db_path) as db:
                rows = db.execute(
                    """
                    SELECT actor, action, data_json
                    FROM audit_log
                    WHERE action IN ('world_authoring.preview','world_authoring.promote')
                    ORDER BY id
                    """
                ).fetchall()
            parsed = [
                {"actor": actor, "action": action, "data": json.loads(data_json)}
                for actor, action, data_json in rows
            ]
            relevant = [
                row
                for row in parsed
                if row["data"].get("proposal_id") == proposal_id
            ]
            actions = {row["action"] for row in relevant}
            if actions != {"world_authoring.preview", "world_authoring.promote"}:
                raise AssertionError(relevant)
            if any(row["actor"] != "nova/http" for row in relevant):
                raise AssertionError(relevant)

            print("NOVA_HIRDA_EARTH_E2E_PASS")
            print(
                json.dumps(
                    {
                        "proposalId": proposal_id,
                        "validationId": validation_id,
                        "status": result["status"],
                        "promotedBy": promotion["promoted_by"],
                        "hirdaWorldAuthority": promotion["hirda_world_authority"],
                        "earthCanonicalDecoration": canonical,
                        "hirdaAuditActions": sorted(actions),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0
        finally:
            server.should_exit = True
            hirda_thread.join(timeout=10)
            earth.shutdown()
            earth.server_close()
            earth_thread.join(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
