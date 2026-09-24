#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from pathlib import Path
from urllib.request import urlopen

from mcp_studio.computer import ComputerUseManager
from mcp_studio.db import Database
from mcp_studio.graft import GraftManager
from mcp_studio.integrations import build_integration_manager
from mcp_studio.machine_registry import MachineRegistry
from mcp_studio.machine_router import MachineCapabilityRouter
from mcp_studio.settings import load_settings
from mcp_studio.tool_permissions import decide_tool_call


def production_machine_snapshot(url: str) -> dict:
    with urlopen(url, timeout=5) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise RuntimeError("production machine snapshot is not an object")
    return payload


async def certify(args: argparse.Namespace) -> dict:
    settings = load_settings(args.config)
    base_dir = settings.config_path.parent
    registry = MachineRegistry(settings.studio, base_dir=base_dir)
    target = registry.get(args.machine)

    production = production_machine_snapshot(args.production_url)
    production_item = next(
        (item for item in production.get("machines", []) if item.get("id") == target.machine_id),
        None,
    )
    if production_item is None:
        raise RuntimeError(f"production registry missing machine: {target.machine_id}")
    production_online = bool(production_item.get("online"))

    health = await registry.provider_health(target, "desktop_commander")
    provider_ready = bool(health.get("ready"))

    workspace_path = str(
        (settings.studio.managed_session_workspaces or {}).get(args.workspace) or base_dir
    )

    with tempfile.TemporaryDirectory(prefix="hirda-machine-cert-") as td:
        db = Database(str(Path(td) / "cert.sqlite3"))
        await db.init()
        managed = await db.create_managed_session(
            name="machine-capability-certification",
            workspace_key=args.workspace,
            project_path=workspace_path,
            server_id="serena-8001",
            port=8298,
            desired_state="stopped",
            metadata={},
        )

        integrations = build_integration_manager(
            GraftManager(settings, db),
            settings.studio,
            base_dir=base_dir,
        )
        computer = ComputerUseManager(settings.studio, db)
        router = MachineCapabilityRouter(registry, integrations, computer)

        try:
            desktop = await integrations.reconcile("desktop-commander")
            backend_ready = bool(
                desktop.get("stage") == "ready" and not desktop.get("blocked")
            )

            bound = await router.bind_session(
                db,
                managed["id"],
                target.machine_id,
                actor="certify-machine-capabilities",
            )
            session = bound["session"]
            machine_bound = (
                session.get("metadata", {}).get("machine_id") == target.machine_id
            )

            write_args = {
                "path": args.marker,
                "content": args.marker_text,
                "mode": "rewrite",
            }
            write_decision = decide_tool_call(
                settings.studio,
                session,
                "hirda__desktop_commander__write_file",
                write_args,
                declared_category="write",
            )
            if not write_decision.allowed:
                raise RuntimeError(
                    f"write permission denied: {write_decision.code} {write_decision.message}"
                )

            context = {
                "managed_session_id": session["id"],
                "workspace_key": session["workspace_key"],
                "project_path": session["project_path"],
                "machine_id": target.machine_id,
                "policy": write_decision.policy,
            }
            await router.call_backend_tool(
                "hirda__desktop_commander__write_file",
                write_args,
                context=context,
            )

            read_args = {"path": args.marker, "offset": 0, "length": 5}
            read_decision = decide_tool_call(
                settings.studio,
                session,
                "hirda__desktop_commander__read_file",
                read_args,
                declared_category="read",
            )
            if not read_decision.allowed:
                raise RuntimeError(
                    f"read permission denied: {read_decision.code} {read_decision.message}"
                )
            context["policy"] = read_decision.policy
            read_result = await router.call_backend_tool(
                "hirda__desktop_commander__read_file",
                read_args,
                context=context,
            )
            remote_read = args.marker_text in json.dumps(read_result, ensure_ascii=False)

            scope_fail_closed = False
            try:
                await router.call_backend_tool(
                    "hirda__desktop_commander__read_file",
                    {"path": "..\\..\\hirda-outside-cert.txt", "offset": 0, "length": 5},
                    context=context,
                )
            except Exception as exc:
                scope_fail_closed = "scope" in str(exc).lower() or "workspace" in str(exc).lower()

            computer_fail_closed = False
            if not target.local:
                try:
                    await router.computer_descriptor(session["id"], session)
                except Exception as exc:
                    computer_fail_closed = "unavailable" in str(exc).lower()

            restored = await router.bind_session(
                db,
                session["id"],
                registry.local_machine_id,
                actor="certify-machine-capabilities",
            )
            restore_local = (
                restored["session"].get("metadata", {}).get("machine_id")
                == registry.local_machine_id
            )
        finally:
            await integrations.close()

    checks = {
        "production_online": production_online,
        "provider_ready": provider_ready,
        "backend_ready": backend_ready,
        "machine_bound": machine_bound,
        "remote_write_read": remote_read,
        "scope_fail_closed": scope_fail_closed,
        "computer_use_fail_closed": computer_fail_closed if not target.local else True,
        "restore_local_machine": restore_local,
    }
    return {
        "schema": "hirda-machine-certification-v1",
        "machine": target.machine_id,
        "workspace": args.workspace,
        "checks": checks,
        "certified": all(checks.values()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Live HIRDA multi-machine certification")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--machine", default="JKFASTDEV")
    parser.add_argument("--workspace", default="mcp-studio")
    parser.add_argument("--production-url", default="http://127.0.0.1:8100/api/machines")
    parser.add_argument("--marker", default="hirda-machine-certification.txt")
    parser.add_argument("--marker-text", default="HIRDA_MULTI_MACHINE_CERTIFIED")
    args = parser.parse_args()
    result = asyncio.run(certify(args))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["certified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
