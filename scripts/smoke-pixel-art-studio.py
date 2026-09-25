#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

from mcp_studio.pixel_art_studio import PixelArtStudioProvider


ROOT = Path(__file__).resolve().parents[1]
UPSTREAM_COMMIT = "f8c246635c4621a6c2b427833149afdc3dc3c719"


def main() -> int:
    provider = PixelArtStudioProvider(
        enabled=True,
        root=ROOT / ".hirda-cache" / "pixel-art-studio",
        output_root=ROOT / "data" / "visual-candidates" / "pixel-art-studio-smoke",
        expected_commit=UPSTREAM_COMMIT,
    )
    state = provider.status()
    if not state["ready"]:
        raise SystemExit("provider not ready: " + json.dumps(state, sort_keys=True))

    candidate = provider.generate_candidate(
        {
            "logicalId": "earth.prop.p8-lantern-smoke",
            "kind": "prop",
            "prompt": "warm pixel lantern for Nova's reading corner",
            "tags": ["lantern", "cozy"],
            "width": 32,
            "height": 32,
            "palette": "sweetie16",
        }
    )
    if not candidate.master_path.is_file() or not candidate.preview_path.is_file():
        raise SystemExit("candidate files missing")
    if not candidate.png_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        raise SystemExit("candidate is not a PNG")

    print("PIXEL_ART_STUDIO_REAL_SMOKE_PASS")
    print(
        json.dumps(
            {
                "candidateId": candidate.candidate_id,
                "logicalId": candidate.logical_id,
                "sha256": candidate.sha256,
                "providerCommit": candidate.provider_commit,
                "master": str(candidate.master_path),
                "preview": str(candidate.preview_path),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
