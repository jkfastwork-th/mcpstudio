from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import re
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import urlparse

import httpx


_LOGICAL_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")
_ALLOWED_KINDS = {"sprite", "prop", "icon"}
_ALLOWED_PALETTES = {"sweetie16", "endesga32", "pico8", "gameboy", "c64", "onebit"}


class PixelArtStudioError(RuntimeError):
    pass


def _clean_text(value: object, *, field: str, max_len: int) -> str:
    text = " ".join(str(value or "").split()).strip()
    if not text:
        raise PixelArtStudioError(f"{field} required")
    if len(text) > max_len:
        raise PixelArtStudioError(f"{field} exceeds {max_len} characters")
    return text


def _bounded_tags(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise PixelArtStudioError("tags must be an array of strings")
    tags: list[str] = []
    for raw in value:
        tag = " ".join(raw.split()).strip().lower()
        if not tag:
            continue
        if len(tag) > 40:
            raise PixelArtStudioError("tag exceeds 40 characters")
        if tag not in tags:
            tags.append(tag)
    if len(tags) > 16:
        raise PixelArtStudioError("at most 16 tags are allowed")
    return tags


@dataclass(frozen=True, slots=True)
class PixelArtCandidate:
    candidate_id: str
    logical_id: str
    asset_kind: str
    prompt: str
    tags: tuple[str, ...]
    palette: str
    width: int
    height: int
    sha256: str
    png_bytes: bytes
    provider_commit: str
    candidate_dir: Path
    master_path: Path
    preview_path: Path

    def earth_payload(
        self,
        *,
        proposal_id: str,
        validation_id: str,
        capability: str,
    ) -> dict[str, Any]:
        proposal_id = _clean_text(proposal_id, field="proposalId", max_len=128)
        validation_id = _clean_text(validation_id, field="validationId", max_len=128)
        capability = _clean_text(capability, field="capability", max_len=64)
        return {
            "schema": "hirda-visual-candidate-v1",
            "status": "candidate_ready",
            "canonical": False,
            "provider": "pixel_art_studio",
            "proposalId": proposal_id,
            "validationId": validation_id,
            "logicalId": self.logical_id,
            "capability": capability,
            "mimeType": "image/png",
            "width": self.width,
            "height": self.height,
            "sha256": self.sha256,
            "artifactBase64": base64.b64encode(self.png_bytes).decode("ascii"),
            "upstreamCommit": self.provider_commit,
            "candidateId": self.candidate_id,
            "authoredBy": "nova",
        }

    def public_manifest(self) -> dict[str, Any]:
        return {
            "schema": "hirda-visual-asset-candidate-v1",
            "candidateId": self.candidate_id,
            "logicalId": self.logical_id,
            "assetKind": self.asset_kind,
            "prompt": self.prompt,
            "tags": list(self.tags),
            "palette": self.palette,
            "width": self.width,
            "height": self.height,
            "sha256": self.sha256,
            "provider": "pixel_art_studio",
            "providerCommit": self.provider_commit,
            "candidatePath": str(self.master_path),
            "previewPath": str(self.preview_path),
            "canonical": False,
        }


class PixelArtStudioProvider:
    """HIRDA-owned bounded adapter around Gamezxz/pixel-art-studio.

    Upstream code supplies deterministic pixel primitives. HIRDA owns the request
    schema and templates, so Nova never supplies Python/build-script code.
    """

    def __init__(
        self,
        *,
        enabled: bool,
        root: Path,
        output_root: Path,
        expected_commit: str = "",
    ) -> None:
        self.enabled = bool(enabled)
        self.root = Path(root).expanduser().resolve()
        self.output_root = Path(output_root).expanduser().resolve()
        self.expected_commit = str(expected_commit or "").strip().lower()
        self._module: ModuleType | None = None

    @classmethod
    def from_settings(
        cls,
        studio: Any,
        *,
        base_dir: Path,
    ) -> "PixelArtStudioProvider":
        root = Path(
            str(
                getattr(
                    studio,
                    "pixel_art_studio_root",
                    ".hirda-cache/pixel-art-studio",
                )
                or ".hirda-cache/pixel-art-studio"
            )
        ).expanduser()
        output_root = Path(
            str(
                getattr(
                    studio,
                    "pixel_art_studio_output_root",
                    "./data/visual-candidates/pixel-art-studio",
                )
                or "./data/visual-candidates/pixel-art-studio"
            )
        ).expanduser()
        if not root.is_absolute():
            root = base_dir / root
        if not output_root.is_absolute():
            output_root = base_dir / output_root
        return cls(
            enabled=bool(getattr(studio, "pixel_art_studio_enabled", False)),
            root=root,
            output_root=output_root,
            expected_commit=str(
                getattr(studio, "pixel_art_studio_expected_commit", "") or ""
            ),
        )

    def _metadata(self) -> dict[str, Any]:
        path = self.root / ".hirda-upstream.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _commit(self) -> str:
        commit = str(self._metadata().get("commit") or "").strip().lower()
        return commit if len(commit) == 40 else ""

    def status(self) -> dict[str, Any]:
        commit = self._commit()
        pixelstudio = self.root / "scripts" / "pixelstudio.py"
        pixelpipe = self.root / "scripts" / "pixelpipe.py"
        study = self.root / "scripts" / "study.py"
        license_path = self.root / "LICENSE"
        required_ok = all(path.is_file() for path in (pixelstudio, pixelpipe, study, license_path))
        pin_ok = bool(
            commit
            and (
                not self.expected_commit
                or commit == self.expected_commit
            )
        )
        return {
            "enabled": self.enabled,
            "root": str(self.root),
            "output_root": str(self.output_root),
            "commit": commit or None,
            "expected_commit": self.expected_commit or None,
            "required_files_ok": required_ok,
            "pin_ok": pin_ok,
            "ready": bool(self.enabled and required_ok and pin_ok),
            "candidate_only": True,
            "arbitrary_code_from_nova": False,
            "earth_world_authority": False,
        }

    def _load_module(self) -> ModuleType:
        state = self.status()
        if not state["ready"]:
            raise PixelArtStudioError(
                "Pixel Art Studio is not ready: "
                + json.dumps(state, sort_keys=True)
            )
        if self._module is not None:
            return self._module
        module_path = self.root / "scripts" / "pixelstudio.py"
        module_name = "hirda_pixelstudio_" + str(state["commit"])[:12]
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        if spec is None or spec.loader is None:
            raise PixelArtStudioError("cannot load pixelstudio.py")
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            raise PixelArtStudioError(f"pixelstudio import failed: {exc}") from exc
        if not hasattr(module, "Sprite") or not hasattr(module, "PALETTES"):
            raise PixelArtStudioError("pixelstudio API contract missing Sprite/PALETTES")
        self._module = module
        return module

    @staticmethod
    def _dimensions(request: dict[str, Any]) -> tuple[int, int]:
        width = int(request.get("width") or 32)
        height = int(request.get("height") or 32)
        if not 8 <= width <= 128 or not 8 <= height <= 128:
            raise PixelArtStudioError("width/height must be between 8 and 128")
        return width, height

    @staticmethod
    def _draw_lantern(sprite: Any, width: int, height: int) -> None:
        cx = width // 2
        ground = max(6, height - 4)
        top = max(2, height // 5)
        sprite.rect(cx - 1, top + 6, cx + 1, ground, "#5d4a3d")
        sprite.rect(cx - 5, top + 3, cx + 5, top + 10, "#ef7d57")
        sprite.rect(cx - 4, top + 4, cx + 4, top + 9, "#ffcd75")
        sprite.rect(cx - 6, top + 2, cx + 6, top + 3, "#333c57")
        sprite.rect(cx - 4, top + 10, cx + 4, top + 11, "#333c57")
        sprite.ellipse(cx - 7, top + 1, cx + 7, top + 13, "#ffcd75", fill=False)

    @staticmethod
    def _draw_bench(sprite: Any, width: int, height: int) -> None:
        x0 = max(2, width // 6)
        x1 = min(width - 3, width - width // 6)
        y = max(4, height * 2 // 3)
        sprite.rect(x0, y - 5, x1, y - 2, "#b86f50")
        sprite.rect(x0, y, x1, y + 3, "#d77643")
        sprite.rect(x0 + 2, y + 4, x0 + 4, min(height - 2, y + 9), "#3e2731")
        sprite.rect(x1 - 4, y + 4, x1 - 2, min(height - 2, y + 9), "#3e2731")

    @staticmethod
    def _draw_tree(sprite: Any, width: int, height: int) -> None:
        cx = width // 2
        ground = height - 3
        crown_y = max(7, height // 3)
        sprite.rect(cx - 2, crown_y + 5, cx + 2, ground, "#733e39")
        sprite.ellipse(cx - 10, crown_y - 5, cx + 2, crown_y + 8, "#f6757a")
        sprite.ellipse(cx - 2, crown_y - 8, cx + 10, crown_y + 7, "#e8b796")
        sprite.ellipse(cx - 5, crown_y - 10, cx + 5, crown_y + 4, "#f4f4f4")

    @staticmethod
    def _draw_sprite(sprite: Any, width: int, height: int) -> None:
        cx = width // 2
        head_r = max(3, min(width, height) // 7)
        head_y = max(head_r + 2, height // 4)
        sprite.ellipse(
            cx - head_r,
            head_y - head_r,
            cx + head_r,
            head_y + head_r,
            "#ffcd75",
        )
        body_top = head_y + head_r - 1
        body_bottom = min(height - 5, body_top + max(6, height // 3))
        sprite.polygon(
            [
                (cx, body_top),
                (max(1, cx - width // 6), body_bottom),
                (min(width - 2, cx + width // 6), body_bottom),
            ],
            "#3b5dc9",
        )
        sprite.line(cx - 2, body_bottom, cx - 4, height - 3, "#333c57")
        sprite.line(cx + 2, body_bottom, cx + 4, height - 3, "#333c57")

    @staticmethod
    def _draw_icon(sprite: Any, width: int, height: int) -> None:
        cx, cy = width // 2, height // 2
        rx, ry = max(3, width // 4), max(4, height // 3)
        sprite.polygon(
            [
                (cx, cy - ry),
                (cx + rx, cy - 1),
                (cx, cy + ry),
                (cx - rx, cy - 1),
            ],
            "#41a6f6",
        )
        sprite.polygon(
            [
                (cx, cy - ry + 2),
                (cx + max(1, rx // 2), cy - 1),
                (cx, cy + max(1, ry // 3)),
                (cx - max(1, rx // 2), cy - 1),
            ],
            "#73eff7",
        )

    def generate_candidate(self, request: dict[str, Any]) -> PixelArtCandidate:
        if not isinstance(request, dict):
            raise PixelArtStudioError("asset request must be an object")
        logical_id = _clean_text(request.get("logicalId"), field="logicalId", max_len=128)
        if not _LOGICAL_ID.fullmatch(logical_id):
            raise PixelArtStudioError("logicalId contains unsupported characters")
        asset_kind = str(request.get("kind") or "prop").strip().lower()
        if asset_kind not in _ALLOWED_KINDS:
            raise PixelArtStudioError(
                "kind must be one of " + ", ".join(sorted(_ALLOWED_KINDS))
            )
        prompt = _clean_text(request.get("prompt"), field="prompt", max_len=500)
        tags = _bounded_tags(request.get("tags"))
        width, height = self._dimensions(request)
        palette = str(request.get("palette") or "sweetie16").strip().lower()
        if palette not in _ALLOWED_PALETTES:
            raise PixelArtStudioError("unsupported palette")

        normalized = {
            "logicalId": logical_id,
            "kind": asset_kind,
            "prompt": prompt,
            "tags": tags,
            "width": width,
            "height": height,
            "palette": palette,
            "providerCommit": self._commit(),
        }
        request_digest = hashlib.sha256(
            json.dumps(
                normalized,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        candidate_id = "pas-" + request_digest[:24]
        candidate_dir = self.output_root / candidate_id
        candidate_dir.mkdir(parents=True, exist_ok=True)
        master_path = candidate_dir / "master.png"
        preview_path = candidate_dir / "preview.png"

        module = self._load_module()
        palette_values = list(module.PALETTES[palette])
        sprite = module.Sprite(width, height, palette=palette_values)

        haystack = " ".join([prompt.lower(), *tags])
        if asset_kind == "sprite":
            self._draw_sprite(sprite, width, height)
        elif "lantern" in haystack or "lamp" in haystack or "โคม" in haystack:
            self._draw_lantern(sprite, width, height)
        elif "bench" in haystack or "seat" in haystack or "ม้านั่ง" in haystack:
            self._draw_bench(sprite, width, height)
        elif "sakura" in haystack or "tree" in haystack or "ต้นไม้" in haystack:
            self._draw_tree(sprite, width, height)
        else:
            self._draw_icon(sprite, width, height)

        sprite.save_png(str(master_path))
        sprite.preview(str(preview_path), scale=6, labels=False)
        png_bytes = master_path.read_bytes()
        if not png_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            raise PixelArtStudioError("pixelstudio did not emit a PNG")
        sha256 = hashlib.sha256(png_bytes).hexdigest()

        manifest = {
            **normalized,
            "schema": "hirda-pixel-art-studio-candidate-v1",
            "candidateId": candidate_id,
            "sha256": sha256,
            "master": "master.png",
            "preview": "preview.png",
            "canonical": False,
            "arbitraryCodeFromNova": False,
            "earthWorldAuthority": False,
        }
        (candidate_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return PixelArtCandidate(
            candidate_id=candidate_id,
            logical_id=logical_id,
            asset_kind=asset_kind,
            prompt=prompt,
            tags=tuple(tags),
            palette=palette,
            width=width,
            height=height,
            sha256=sha256,
            png_bytes=png_bytes,
            provider_commit=self._commit(),
            candidate_dir=candidate_dir,
            master_path=master_path,
            preview_path=preview_path,
        )


class VisualAssetOrchestrator:
    """Generate a deterministic HIRDA candidate, then let Earth promote it."""

    _CAPABILITY_KIND = {
        "character": "sprite",
        "prop": "prop",
    }

    def __init__(
        self,
        provider: PixelArtStudioProvider,
        *,
        earth_validator_url: str | None,
        timeout_seconds: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.provider = provider
        self.timeout_seconds = float(timeout_seconds)
        self.transport = transport
        self.earth_validator_url = self._validate_earth_url(earth_validator_url)

    @staticmethod
    def _validate_earth_url(value: str | None) -> str | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        parsed = urlparse(raw)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise PixelArtStudioError(
                "Earth world-authoring endpoint must use loopback HTTP"
            )
        return raw

    def _promote_url(self) -> str:
        if not self.earth_validator_url:
            raise PixelArtStudioError("Earth world-authoring validator is not configured")
        marker = "/api/world-authoring/validate"
        if marker not in self.earth_validator_url:
            raise PixelArtStudioError("unexpected Earth validator URL")
        base = self.earth_validator_url.split(marker, 1)[0]
        return f"{base}/api/world-authoring/promote"

    def _provider_request(
        self,
        proposal: dict[str, Any],
    ) -> tuple[dict[str, Any], str]:
        if proposal.get("kind") != "request_visual_asset":
            raise PixelArtStudioError(
                "Pixel Art Studio requires request_visual_asset proposal"
            )
        asset_request = proposal.get("assetRequest")
        if not isinstance(asset_request, dict):
            raise PixelArtStudioError("request_visual_asset requires assetRequest")

        logical_id = _clean_text(
            asset_request.get("logicalId"),
            field="assetRequest.logicalId",
            max_len=128,
        )
        capability = _clean_text(
            asset_request.get("capability"),
            field="assetRequest.capability",
            max_len=64,
        ).lower()
        provider_kind = self._CAPABILITY_KIND.get(capability)
        if provider_kind is None:
            raise PixelArtStudioError(
                "pixel_art_studio currently supports character and prop capabilities"
            )

        description = _clean_text(
            asset_request.get("description"),
            field="assetRequest.description",
            max_len=500,
        )
        request: dict[str, Any] = {
            "logicalId": logical_id,
            "kind": provider_kind,
            "prompt": description,
            "tags": proposal.get("tags") or [],
            "width": asset_request.get("width") or 32,
            "height": asset_request.get("height") or 32,
            "palette": asset_request.get("palette") or "sweetie16",
        }
        return request, capability

    async def generate_and_promote(
        self,
        *,
        proposal: dict[str, Any],
        commands: list[dict[str, Any]],
        validation_id: str,
        rationale: str,
    ) -> dict[str, Any]:
        if not isinstance(proposal, dict):
            raise PixelArtStudioError("proposal must be an object")
        if not isinstance(commands, list) or any(
            not isinstance(item, dict) for item in commands
        ):
            raise PixelArtStudioError("commands must be an array of objects")

        proposal_id = _clean_text(
            proposal.get("proposalId"),
            field="proposalId",
            max_len=128,
        )
        validation_id = _clean_text(
            validation_id,
            field="validationId",
            max_len=128,
        )
        rationale = _clean_text(rationale, field="rationale", max_len=1000)

        provider_request, capability = self._provider_request(proposal)
        candidate = self.provider.generate_candidate(provider_request)
        visual_candidate = candidate.earth_payload(
            proposal_id=proposal_id,
            validation_id=validation_id,
            capability=capability,
        )

        async with httpx.AsyncClient(
            timeout=self.timeout_seconds,
            transport=self.transport,
        ) as client:
            response = await client.post(
                self._promote_url(),
                json={
                    "proposal": proposal,
                    "commands": commands,
                    "validationId": validation_id,
                    "authoredBy": "nova",
                    "rationale": rationale,
                    "visualCandidate": visual_candidate,
                },
            )
        if response.status_code >= 400:
            raise PixelArtStudioError(
                "Earth visual promotion failed: " + response.text[:2000]
            )

        try:
            promotion = response.json()
        except ValueError as exc:
            raise PixelArtStudioError(
                "Earth visual promotion returned invalid JSON"
            ) from exc
        if not isinstance(promotion, dict):
            raise PixelArtStudioError("Earth visual promotion returned non-object")
        if (
            promotion.get("mutationAuthorized") is not True
            or promotion.get("worldAuthority") != "earth-616"
            or promotion.get("authoredBy") != "nova"
            or promotion.get("validationId") != validation_id
            or promotion.get("proposalId") != proposal_id
            or promotion.get("logicalId") != candidate.logical_id
            or promotion.get("providerId") != "pixel_art_studio"
        ):
            raise PixelArtStudioError(
                "Earth visual promotion authority contract failed"
            )

        return {
            "schema": "hirda-visual-asset-result-v1",
            "status": promotion.get("status"),
            "candidate": candidate.public_manifest(),
            "earthPromotion": promotion,
            "promotedBy": "earth-616",
            "hirdaWorldAuthority": False,
        }
