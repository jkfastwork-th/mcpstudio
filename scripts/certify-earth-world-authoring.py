from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from mcp_studio.world_authoring import WorldAuthoringManager


class OneWorkspacePool:
    def __init__(self, project_path: Path):
        self.project_path = project_path.resolve()

    async def list_workspaces(self):
        return [
            {
                "key": "earth-world-cert",
                "name": "Earth World Certification",
                "project_path": str(self.project_path),
                "enabled": True,
            }
        ]


async def run(project_path: Path, validator_url: str | None = None) -> dict:
    manager = WorldAuthoringManager(
        OneWorkspacePool(project_path),
        timeout_seconds=20,
        validator_url=validator_url,
    )
    result = await manager.preview(
        workspace="earth-world-cert",
        proposal={
            "proposalId": "hirda-live-cert-001",
            "kind": "decorate_area",
            "intent": "quiet sakura reading corner",
            "targetArea": {"x": 560, "y": 360, "width": 120, "height": 90},
            "tags": ["sakura", "bench", "lamp"],
        },
    )

    validation = result.get("result", {}).get("validation", {})
    commands = validation.get("commands") or []

    assert result["preview_only"] is True
    assert result["world_authority_changed"] is False
    assert result["asset_promoted"] is False
    assert validation.get("valid") is True
    assert validation.get("requiresApproval") is False
    assert commands and commands[0].get("type") == "map.decorate"

    earth_validation = result.get("earth_validation") or {}
    if validator_url:
        assert earth_validation.get("configured") is True
        assert earth_validation.get("valid") is True
        assert earth_validation.get("mutationAuthorized") is False
        assert earth_validation.get("worldAuthority") == "earth-616"
    else:
        assert earth_validation.get("configured") is False
        assert earth_validation.get("status") == "not_configured"

    return {
        "workspace": result["workspace"]["project_path"],
        "proposal_id": result["result"]["proposal"]["proposalId"],
        "valid": validation["valid"],
        "requires_approval": validation["requiresApproval"],
        "command": commands[0]["type"],
        "preview_only": result["preview_only"],
        "world_authority_changed": result["world_authority_changed"],
        "asset_promoted": result["asset_promoted"],
        "earth_validation": earth_validation,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_path", type=Path)
    parser.add_argument("--validator-url")
    args = parser.parse_args()

    summary = asyncio.run(run(args.project_path, args.validator_url))
    print(
        "HIRDA_EARTH_WORLD_AUTHORING_AUTHORITY_PASS"
        if args.validator_url
        else "HIRDA_EARTH_WORLD_AUTHORING_PREVIEW_PASS"
    )
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
