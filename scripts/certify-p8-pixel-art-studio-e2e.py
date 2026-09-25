#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
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
from mcp_studio.pixel_art_studio import PixelArtStudioProvider, VisualAssetOrchestrator
from mcp_studio.world_authoring import WorldAuthoringManager


HIRDA_ROOT = Path(__file__).resolve().parents[1]
EARTH_ROOT = Path(
    os.environ.get("HIRDA_P8_EARTH_ROOT", "/data/earth-616-pixi-world")
).resolve()
NOVA_ROOT = Path(
    os.environ.get(
        "HIRDA_P8_NOVA_ROOT",
        "/home/alfred/ghq/github.com/jkfastdevth/nova-oracle",
    )
).resolve()
EARTH_PORT = int(os.environ.get("HIRDA_P8_EARTH_PORT", "18826"))
HIRDA_PORT = int(os.environ.get("HIRDA_P8_HIRDA_PORT", "18110"))
WORKSPACE = os.environ.get("HIRDA_P8_WORKSPACE", "earth-616-p8-live")
UPSTREAM_COMMIT = "f8c246635c4621a6c2b427833149afdc3dc3c719"


class WorkspacePool:
    enabled = True

    async def list_workspaces(self):
        return [
            {
                "key": WORKSPACE,
                "name": "Earth-616 P8 live certification",
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

    with tempfile.TemporaryDirectory(prefix="hirda-p8-live-") as td:
        temp_root = Path(td)
        earth_db = temp_root / "earth616-p8.sqlite3"
        earth_frontend = temp_root / "frontend"
        earth_frontend.mkdir(parents=True, exist_ok=True)

        earth = make_server(
            db_path=earth_db,
            frontend_dir=earth_frontend,
            port=EARTH_PORT,
            world_pixel_dist_dir=EARTH_ROOT / "pixel-world" / "dist",
        )
        earth_thread = threading.Thread(
            target=earth.serve_forever,
            name="earth616-p8-e2e",
            daemon=True,
        )
        earth_thread.start()

        audit_db_path = temp_root / "hirda-p8.sqlite3"
        audit_db = Database(str(audit_db_path))
        asyncio.run(audit_db.init())
        pool = WorkspacePool()
        earth_validator_url = (
            f"http://127.0.0.1:{EARTH_PORT}/api/world-authoring/validate"
        )
        authoring = WorldAuthoringManager(
            pool,
            timeout_seconds=20,
            validator_url=earth_validator_url,
        )
        gateway = GatewaySessionManager(
            SimpleNamespace(),
            audit_db,
            managed_sessions=pool,
            world_authoring=authoring,
        )
        provider = PixelArtStudioProvider(
            enabled=True,
            root=HIRDA_ROOT / ".hirda-cache" / "pixel-art-studio",
            output_root=temp_root / "visual-candidates",
            expected_commit=UPSTREAM_COMMIT,
        )
        state = provider.status()
        if state.get("ready") is not True or state.get("commit") != UPSTREAM_COMMIT:
            raise AssertionError(state)
        visual_assets = VisualAssetOrchestrator(
            provider,
            earth_validator_url=earth_validator_url,
            timeout_seconds=20,
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

        @app.post("/api/visual-assets/generate")
        async def generate_visual(payload: dict):
            proposal = dict(payload.get("proposal") or {})
            result = await visual_assets.generate_and_promote(
                proposal=proposal,
                commands=[
                    dict(item)
                    for item in (payload.get("commands") or [])
                    if isinstance(item, dict)
                ],
                validation_id=str(payload.get("validationId") or ""),
                rationale=str(payload.get("rationale") or ""),
            )
            candidate = result.get("candidate", {})
            promotion = result.get("earthPromotion", {})
            await audit_db.add_audit(
                "visual_asset.promote",
                actor="nova/http",
                target_type="visual_asset",
                target_id=str(candidate.get("logicalId") or ""),
                data={
                    "provider": "pixel_art_studio",
                    "proposal_id": proposal.get("proposalId"),
                    "candidate_id": candidate.get("candidateId"),
                    "logical_id": candidate.get("logicalId"),
                    "sha256": candidate.get("sha256"),
                    "provider_commit": candidate.get("providerCommit"),
                    "promoted_by": result.get("promotedBy"),
                    "hirda_world_authority": result.get("hirdaWorldAuthority"),
                    "earth_validation_id": promotion.get("validationId"),
                    "canonical_url": promotion.get("canonicalUrl"),
                },
            )
            return result

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
            name="hirda-p8-e2e",
            daemon=True,
        )
        hirda_thread.start()

        try:
            wait_json(f"http://127.0.0.1:{EARTH_PORT}/api/world")
            wait_json(f"http://127.0.0.1:{HIRDA_PORT}/health")

            module_path = NOVA_ROOT / "portable_oracle" / "world_authoring.py"
            spec = importlib.util.spec_from_file_location(
                "hirda_p8_nova_world_authoring",
                module_path,
            )
            if spec is None or spec.loader is None:
                raise RuntimeError("cannot load Nova world_authoring.py")
            nova_world_authoring = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(nova_world_authoring)
            WorldAuthoringClient = nova_world_authoring.WorldAuthoringClient

            client = WorldAuthoringClient(
                NOVA_ROOT,
                base_url=f"http://127.0.0.1:{HIRDA_PORT}",
                workspace=WORKSPACE,
                timeout_sec=20,
            )
            result = client.submit_nova_decision(
                {
                    "kind": "request_visual_asset",
                    "intent": "create my canonical twilight reading lantern",
                    "targetArea": {"x": 690, "y": 420, "width": 28, "height": 28},
                    "tags": ["lantern", "cozy"],
                    "assetRequest": {
                        "logicalId": "earth.prop.nova-p8-twilight-lantern",
                        "capability": "prop",
                        "description": "warm pixel lantern for Nova's twilight reading corner",
                        "width": 32,
                        "height": 32,
                        "palette": "sweetie16",
                    },
                    "rationale": "หนูอยากสร้างโคมไฟพิกเซลของตัวเองให้เป็นพร็อพจริงในโลก",
                },
                recorded_at="2026-09-24T17:55:00Z",
                decision_ref="nova-p8-live-visual-20260924",
            )
            if result.get("status") not in {"promoted", "already_promoted"}:
                raise AssertionError(result)

            proposal_id = result["proposal"]["proposalId"]
            validation_id = result["validation"]["validationId"]
            promotion = result["promotion"]
            earth_promotion = promotion["earthPromotion"]
            if promotion.get("promotedBy") != "earth-616":
                raise AssertionError(promotion)
            if promotion.get("hirdaWorldAuthority") is not False:
                raise AssertionError(promotion)
            if earth_promotion.get("providerId") != "pixel_art_studio":
                raise AssertionError(earth_promotion)

            world = wait_json(f"http://127.0.0.1:{EARTH_PORT}/api/world")
            assets = world.get("visualAssets")
            if not isinstance(assets, list):
                raise AssertionError("Earth projection has no visualAssets")
            matching = [
                item
                for item in assets
                if isinstance(item, dict)
                and item.get("logicalId") == "earth.prop.nova-p8-twilight-lantern"
            ]
            if len(matching) != 1:
                raise AssertionError(matching)
            canonical = matching[0]
            if canonical.get("proposalId") != proposal_id:
                raise AssertionError(canonical)
            if canonical.get("validationId") != validation_id:
                raise AssertionError(canonical)
            if canonical.get("providerId") != "pixel_art_studio":
                raise AssertionError(canonical)
            if canonical.get("targetArea") != {
                "x": 690,
                "y": 420,
                "width": 28,
                "height": 28,
            }:
                raise AssertionError(canonical)

            canonical_url = str(canonical.get("canonicalUrl") or "")
            if not canonical_url.startswith("/assets/world-pixel/generated/"):
                raise AssertionError(canonical)
            with urlopen(
                f"http://127.0.0.1:{EARTH_PORT}{canonical_url}",
                timeout=5,
            ) as response:
                png = response.read()
            if not png.startswith(b"\x89PNG\r\n\x1a\n"):
                raise AssertionError("canonical asset is not PNG")
            sha256 = hashlib.sha256(png).hexdigest()
            if sha256 != canonical.get("sha256"):
                raise AssertionError((sha256, canonical))

            with sqlite3.connect(audit_db_path) as db:
                rows = db.execute(
                    """
                    SELECT actor, action, data_json
                    FROM audit_log
                    WHERE action IN ('world_authoring.preview','visual_asset.promote')
                    ORDER BY id
                    """
                ).fetchall()
            parsed = [
                {"actor": actor, "action": action, "data": json.loads(data_json)}
                for actor, action, data_json in rows
            ]
            actions = {row["action"] for row in parsed}
            if actions != {"world_authoring.preview", "visual_asset.promote"}:
                raise AssertionError(parsed)
            if any(row["actor"] != "nova/http" for row in parsed):
                raise AssertionError(parsed)

            candidate = promotion["candidate"]
            if candidate.get("providerCommit") != UPSTREAM_COMMIT:
                raise AssertionError(candidate)
            if candidate.get("canonical") is not False:
                raise AssertionError(candidate)

            print("P8_PIXEL_ART_STUDIO_E2E_PASS")
            print(
                json.dumps(
                    {
                        "proposalId": proposal_id,
                        "validationId": validation_id,
                        "logicalId": canonical["logicalId"],
                        "candidateId": candidate["candidateId"],
                        "providerCommit": candidate["providerCommit"],
                        "sha256": sha256,
                        "canonicalUrl": canonical_url,
                        "promotedBy": promotion["promotedBy"],
                        "hirdaWorldAuthority": promotion["hirdaWorldAuthority"],
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
