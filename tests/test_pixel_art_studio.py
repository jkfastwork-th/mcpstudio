from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import pytest

from mcp_studio.pixel_art_studio import (
    PixelArtStudioError,
    PixelArtStudioProvider,
    VisualAssetOrchestrator,
)


_COMMIT = "1" * 40
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M/wHwAF/gL+"
    "X2NDNwAAAABJRU5ErkJggg=="
)


def _fake_upstream(root: Path) -> Path:
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    (root / "LICENSE").write_text("MIT\n", encoding="utf-8")
    (scripts / "pixelpipe.py").write_text("# fake\n", encoding="utf-8")
    (scripts / "study.py").write_text("# fake\n", encoding="utf-8")
    (root / ".hirda-upstream.json").write_text(
        json.dumps(
            {
                "schema": "hirda-pixel-art-studio-upstream-v1",
                "repository": "https://github.com/Gamezxz/pixel-art-studio",
                "commit": _COMMIT,
                "ref": "main",
            }
        ),
        encoding="utf-8",
    )
    (scripts / "pixelstudio.py").write_text(
        """
import base64
from pathlib import Path
PALETTES = {
    "sweetie16": ["#000000", "#ffffff"],
    "endesga32": ["#000000", "#ffffff"],
    "pico8": ["#000000", "#ffffff"],
    "gameboy": ["#000000", "#ffffff"],
    "c64": ["#000000", "#ffffff"],
    "onebit": ["#000000", "#ffffff"],
}
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M/wHwAF/gL+"
    "X2NDNwAAAABJRU5ErkJggg=="
)
class Sprite:
    def __init__(self, width, height, palette=None):
        self.width = width
        self.height = height
    def rect(self, *args, **kwargs): return self
    def ellipse(self, *args, **kwargs): return self
    def polygon(self, *args, **kwargs): return self
    def line(self, *args, **kwargs): return self
    def save_png(self, path, *args, **kwargs):
        Path(path).write_bytes(PNG)
    def preview(self, path, *args, **kwargs):
        Path(path).write_bytes(PNG)
""",
        encoding="utf-8",
    )
    return root


def _provider(tmp_path: Path) -> PixelArtStudioProvider:
    root = _fake_upstream(tmp_path / "upstream")
    return PixelArtStudioProvider(
        enabled=True,
        root=root,
        output_root=tmp_path / "candidates",
        expected_commit=_COMMIT,
    )


def test_provider_generates_bounded_candidate_without_nova_code(tmp_path: Path):
    provider = _provider(tmp_path)
    candidate = provider.generate_candidate(
        {
            "logicalId": "earth.prop.twilight-lantern",
            "kind": "prop",
            "prompt": "warm lantern for a quiet reading corner",
            "tags": ["lantern", "cozy"],
            "width": 32,
            "height": 32,
            "palette": "sweetie16",
        }
    )

    assert candidate.png_bytes == _PNG
    assert candidate.master_path.is_file()
    assert candidate.preview_path.is_file()
    manifest = json.loads((candidate.candidate_dir / "manifest.json").read_text())
    assert manifest["canonical"] is False
    assert manifest["arbitraryCodeFromNova"] is False
    assert manifest["earthWorldAuthority"] is False
    assert manifest["providerCommit"] == _COMMIT


def test_provider_rejects_path_like_logical_id(tmp_path: Path):
    provider = _provider(tmp_path)
    with pytest.raises(PixelArtStudioError, match="logicalId"):
        provider.generate_candidate(
            {
                "logicalId": "../../escape",
                "kind": "prop",
                "prompt": "lantern",
                "tags": ["lantern"],
            }
        )


@pytest.mark.asyncio
async def test_orchestrator_keeps_earth_as_promotion_authority(tmp_path: Path):
    provider = _provider(tmp_path)
    calls: list[str] = []
    proposal = {
        "proposalId": "nova-world-proposal-p8-test",
        "kind": "request_visual_asset",
        "intent": "create a warm lantern prop",
        "tags": ["lantern", "cozy"],
        "assetRequest": {
            "logicalId": "earth.prop.twilight-lantern",
            "capability": "prop",
            "description": "warm lantern",
            "width": 32,
            "height": 32,
            "palette": "sweetie16",
        },
    }
    commands = [{
        "type": "asset.request",
        "logicalId": "earth.prop.twilight-lantern",
        "request": {
            "requestId": proposal["proposalId"],
            "capability": "prop",
            "description": "warm lantern",
            "styleProfile": "earth616-cozy-isometric-jrpg",
        },
    }]

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        payload = json.loads(request.content.decode())
        assert payload["proposal"] == proposal
        assert payload["commands"] == commands
        assert payload["validationId"] == "earth-validation-p8"
        assert payload["authoredBy"] == "nova"
        candidate = payload["visualCandidate"]
        assert candidate["schema"] == "hirda-visual-candidate-v1"
        assert candidate["status"] == "candidate_ready"
        assert candidate["canonical"] is False
        assert candidate["provider"] == "pixel_art_studio"
        assert candidate["proposalId"] == proposal["proposalId"]
        assert candidate["validationId"] == "earth-validation-p8"
        assert candidate["logicalId"] == "earth.prop.twilight-lantern"
        assert candidate["capability"] == "prop"
        assert base64.b64decode(candidate["artifactBase64"]) == _PNG
        return httpx.Response(
            200,
            json={
                "schema": "earth616-visual-asset-promotion-v1",
                "status": "promoted",
                "proposalId": proposal["proposalId"],
                "logicalId": "earth.prop.twilight-lantern",
                "capability": "prop",
                "providerId": "pixel_art_studio",
                "validationId": "earth-validation-p8",
                "mutationAuthorized": True,
                "worldAuthority": "earth-616",
                "authoredBy": "nova",
                "canonicalUrl": "/assets/world-pixel/generated/lantern.png",
            },
        )

    orchestrator = VisualAssetOrchestrator(
        provider,
        earth_validator_url="http://127.0.0.1:8816/api/world-authoring/validate",
        transport=httpx.MockTransport(handler),
    )
    result = await orchestrator.generate_and_promote(
        proposal=proposal,
        commands=commands,
        validation_id="earth-validation-p8",
        rationale="Nova chose a deterministic lantern candidate.",
    )

    assert calls == ["/api/world-authoring/promote"]
    assert result["status"] == "promoted"
    assert result["promotedBy"] == "earth-616"
    assert result["hirdaWorldAuthority"] is False
    assert result["earthPromotion"]["canonicalUrl"].endswith("lantern.png")
