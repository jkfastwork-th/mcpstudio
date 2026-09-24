from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from .managed_sessions import ManagedSessionManager


class WorldAuthoringError(RuntimeError):
    pass


class WorldAuthoringManager:
    """Preview-only bridge from HIRDA to a registered Earth's authoring CLI.

    The bridge never mutates Earth state. It only invokes the workspace-local
    authoring preview command and returns its validation/command plan.
    """

    def __init__(
        self,
        managed_sessions: ManagedSessionManager,
        *,
        timeout_seconds: float = 12.0,
        validator_url: str | None = None,
        validator_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.managed_sessions = managed_sessions
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.validator_url = self._validate_validator_url(validator_url)
        self.validator_transport = validator_transport

    @staticmethod
    def _validate_validator_url(value: str | None) -> str | None:
        url = str(value or "").strip()
        if not url:
            return None

        parsed = urlparse(url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path != "/api/world-authoring/validate"
        ):
            raise WorldAuthoringError(
                "Earth validator URL must be loopback HTTP "
                "ending exactly in /api/world-authoring/validate"
            )
        return url

    async def _resolve_workspace(self, selector: str) -> dict[str, Any]:
        value = str(selector or "").strip()
        if not value:
            raise WorldAuthoringError("workspace is required")

        workspaces = await self.managed_sessions.list_workspaces()
        exact = [
            workspace
            for workspace in workspaces
            if value in {
                str(workspace.get("key") or ""),
                str(workspace.get("project_path") or ""),
            }
        ]
        if len(exact) == 1:
            return exact[0]

        named = [
            workspace
            for workspace in workspaces
            if str(workspace.get("name") or "").casefold() == value.casefold()
        ]
        if len(named) == 1:
            return named[0]

        raise WorldAuthoringError(f"unknown or ambiguous world-authoring workspace: {value}")

    async def _earth_validate(
        self,
        *,
        proposal: dict[str, Any],
        preview_result: dict[str, Any],
    ) -> dict[str, Any]:
        if not self.validator_url:
            return {
                "configured": False,
                "status": "not_configured",
                "mutationAuthorized": False,
            }

        local_validation = preview_result.get("validation")
        commands = (
            local_validation.get("commands")
            if isinstance(local_validation, dict)
            else []
        )
        payload = {
            "proposal": preview_result.get("proposal")
            if isinstance(preview_result.get("proposal"), dict)
            else proposal,
            "commands": commands if isinstance(commands, list) else [],
        }

        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                transport=self.validator_transport,
            ) as client:
                response = await client.post(self.validator_url, json=payload)
        except httpx.HTTPError as exc:
            raise WorldAuthoringError(
                f"Earth world-authoring validator unavailable: {exc}"
            ) from exc

        if response.status_code >= 400:
            detail = response.text.strip()
            raise WorldAuthoringError(
                f"Earth world-authoring validator returned HTTP "
                f"{response.status_code}: {detail[:2000]}"
            )

        try:
            result = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise WorldAuthoringError(
                "Earth world-authoring validator returned invalid JSON"
            ) from exc

        if not isinstance(result, dict):
            raise WorldAuthoringError(
                "Earth world-authoring validator returned a non-object response"
            )

        if result.get("mutationAuthorized") is not False:
            raise WorldAuthoringError(
                "Earth world-authoring validator violated preview-only contract"
            )

        return {
            "configured": True,
            "status": "validated",
            **result,
        }

    async def promote(
        self,
        *,
        proposal: dict[str, Any],
        commands: list[dict[str, Any]],
        validation_id: str,
        rationale: str,
        authored_by: str = "nova",
    ) -> dict[str, Any]:
        if not self.validator_url:
            raise WorldAuthoringError("Earth world-authoring validator is not configured")
        if not isinstance(proposal, dict) or not proposal:
            raise WorldAuthoringError("proposal must be a non-empty object")
        if not isinstance(commands, list) or any(not isinstance(item, dict) for item in commands):
            raise WorldAuthoringError("commands must be an array of objects")
        validation_id = str(validation_id or "").strip()
        rationale = " ".join(str(rationale or "").split())
        if not validation_id:
            raise WorldAuthoringError("validationId required")
        if authored_by != "nova":
            raise WorldAuthoringError("world authoring promotion requires authoredBy=nova")
        if not rationale:
            raise WorldAuthoringError("promotion rationale required")

        promote_url = self.validator_url.rsplit("/", 1)[0] + "/promote"
        payload = {
            "proposal": proposal,
            "commands": commands,
            "validationId": validation_id,
            "authoredBy": authored_by,
            "rationale": rationale,
        }
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                transport=self.validator_transport,
            ) as client:
                response = await client.post(promote_url, json=payload)
        except httpx.HTTPError as exc:
            raise WorldAuthoringError(
                f"Earth world-authoring promoter unavailable: {exc}"
            ) from exc

        if response.status_code >= 400:
            detail = response.text.strip()
            raise WorldAuthoringError(
                f"Earth world-authoring promoter returned HTTP "
                f"{response.status_code}: {detail[:2000]}"
            )

        try:
            result = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise WorldAuthoringError(
                "Earth world-authoring promoter returned invalid JSON"
            ) from exc
        if not isinstance(result, dict):
            raise WorldAuthoringError(
                "Earth world-authoring promoter returned a non-object response"
            )
        if (
            result.get("mutationAuthorized") is not True
            or result.get("worldAuthority") != "earth-616"
            or result.get("authoredBy") != "nova"
            or result.get("validationId") != validation_id
        ):
            raise WorldAuthoringError(
                "Earth world-authoring promoter violated authority contract"
            )
        return result

    async def resolve_workspace(self, selector: str) -> dict[str, Any]:
        return await self._resolve_workspace(selector)

    def validate_audit_events(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        allowed_kinds = {
            "provider_availability",
            "job_created",
            "job_updated",
            "asset_intake",
            "asset_approved",
            "asset_rejected",
            "asset_promoted",
            "earth_validation_attached",
            "placeholder_pack_imported",
        }
        if not isinstance(events, list) or not events:
            raise WorldAuthoringError("events must be a non-empty array")
        if len(events) > 100:
            raise WorldAuthoringError("at most 100 world-authoring audit events are accepted per call")

        normalized: list[dict[str, Any]] = []
        for index, raw in enumerate(events):
            if not isinstance(raw, dict):
                raise WorldAuthoringError(f"audit event {index} must be an object")
            kind = str(raw.get("kind") or "").strip()
            if kind not in allowed_kinds:
                raise WorldAuthoringError(f"unsupported world-authoring audit event kind: {kind}")

            event_id = str(raw.get("eventId") or "").strip()
            if not event_id or len(event_id) > 160:
                raise WorldAuthoringError(f"audit event {index} requires a bounded eventId")

            details = raw.get("details") or {}
            if not isinstance(details, dict):
                raise WorldAuthoringError(f"audit event {index} details must be an object")
            safe_details: dict[str, Any] = {}
            for key, value in details.items():
                if not isinstance(key, str) or len(key) > 80:
                    raise WorldAuthoringError(f"audit event {index} has invalid detail key")
                if not isinstance(value, (str, int, float, bool)) and value is not None:
                    raise WorldAuthoringError(
                        f"audit event {index} detail {key!r} must be scalar"
                    )
                if isinstance(value, str) and len(value) > 1000:
                    raise WorldAuthoringError(
                        f"audit event {index} detail {key!r} is too long"
                    )
                safe_details[key] = value

            provider_id = str(raw.get("providerId") or "")[:160] or None
            job_id = str(raw.get("jobId") or "")[:160] or None
            request_id = str(raw.get("requestId") or "")[:160] or None
            logical_id = str(raw.get("logicalId") or "")[:240] or None
            pack_id = str(raw.get("packId") or "")[:160] or None

            if kind in {"job_created", "job_updated", "asset_intake", "asset_approved", "asset_promoted", "earth_validation_attached"}:
                if not request_id or not logical_id:
                    raise WorldAuthoringError(
                        f"audit event {index} kind {kind} requires requestId and logicalId"
                    )
            if kind in {"job_created", "job_updated", "asset_intake"} and not job_id:
                raise WorldAuthoringError(
                    f"audit event {index} kind {kind} requires jobId"
                )
            if kind in {"asset_approved", "asset_promoted", "earth_validation_attached"}:
                validation_id = str(safe_details.get("validationId") or "").strip()
                if not validation_id:
                    raise WorldAuthoringError(
                        f"audit event {index} kind {kind} requires details.validationId"
                    )
            if kind == "earth_validation_attached":
                if safe_details.get("valid") is not True:
                    raise WorldAuthoringError(
                        f"audit event {index} Earth validation must be valid"
                    )
                if safe_details.get("mutationAuthorized") is not False:
                    raise WorldAuthoringError(
                        f"audit event {index} Earth validation must deny mutation authority"
                    )
                if safe_details.get("worldAuthority") != "earth-616":
                    raise WorldAuthoringError(
                        f"audit event {index} Earth validation worldAuthority must be earth-616"
                    )

            trace_id = ":".join(
                value for value in (request_id, job_id, logical_id) if value
            )

            normalized.append(
                {
                    "event_id": event_id,
                    "kind": kind,
                    "recorded_at": str(raw.get("recordedAt") or "")[:80],
                    "provider_id": provider_id,
                    "job_id": job_id,
                    "request_id": request_id,
                    "logical_id": logical_id,
                    "pack_id": pack_id,
                    "trace_id": trace_id or None,
                    "details": safe_details,
                }
            )
        return normalized

    async def preview(
        self,
        *,
        workspace: str,
        proposal: dict[str, Any],
        providers: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(proposal, dict) or not proposal:
            raise WorldAuthoringError("proposal must be a non-empty object")

        target = await self._resolve_workspace(workspace)
        project_path = Path(str(target.get("project_path") or "")).resolve()
        pixel_world = project_path / "pixel-world"
        package_json = pixel_world / "package.json"
        cli = pixel_world / "tools" / "world-authoring.mjs"

        if not package_json.is_file() or not cli.is_file():
            raise WorldAuthoringError(
                f"workspace {target.get('key')!r} does not expose the Earth Pixi authoring CLI"
            )

        request_payload = {
            "proposal": proposal,
            "providers": list(providers or []),
        }
        payload = json.dumps(request_payload, ensure_ascii=False).encode("utf-8")

        try:
            process = await asyncio.create_subprocess_exec(
                "npm",
                "run",
                "authoring:preview",
                "--silent",
                cwd=str(pixel_world),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (OSError, ValueError) as exc:
            raise WorldAuthoringError(f"failed to start Earth authoring CLI: {exc}") from exc

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(payload),
                timeout=self.timeout_seconds,
            )
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise WorldAuthoringError(
                f"Earth authoring preview timed out after {self.timeout_seconds:.1f}s"
            ) from exc

        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            raise WorldAuthoringError(
                f"Earth authoring preview failed with exit {process.returncode}: {detail[:2000]}"
            )

        try:
            result = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorldAuthoringError("Earth authoring preview returned invalid JSON") from exc

        if not isinstance(result, dict):
            raise WorldAuthoringError("Earth authoring preview returned a non-object response")

        earth_validation = await self._earth_validate(
            proposal=proposal,
            preview_result=result,
        )

        return {
            "preview_only": True,
            "world_authority_changed": False,
            "asset_promoted": False,
            "earth_validation": earth_validation,
            "workspace": {
                "key": target.get("key"),
                "name": target.get("name"),
                "project_path": str(project_path),
            },
            "result": result,
        }
