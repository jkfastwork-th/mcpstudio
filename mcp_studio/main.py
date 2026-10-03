from __future__ import annotations

import asyncio
import hashlib
import html
import ipaddress
import json
import os
import time
from typing import Any
from datetime import datetime, timezone
from uuid import uuid4

import websockets
from urllib.parse import parse_qs
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.websockets import WebSocketDisconnect

from .db import Database, LeaseConflict
from .gateway import GatewaySessionManager, make_gateway_router
from .managed_sessions import ManagedSessionManager, ManagedSessionError, ManagedSessionConflict, WorkspaceNotAllowed
from .health import HealthManager
from .herdr import HerdrManager
from .capsules import CapsuleService, CapsuleNotFound, CapsuleDeliveryError
from .agent_runtimes import AgentRuntimeInventory
from .models import (
    SessionCreate, SessionHeartbeat, WorkerBind, WorkerHeartbeat, WorkerStatePatch,
    WorkSubmit, WorkFinish, WorkFail, WorkDetach, AlertAcknowledge, RetryDispatch, FaultInject, BatchDispatch,
    SessionReclaim, OAuthClientCreate, TunnelRegister, ManagedWorkspaceRegister, ManagedSessionCreate, ManagedSessionRename, ManagedSessionPermissionsUpdate, ManagedSessionMachineUpdate, MachineCapabilityRouteRequest, MachineEnrollmentApprove, MachineEnrollmentReject, DevicePairingStart, DevicePairingVerify, DevicePairingApprove, DevicePairingReject, DeviceRevoke,
    GraftConfigureRequest, GraftQueryRequest, GraftRollbackRequest, ComputerRepairRequest, ManagedGatewayAttach, GatewayContextUsageReport, SessionHandoffPrepareRequest,
    CapsuleCreate, CapsuleStageUpdate, CapsuleHandoff, CapsuleHandoffAck, CapsuleContractUpdate,
    CapsuleHandoffValidation, CapsuleHandoffApproval, CapsuleHandoffCompletion, LaneStateUpdate, CapsuleComplete,
)
from .settings import Settings, load_settings
from .workers import WorkerManager
from .scheduler import Scheduler
from .execution import ExecutionSupervisor
from .connectivity import ConnectivityManager
from .oauth import OAuthManager, OAuthError
from .operations import OperationsManager
from .observability import ObservabilityManager
from .computer import ComputerUseManager
from .graft import GraftManager, GraftError
from .integrations import IntegrationError, build_integration_manager
from .machine_registry import MachineRegistryError
from .node_registry import NodeRegistry
from .node_hardware import refresh_local_node_hardware
from .node_backends import refresh_local_node_backends
from .node_models import refresh_local_node_models
from .node_capabilities import refresh_local_node_capabilities
from .node_health import NodeHealthMonitor, mark_node_unavailable
from .node_observations import ingest_node_observations
from .machine_enrollment import MachineEnrollmentManager, MachineEnrollmentError
from .device_pairing import DevicePairingManager, DevicePairingError
from .device_pairing_web import pairing_policy_from_form, render_pairing_page
from .device_broker import DeviceBroker, DeviceBrokerError
from .machine_router import MachineCapabilityRouter
from .reflex_metrics import ReflexMetrics
from .action_adapter import evaluate_action_envelope
from .visual_reflex import VisualReflexError, evaluate_visual_reflex
from .target_collector import TargetCollectorError, VisualTargetCollector
from .action_registry import ActionProviderRegistryError, build_action_provider_registry
from .cognitive_router import CognitiveRouter, CognitiveRouterError
from .hermes_groups import HermesGroupClient
from .cognitive_observability import CognitiveObservability
from .pixel_art_studio import (
    PixelArtStudioError,
    PixelArtStudioProvider,
    VisualAssetOrchestrator,
)
from .world_authoring import WorldAuthoringError, WorldAuthoringManager
from .plugin_bundle import router as plugin_bundle_router


ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = os.environ.get("MCP_STUDIO_CONFIG", str(ROOT / "config.yaml"))
settings: Settings = load_settings(CONFIG_PATH)
db = Database(settings.studio.database)
health = HealthManager(settings, db)
workers = WorkerManager(settings, db)
herdr = HerdrManager(settings, db, health)
capsules = CapsuleService(
    db,
    herdr,
    local_api_base=f"http://127.0.0.1:{settings.studio.bind_port}",
)
agent_runtimes = AgentRuntimeInventory(herdr)
scheduler = Scheduler(settings, db, health, herdr)
execution = ExecutionSupervisor(settings, db, herdr, scheduler)
connectivity = ConnectivityManager(settings, db)
oauth = OAuthManager(settings, db)
managed_sessions = ManagedSessionManager(settings, db)
graft = GraftManager(settings, db)
earth_world_validator_url = (
    os.environ.get("HIRDA_EARTH_WORLD_VALIDATOR_URL")
    or str(getattr(settings.studio, "earth_world_validator_url", "") or "").strip()
    or None
)
pixel_art_studio = PixelArtStudioProvider.from_settings(
    settings.studio,
    base_dir=settings.config_path.parent,
)
integration_manager = build_integration_manager(
    graft,
    settings.studio,
    base_dir=settings.config_path.parent,
    pixel_art_studio=pixel_art_studio,
)
visual_assets = VisualAssetOrchestrator(
    pixel_art_studio,
    earth_validator_url=earth_world_validator_url,
)
world_authoring = WorldAuthoringManager(
    managed_sessions,
    validator_url=earth_world_validator_url,
)
computer = ComputerUseManager(settings.studio, db)
device_pairing = DevicePairingManager(
    settings.studio, base_dir=settings.config_path.parent
)
device_broker = DeviceBroker(settings.studio, device_pairing)
machine_registry = NodeRegistry(
    settings.studio,
    base_dir=settings.config_path.parent,
    device_pairing=device_pairing,
    device_broker=device_broker,
)
refresh_local_node_hardware(
    machine_registry, storage_path=settings.config_path.parent
)
refresh_local_node_backends(
    machine_registry, settings.studio.node_backend_probes
)
refresh_local_node_models(
    machine_registry, settings.studio.node_backend_probes
)
refresh_local_node_capabilities(
    machine_registry, settings.studio.node_backend_probes
)
node_health_monitor = NodeHealthMonitor(
    machine_registry,
    settings.studio.node_backend_probes,
    storage_path=settings.config_path.parent,
    interval_seconds=settings.studio.node_health_interval_seconds,
)
machine_enrollment = MachineEnrollmentManager(
    settings.studio, machine_registry, base_dir=settings.config_path.parent
)
machine_router = MachineCapabilityRouter(machine_registry, integration_manager, computer)
visual_target_collector = VisualTargetCollector(machine_registry)
gateway_sessions = GatewaySessionManager(
    settings,
    db,
    oauth,
    managed_sessions,
    graft,
    capsules,
    world_authoring,
    integration_manager,
    machine_router,
    machine_enrollment,
    herdr,
)
operations = OperationsManager(settings, db)
observability = ObservabilityManager(settings, db)
reflex_metrics = ReflexMetrics(settings.studio)
action_provider_registry = build_action_provider_registry(settings.studio)
hermes_groups = HermesGroupClient(settings.studio)
cognitive_router = CognitiveRouter(
    settings.studio,
    herdr,
    agent_runtimes,
    capsules,
    machine_router,
    hermes_groups,
)
cognitive_observability = CognitiveObservability(
    db,
    agent_runtimes,
    settings.studio,
)

_COGNITIVE_EXECUTE_IDEMPOTENCY_TTL_SECONDS = 300.0
_COGNITIVE_EXECUTE_IDEMPOTENCY_MAX_ENTRIES = 256
_cognitive_execute_idempotency_lock = asyncio.Lock()
_cognitive_execute_inflight: dict[str, tuple[str, asyncio.Task[dict[str, Any]]]] = {}
_cognitive_execute_completed: dict[str, tuple[float, str, dict[str, Any]]] = {}

templates = Jinja2Templates(directory=str(ROOT / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init()
    if oauth.enabled:
        canonical_peer_scope = gateway_sessions.oauth_peer_scope_id(oauth.resource)
        peer_scope_normalization = await db.normalize_oauth_peer_scope(
            canonical_peer_scope
        )
        if any(peer_scope_normalization.values()):
            await db.add_event(
                "peer.scope.normalized",
                "Normalized legacy OAuth peer scopes to the stable MCP resource realm.",
                data={
                    "canonical_peer_scope": canonical_peer_scope,
                    **peer_scope_normalization,
                },
            )
    # Integrations self-validate and self-certify at startup. Failures are
    # recorded in lifecycle state and never prevent the HIRDA control plane from starting.
    await integration_manager.snapshot(reconcile=True)
    await machine_enrollment.start()
    await node_health_monitor.start()
    await workers.start()
    await health.start()
    await herdr.start()
    await cognitive_router.start_runtime_recovery()
    await managed_sessions.start()
    await operations.start()
    await observability.start()
    await scheduler.start()
    await execution.start()
    await connectivity.start()
    await db.add_audit("studio.start", actor="system", data={"version": "0.9.10-m6.2.5", "production_mode": settings.studio.production_mode})
    yield
    await db.add_audit("studio.stop", actor="system", data={"version": "0.9.10-m6.2.5"})
    await cognitive_router.stop_runtime_recovery()
    await node_health_monitor.stop()
    await machine_enrollment.stop()
    await integration_manager.close()
    await connectivity.stop()
    await execution.stop()
    await scheduler.stop()
    await observability.stop()
    await operations.stop()
    await managed_sessions.stop()
    await computer.stop()
    await herdr.stop()
    await health.stop()
    await workers.stop()


app = FastAPI(title="MCP Studio", version="0.9.10", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")

_computer_novnc_dir = Path(settings.studio.computer_novnc_dir or "")
if _computer_novnc_dir.is_dir() and (_computer_novnc_dir / "vnc.html").is_file():
    app.mount(
        "/computer/novnc",
        StaticFiles(directory=str(_computer_novnc_dir)),
        name="computer-novnc",
    )


@app.middleware("http")
async def _novnc_no_cache(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith(("/computer/novnc/", "/static/")):
        # noVNC and HIRDA load version-coupled JS/CSS assets. Revalidating
        # avoids stale HTML/JS pairs that surface as generic connection errors
        # or keep an older Computer Use client alive after a backend fix.
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


app.include_router(make_gateway_router(settings, db, gateway_sessions))
app.include_router(plugin_bundle_router)


def _oauth_error_response(exc: OAuthError) -> JSONResponse:
    return JSONResponse(
        {"error": exc.error, "error_description": exc.description},
        status_code=exc.status_code,
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


@app.get("/.well-known/oauth-authorization-server")
async def oauth_authorization_server_metadata():
    if not settings.studio.oauth_enabled:
        raise HTTPException(status_code=404, detail="OAuth disabled")
    return JSONResponse(oauth.authorization_server_metadata(), headers={"Cache-Control": "no-store"})


@app.get("/.well-known/oauth-protected-resource")
async def oauth_protected_resource_metadata():
    if not settings.studio.oauth_enabled:
        raise HTTPException(status_code=404, detail="OAuth disabled")
    return JSONResponse(oauth.protected_resource_metadata(), headers={"Cache-Control": "no-store"})


@app.get("/.well-known/oauth-protected-resource/mcp/{server_id}")
async def oauth_protected_resource_metadata_server(server_id: str):
    if not settings.studio.oauth_enabled:
        raise HTTPException(status_code=404, detail="OAuth disabled")
    expected = settings.studio.oauth_resource_server_id or settings.studio.worker_server_id
    if server_id != expected:
        raise HTTPException(status_code=404, detail="Unknown protected resource")
    return JSONResponse(oauth.protected_resource_metadata(), headers={"Cache-Control": "no-store"})


@app.get("/oauth/authorize", response_class=HTMLResponse)
async def oauth_authorize(request: Request):
    if not settings.studio.oauth_enabled:
        raise HTTPException(status_code=404, detail="OAuth disabled")
    params = {k: v for k, v in request.query_params.items()}
    try:
        req = await oauth.validate_authorization_request(params)
    except OAuthError as exc:
        return HTMLResponse(
            f"<h1>OAuth request rejected</h1><p>{html.escape(exc.error)}: {html.escape(exc.description)}</p>",
            status_code=exc.status_code,
            headers={"Cache-Control": "no-store"},
        )
    hidden = "".join(
        f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(str(v), quote=True)}">'
        for k, v in params.items()
    )
    owner_session_valid = oauth.verify_owner_session(
        request.cookies.get("mcpstudio_owner_session", "")
    )
    if owner_session_valid:
        owner_approval_html = """
        <p class="small">Owner approval is remembered on this browser. Review the request and continue.</p>
        <button type="submit">Authorize</button>
        """
    else:
        owner_approval_html = """
        <p class="small">Enter the owner approval token stored locally on the MCP Studio host. The token is never sent to ChatGPT.</p>
        <label>Owner approval token</label><input type="password" name="owner_token" autocomplete="off" required>
        <button type="submit">Authorize</button>
        """
    page = f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MCP Studio authorization</title>
<style>
body{{font-family:system-ui,sans-serif;background:#111;color:#eee;max-width:720px;margin:48px auto;padding:0 20px}}
.card{{background:#1b1d20;border:1px solid #333;border-radius:16px;padding:24px}}code{{word-break:break-all}}
input[type=password]{{width:100%;box-sizing:border-box;padding:12px;margin:10px 0 16px;background:#0f1012;color:#fff;border:1px solid #444;border-radius:8px}}
button{{padding:12px 18px;border:0;border-radius:8px;font-weight:700;cursor:pointer}}
.small{{color:#aaa;font-size:14px}}
</style></head><body><div class="card">
<h1>Authorize ChatGPT → MCP Studio</h1>
<p><strong>Client:</strong> {html.escape(req['client']['client_name'])}</p>
<p><strong>Resource:</strong> <code>{html.escape(req['resource'])}</code></p>
<p><strong>Scopes:</strong> <code>{html.escape(req['scope'])}</code></p>
<form method="post" action="/oauth/authorize">{hidden}
{owner_approval_html}
</form>
</div></body></html>"""
    return HTMLResponse(
        page,
        headers={
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self' https://chatgpt.com; base-uri 'none'; frame-ancestors 'none'",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.post("/oauth/authorize")
async def oauth_authorize_submit(request: Request):
    if not settings.studio.oauth_enabled:
        raise HTTPException(status_code=404, detail="OAuth disabled")
    body = (await request.body()).decode("utf-8", errors="replace")
    parsed = parse_qs(body, keep_blank_values=True)
    params = {k: v[-1] for k, v in parsed.items() if v}
    owner_token = params.pop("owner_token", "")
    owner_session_token = request.cookies.get("mcpstudio_owner_session", "")
    approved_by_owner_token = oauth.verify_owner_token(owner_token)
    try:
        target = await oauth.approve_authorization(
            params,
            owner_token,
            owner_session_token=owner_session_token,
        )
    except OAuthError as exc:
        response = HTMLResponse(
            f"<h1>Authorization denied</h1><p>{html.escape(exc.error)}: {html.escape(exc.description)}</p>",
            status_code=exc.status_code,
            headers={"Cache-Control": "no-store"},
        )
        if owner_session_token and not oauth.verify_owner_session(owner_session_token):
            response.delete_cookie("mcpstudio_owner_session", path="/")
            response.delete_cookie("mcpstudio_owner_session", path="/oauth")
        return response
    await db.add_event(
        "oauth.authorization.approved",
        f"OAuth authorization approved for client {params.get('client_id', '')}",
        data={"client_id": params.get("client_id"), "scope": params.get("scope")},
    )
    response = RedirectResponse(target, status_code=303, headers={"Cache-Control": "no-store"})
    if approved_by_owner_token:
        response.set_cookie(
            "mcpstudio_owner_session",
            oauth.issue_owner_session(),
            max_age=settings.studio.oauth_owner_session_ttl_seconds,
            path="/",
            secure=True,
            httponly=True,
            samesite="lax",
        )
    return response


@app.post("/oauth/token")
async def oauth_token(request: Request):
    if not settings.studio.oauth_enabled:
        raise HTTPException(status_code=404, detail="OAuth disabled")
    body = (await request.body()).decode("utf-8", errors="replace")
    parsed = parse_qs(body, keep_blank_values=True)
    form = {k: v[-1] for k, v in parsed.items() if v}
    try:
        result = await oauth.token(form, request.headers.get("authorization", ""))
    except OAuthError as exc:
        await db.add_event(
            "oauth.token.failed",
            f"OAuth token request failed using {form.get('grant_type', '')}",
            severity="warning",
            data={"grant_type": form.get("grant_type"), "error": exc.error},
        )
        return _oauth_error_response(exc)
    await db.add_event(
        "oauth.token.issued",
        f"OAuth token issued using {form.get('grant_type', '')}",
        data={"grant_type": form.get("grant_type"), "client_id": form.get("client_id")},
    )
    return JSONResponse(
        result,
        headers={"Cache-Control": "no-store", "Pragma": "no-cache", "X-Content-Type-Options": "nosniff"},
    )


@app.get("/api/oauth/status")
async def oauth_status():
    clients = await db.list_oauth_clients() if settings.studio.oauth_enabled else []
    safe_clients = [
        {
            "client_id": x["client_id"],
            "client_name": x["client_name"],
            "redirect_uris": x["redirect_uris"],
            "scopes": x["scopes"],
            "token_endpoint_auth_method": x["token_endpoint_auth_method"],
            "enabled": x["enabled"],
        }
        for x in clients
    ]
    return {
        "enabled": settings.studio.oauth_enabled,
        "issuer": oauth.issuer if settings.studio.oauth_enabled else None,
        "resource": oauth.resource if settings.studio.oauth_enabled else None,
        "owner_token_configured": oauth.owner_token_configured() if settings.studio.oauth_enabled else False,
        "clients": safe_clients,
    }


@app.post("/api/oauth/owner-approval-tokens")
async def oauth_create_owner_approval_token(request: Request):
    if not settings.studio.oauth_enabled:
        raise HTTPException(status_code=404, detail="OAuth disabled")
    owner_session = request.cookies.get("mcpstudio_owner_session", "")
    root_token = request.headers.get("x-hirda-owner-approval", "")
    peer = request.client.host if request.client else ""
    local_admin = peer in {"127.0.0.1", "::1"}
    if not (
        local_admin
        or oauth.verify_owner_session(owner_session)
        or oauth.verify_owner_token(root_token)
    ):
        raise HTTPException(status_code=403, detail="Owner bootstrap approval required")
    result = await oauth.create_owner_approval_token(
        lifetime_seconds=600,
        purpose="owner_approval",
    )
    await db.add_event(
        "oauth.owner_approval_token.created",
        "One-time owner approval token created",
        data={
            "purpose": result["purpose"],
            "expires_at": result["expires_at"],
            "max_uses": result["max_uses"],
        },
    )
    return JSONResponse(
        result,
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


@app.post("/api/oauth/clients")
async def oauth_create_client(payload: OAuthClientCreate, request: Request):
    if not settings.studio.oauth_enabled:
        raise HTTPException(status_code=404, detail="OAuth disabled")
    owner_session = request.cookies.get("mcpstudio_owner_session", "")
    owner_token = request.headers.get("x-hirda-owner-approval", "")
    peer = request.client.host if request.client else ""
    local_admin = peer in {"127.0.0.1", "::1"}
    owner_session_valid = oauth.verify_owner_session(owner_session)
    owner_token_valid = False
    if owner_token and not owner_session_valid:
        owner_token_valid = await oauth.consume_owner_approval_token(
            owner_token, purpose="owner_approval"
        )
    if not local_admin and not owner_session_valid and not owner_token_valid:
        await db.add_event(
            "oauth.client.create_denied",
            "OAuth client creation denied: owner approval required",
            severity="warning",
            data={"client_name": payload.client_name},
        )
        raise HTTPException(status_code=403, detail="Owner approval required")
    try:
        item = await oauth.create_client(
            redirect_uris=[payload.redirect_uri.strip()],
            client_name=payload.client_name.strip(),
            token_endpoint_auth_method=payload.token_endpoint_auth_method,
            scopes=payload.scopes,
            issue_secret=payload.token_endpoint_auth_method != "none",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await db.add_event(
        "oauth.client.created",
        f"OAuth client created for {item['client_name']}",
        data={
            "client_id": item["client_id"],
            "client_name": item["client_name"],
            "redirect_uris": item["redirect_uris"],
            "scopes": item["scopes"],
            "token_endpoint_auth_method": item["token_endpoint_auth_method"],
        },
    )
    result = {
        "client_id": item["client_id"],
        "client_secret": item.get("client_secret"),
        "client_name": item["client_name"],
        "redirect_uris": item["redirect_uris"],
        "scopes": item["scopes"],
        "token_endpoint_auth_method": item["token_endpoint_auth_method"],
        "issuer": oauth.issuer,
        "resource": oauth.resource,
    }
    response = JSONResponse(
        result,
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )
    if owner_token_valid and not owner_session_valid:
        response.set_cookie(
            "mcpstudio_owner_session",
            oauth.issue_owner_session(),
            max_age=settings.studio.oauth_owner_session_ttl_seconds,
            path="/",
            secure=True,
            httponly=True,
            samesite="lax",
        )
    return response


@app.get("/healthz")
async def healthz():
    return {"ok": True, "version": "0.9.10-m6.2.5", "production_mode": settings.studio.production_mode}


@app.get("/readyz")
async def readyz():
    # Read-only readiness proof: database initialized and at least one configured
    # upstream has a health snapshot. It intentionally does not mutate runtime.
    try:
        await db.session_summary()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"database not ready: {exc}")
    if not health.snapshots:
        raise HTTPException(status_code=503, detail="upstream health not sampled yet")
    return {
        "ok": True,
        "version": "0.9.10-m6.2.5",
        "production_mode": settings.studio.production_mode,
        "servers": {k: v.status for k, v in health.snapshots.items()},
    }


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html", context={"request": request})


@app.get("/api/status")
async def api_status():
    snapshots = [x.model_dump(mode="json") for x in health.snapshots.values()]
    overall = "healthy"
    if any(x["status"] == "down" for x in snapshots):
        overall = "down"
    elif any(x["status"] == "degraded" for x in snapshots):
        overall = "degraded"
    return {
        "studio": {
            "status": "healthy",
            "production_mode": settings.studio.production_mode,
            "upstream_status": overall,
            "version": "0.9.10-m6.2.5",
            "gateway_enabled": settings.studio.gateway_enabled,
            "execution_enabled": settings.studio.execution_enabled,
            "execution_parallelism": settings.studio.execution_parallelism,
            "resilience_test_mode": settings.studio.resilience_test_mode,
            "connectivity_enabled": settings.studio.connectivity_enabled,
            "connectivity_auto_reconnect": settings.studio.connectivity_auto_reconnect,
            "session_stale_seconds": settings.studio.session_stale_seconds,
            "connectivity_test_mode": settings.studio.connectivity_test_mode,
            "gateway_allowed_servers": settings.studio.gateway_allowed_servers,
            "gateway_session_enabled": settings.studio.gateway_session_enabled,
            "gateway_session_auto_reconnect": settings.studio.gateway_session_auto_reconnect,
            "gateway_session_replay_on_404": settings.studio.gateway_session_replay_on_404,
            "gateway_session_test_mode": settings.studio.gateway_session_test_mode,
            "openai_compatibility_enabled": settings.studio.openai_compatibility_enabled,
            "openai_connector_reclaim_enabled": settings.studio.openai_connector_reclaim_enabled,
            "openai_local_ingress_enabled": settings.studio.openai_local_ingress_enabled,
            "openai_local_ingress_id": settings.studio.openai_local_ingress_id,
            "openai_local_ingress_path": f"/ingress/openai/{settings.studio.worker_server_id or 'serena-8001'}",
            "managed_session_enabled": settings.studio.managed_session_enabled,
            "managed_session_require_binding_for_tools": settings.studio.managed_session_require_binding_for_tools,
            "managed_session_cutover_enabled": settings.studio.managed_session_cutover_enabled,
            "managed_session_legacy_upstream_role": "discovery_only" if settings.studio.managed_session_cutover_enabled else "normal",
            "oauth_enabled": settings.studio.oauth_enabled,
            "oauth_issuer": settings.studio.oauth_issuer,
            "oauth_resource": oauth.resource if settings.studio.oauth_enabled else None,
            "oauth_owner_token_configured": oauth.owner_token_configured() if settings.studio.oauth_enabled else False,
            "operations_enabled": settings.studio.operations_enabled,
            "operations_interval_seconds": settings.studio.operations_interval_seconds,
            "cancel_pending_detach_seconds": settings.studio.cancel_pending_detach_seconds,
            "observability_enabled": settings.studio.observability_enabled,
            "observability_interval_seconds": settings.studio.observability_interval_seconds,
            "observability_window_minutes": settings.studio.observability_window_minutes,
            "config": str(settings.config_path),
        },
        "workers": await workers.summary(),
        "work": await db.work_summary(),
        "execution": execution.snapshot,
        "telemetry": await db.runtime_metrics(),
        "herdr": herdr.snapshot,
        "connectivity": connectivity.snapshot,
        "sessions": await db.session_summary(),
        "gateway_sessions": await db.gateway_session_summary(),
        "managed_sessions": {**(await managed_sessions.status()), "cutover": await db.managed_cutover_summary()},
        "tunnel_sessions": await db.tunnel_session_summary(),
        "operations": {**operations.snapshot, **(await db.operations_summary()), "schema": await db.schema_status()},
        "observability": await observability.report() if settings.studio.observability_enabled else {"overall_state": "disabled"},
        "integrations": await integration_manager.snapshot(),
        "servers": snapshots,
        "tunnels": await db.list_tunnels(),
    }


@app.get("/api/reflex/metrics")
async def reflex_metrics_status(window: str = "24h", recent: int = 20):
    return reflex_metrics.snapshot(window=window, recent=recent)


@app.get("/api/integrations")
async def integration_registry_status(reconcile: bool = False):
    return await integration_manager.snapshot(reconcile=reconcile)


@app.get("/api/integrations/{integration_id}")
async def integration_detail(integration_id: str, reconcile: bool = False):
    try:
        return await integration_manager.detail(integration_id, reconcile=reconcile)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Unknown integration") from exc


@app.post("/api/integrations/{integration_id}/reconcile")
async def integration_reconcile(integration_id: str):
    try:
        return await integration_manager.reconcile(integration_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Unknown integration") from exc


def _require_loopback_control(request: Request) -> None:
    host = str(request.client.host if request.client else "").strip()
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Not Found") from exc
    if not address.is_loopback:
        raise HTTPException(status_code=404, detail="Not Found")


@app.post("/api/openbrowser/local-control")
async def openbrowser_local_control(request: Request, payload: dict[str, Any]):
    """Loopback-only HIRDA gate for bounded existing-CDP browser control.

    This endpoint intentionally exposes only the small OpenBrowser subset needed
    by local control-plane services. Request bodies are never persisted or
    audited, which allows sensitive input_text values to remain ephemeral.
    """
    _require_loopback_control(request)
    owner_session = str(request.headers.get("X-HIRDA-Owner-Session") or "").strip()
    if not oauth.verify_owner_session(owner_session):
        raise HTTPException(status_code=403, detail="owner_session_required")
    action = str(payload.get("action") or "").strip()
    if action not in {"attach_session", "state", "navigate", "click_xy", "input_text"}:
        raise HTTPException(status_code=400, detail="unsupported_openbrowser_action")

    managed_session_id = str(payload.get("managed_session_id") or "").strip()
    if not managed_session_id or len(managed_session_id) > 160:
        raise HTTPException(status_code=400, detail="managed_session_id_required")

    arguments = payload.get("arguments")
    if not isinstance(arguments, dict):
        arguments = {}

    context: dict[str, Any] = {"managed_session_id": managed_session_id}
    if action == "attach_session":
        raw_port = payload.get("cdp_port")
        if isinstance(raw_port, bool) or not isinstance(raw_port, int) or not 1 <= raw_port <= 65535:
            raise HTTPException(status_code=400, detail="valid_cdp_port_required")
        context["cdp_port"] = raw_port

    call_arguments = dict(arguments)
    try:
        result = await integration_manager.call_backend_tool(
            f"hirda__openbrowser__{action}",
            call_arguments,
            context=context,
        )
        return JSONResponse(
            result,
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )
    except (IntegrationError, RuntimeError, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail=f"openbrowser_control_failed:{type(exc).__name__}",
        ) from exc
    finally:
        if action == "input_text":
            call_arguments["text"] = ""
            if isinstance(arguments, dict) and "text" in arguments:
                arguments["text"] = ""
            payload["arguments"] = {}


def _pstack_activity_adapter():
    try:
        registration = integration_manager.get("pstack")
    except KeyError as exc:
        raise HTTPException(status_code=503, detail="Pstack integration is not active") from exc
    adapter = registration.adapter
    if not hasattr(adapter, "activity_snapshot"):
        raise HTTPException(
            status_code=503,
            detail="Pstack activity observability is unavailable",
        )
    return adapter


async def _pstack_activity_snapshot(
    *,
    since: int = 0,
    limit: int = 100,
) -> dict[str, Any]:
    opened_rows = await db.list_events(
        kind_prefix="pstack.task.opened",
        limit=5000,
        ascending=True,
    )
    lifecycle_rows = await db.list_events(
        kind_prefix="pstack.lifecycle.checkpoint",
        limit=50000,
        ascending=True,
    )
    hirda_rows = await db.list_events(
        kind_prefix="pstack.hirda_",
        limit=50000,
        ascending=True,
    )
    herdr_rows = await db.list_events(
        kind_prefix="pstack.herdr_tab.",
        limit=50000,
        ascending=True,
    )
    recent_rows = await db.list_events(
        kind_prefix="pstack.",
        limit=5000,
        ascending=False,
    )
    rows_by_key: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in [
        *opened_rows,
        *lifecycle_rows,
        *hirda_rows,
        *herdr_rows,
        *recent_rows,
    ]:
        row_id = row.get("id")
        key = (
            ("id", row_id)
            if row_id is not None
            else (
                "fallback",
                row.get("created_at"),
                row.get("kind"),
                row.get("correlation_id"),
                row.get("message"),
            )
        )
        rows_by_key[key] = row
    rows = sorted(
        rows_by_key.values(),
        key=lambda row: (
            str(row.get("created_at") or ""),
            int(row.get("id") or 0),
        ),
    )
    traces: dict[str, dict[str, Any]] = {}
    persistent_events: list[dict[str, Any]] = []
    seen_event_keys: set[tuple[Any, ...]] = set()

    def event_key(event: dict[str, Any]) -> tuple[Any, ...]:
        trace_key = str(event.get("trace_id") or "").strip()
        event_metadata = event.get("metadata")
        event_metadata = event_metadata if isinstance(event_metadata, dict) else {}
        event_id = str(
            event.get("activity_event_id")
            or event_metadata.get("activity_event_id")
            or ""
        ).strip()
        if event_id:
            return trace_key, event_id
        return (
            trace_key,
            str(event.get("actor") or ""),
            str(event.get("phase") or ""),
            str(event.get("title") or ""),
            str(event.get("detail") or ""),
            str(event.get("status") or ""),
        )

    for row in rows:
        data = dict(row.get("data") or {})
        trace_id = str(data.get("trace_id") or "").strip()
        if not trace_id:
            continue
        timestamp = str(data.get("activity_timestamp") or row.get("created_at") or "")
        event = {
            "schema": "hirda-pstack-activity-event-v1",
            "sequence": 0,
            "trace_id": trace_id,
            "timestamp": timestamp,
            "actor": str(data.get("actor") or "hirda"),
            "phase": str(data.get("phase") or row.get("kind") or "event"),
            "title": str(data.get("title") or row.get("message") or "Pstack event"),
            "detail": str(data.get("detail") or row.get("message") or ""),
            "status": str(data.get("status") or "passed"),
            "workflow": data.get("workflow"),
            "role": data.get("role"),
            "tool": data.get("tool"),
            "runtime_authority": "hirda",
            "metadata": dict(data.get("metadata") or {}),
            "activity_event_id": data.get("activity_event_id"),
        }
        persistent_events.append(event)
        seen_event_keys.add(event_key(event))

        trace = traces.setdefault(
            trace_id,
            {
                "trace_id": trace_id,
                "task_id": data.get("task_id"),
                "tool": data.get("tool") or "route_task",
                "goal": str(data.get("goal") or ""),
                "workflow": data.get("workflow"),
                "role": data.get("role"),
                "status": "active",
                "current_step": None,
                "lifecycle": (
                    dict(data.get("lifecycle") or {})
                    if isinstance(data.get("lifecycle"), dict)
                    else {}
                ),
                "started_at": timestamp,
                "updated_at": timestamp,
                "identity": {
                    key: value
                    for key, value in {
                        "task_id": data.get("task_id"),
                        "workspace_key": data.get("workspace_key"),
                        "managed_session_id": data.get("managed_session_id"),
                        "gateway_session_id": data.get("gateway_session_id"),
                    }.items()
                    if value
                },
            },
        )
        if data.get("goal"):
            trace["goal"] = str(data.get("goal"))
        if data.get("workflow"):
            trace["workflow"] = data.get("workflow")
        if data.get("role"):
            trace["role"] = data.get("role")
        if data.get("tool"):
            trace["tool"] = data.get("tool")
        if data.get("task_id"):
            trace["task_id"] = data.get("task_id")
        if isinstance(data.get("lifecycle"), dict):
            trace["lifecycle"] = dict(data.get("lifecycle") or {})
        identity_fields = {
            key: data.get(key)
            for key in (
                "task_id",
                "request_id",
                "workspace_key",
                "managed_session_id",
                "gateway_session_id",
            )
            if data.get(key)
        }
        if identity_fields:
            trace["identity"] = {**dict(trace.get("identity") or {}), **identity_fields}
        row_kind = str(row.get("kind") or "").casefold()
        if row_kind in {
            "pstack.herdr_tab.opened",
            "pstack.herdr_tab.closed",
            "pstack.herdr_tab.open_failed",
            "pstack.herdr_tab.close_failed",
        }:
            tab_status = {
                "pstack.herdr_tab.opened": "open",
                "pstack.herdr_tab.closed": "closed",
                "pstack.herdr_tab.open_failed": "unavailable",
                "pstack.herdr_tab.close_failed": "close_failed",
            }[row_kind]
            trace["herdr_tab"] = {
                **dict(trace.get("herdr_tab") or {}),
                "status": tab_status,
                "tab_id": data.get("tab_id"),
                "label": data.get("tab_label"),
                "workspace_id": data.get("herdr_workspace_id"),
                "persistent": False,
                "owner": "pstack",
                "updated_at": timestamp,
            }
        trace["updated_at"] = timestamp
        trace["current_step"] = {
            "phase": event["phase"],
            "title": event["title"],
            "status": event["status"],
            "actor": event["actor"],
        }
        if data.get("trace_status"):
            trace["status"] = str(data.get("trace_status"))
        elif event["status"] == "failed":
            trace["status"] = "failed"

    try:
        adapter = _pstack_activity_adapter()
        memory = adapter.activity_snapshot(since=0, limit=250)
    except HTTPException:
        memory = {
            "traces": [],
            "events": [],
        }

    for trace in memory.get("traces") or []:
        trace_id = str(trace.get("trace_id") or "").strip()
        if not trace_id:
            continue
        current = traces.get(trace_id)
        if current is None:
            traces[trace_id] = dict(trace)
            continue
        memory_updated = str(trace.get("updated_at") or trace.get("started_at") or "")
        current_updated = str(current.get("updated_at") or current.get("started_at") or "")
        if memory_updated > current_updated:
            current.update({
                key: trace[key]
                for key in ("status", "current_step", "updated_at", "lifecycle")
                if key in trace
            })

    for event in memory.get("events") or []:
        trace_id = str(event.get("trace_id") or "").strip()
        if not trace_id:
            continue
        memory_event = dict(event)
        key = event_key(memory_event)
        if key not in seen_event_keys:
            persistent_events.append(memory_event)
            seen_event_keys.add(key)
        trace = traces.get(trace_id)
        if trace is not None:
            phase = str(memory_event.get("phase") or "").casefold()
            event_status = str(memory_event.get("status") or "").casefold()
            memory_timestamp = str(memory_event.get("timestamp") or "")
            current_timestamp = str(trace.get("updated_at") or trace.get("started_at") or "")
            if not current_timestamp or memory_timestamp > current_timestamp:
                if phase == "hirda_execution_started" or phase == "tool_started":
                    trace["status"] = "executing"
                elif phase == "hirda_execution_completed":
                    trace["status"] = "completed"
                elif phase == "hirda_execution_failed" or event_status == "failed":
                    trace["status"] = "failed"
                trace["updated_at"] = memory_timestamp or current_timestamp
                trace["current_step"] = {
                    "phase": memory_event.get("phase"),
                    "title": memory_event.get("title"),
                    "status": memory_event.get("status"),
                    "actor": memory_event.get("actor"),
                }

    canonical_rows = list(await db.list_sessions(limit=500))
    for lister_name in ("list_gateway_sessions", "list_managed_sessions"):
        lister = getattr(db, lister_name, None)
        if callable(lister):
            canonical_rows.extend(await lister(limit=500))

    canonical_tasks: dict[str, tuple[tuple[int, float, str], dict[str, Any]]] = {}
    for logical in canonical_rows:
        metadata = logical.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        task = metadata.get("active_pstack_task")
        task = dict(task) if isinstance(task, dict) else {}
        trace_id = str(task.get("trace_id") or "").strip()
        if not trace_id:
            continue
        lifecycle = task.get("lifecycle")
        lifecycle = lifecycle if isinstance(lifecycle, dict) else {}
        score = (
            int(lifecycle.get("revision") or 0),
            float(lifecycle.get("updated_at") or task.get("opened_at") or 0.0),
            str(logical.get("last_seen_at") or ""),
        )
        current = canonical_tasks.get(trace_id)
        if current is None or score > current[0]:
            canonical_tasks[trace_id] = (score, task)

    for trace_id, (_, task) in canonical_tasks.items():
        lifecycle = task.get("lifecycle")
        lifecycle = dict(lifecycle) if isinstance(lifecycle, dict) else {}
        lifecycle_updated = lifecycle.get("updated_at")
        task_updated_at = ""
        if isinstance(lifecycle_updated, (int, float)) and lifecycle_updated > 0:
            task_updated_at = datetime.fromtimestamp(
                float(lifecycle_updated),
                timezone.utc,
            ).isoformat()
        trace = traces.setdefault(
            trace_id,
            {
                "trace_id": trace_id,
                "task_id": task.get("task_id"),
                "tool": "route_task",
                "goal": str(task.get("goal") or ""),
                "workflow": task.get("workflow"),
                "role": None,
                "status": str(task.get("status") or "active"),
                "current_step": None,
                "lifecycle": lifecycle,
                "started_at": task_updated_at,
                "updated_at": task_updated_at,
                "identity": {},
            },
        )
        for key in ("task_id", "goal", "workflow"):
            if task.get(key):
                trace[key] = task.get(key)
        if lifecycle:
            trace["lifecycle"] = lifecycle
        task_status = str(task.get("status") or "").strip()
        if task_status:
            trace["status"] = task_status
        herdr_tab = task.get("herdr_tab")
        if isinstance(herdr_tab, dict) and herdr_tab:
            trace["herdr_tab"] = dict(herdr_tab)
        identity_fields = {
            key: task.get(key)
            for key in (
                "task_id",
                "workspace_key",
                "managed_session_id",
                "gateway_session_id",
            )
            if task.get(key)
        }
        if identity_fields:
            trace["identity"] = {
                **dict(trace.get("identity") or {}),
                **identity_fields,
            }
        if task_updated_at and task_updated_at >= str(trace.get("updated_at") or ""):
            trace["updated_at"] = task_updated_at

    canonical_trace_ids = set(canonical_tasks)
    terminal_trace_states = {"completed", "expired", "failed", "blocked"}
    for trace_id, trace in traces.items():
        if trace_id in canonical_trace_ids:
            continue
        identity = trace.get("identity")
        identity = identity if isinstance(identity, dict) else {}
        has_canonical_owner = bool(
            identity.get("managed_session_id") or identity.get("gateway_session_id")
        )
        lifecycle = trace.get("lifecycle")
        lifecycle = lifecycle if isinstance(lifecycle, dict) else {}
        trace_status = str(trace.get("status") or "").casefold()
        lifecycle_status = str(lifecycle.get("status") or "").casefold()
        if (
            has_canonical_owner
            and trace_status not in terminal_trace_states
            and lifecycle_status not in {"completed", "blocked"}
        ):
            trace["status"] = "expired"
            trace["terminal_disposition"] = "expired"
            trace["projection_reason"] = "canonical_owner_missing"
            if lifecycle:
                lifecycle = dict(lifecycle)
                lifecycle["status"] = "completed"
                lifecycle["current_phase"] = "done"
                lifecycle["terminal_disposition"] = "expired"
                trace["lifecycle"] = lifecycle

    for trace in traces.values():
        lifecycle = trace.get("lifecycle")
        if not isinstance(lifecycle, dict) or not lifecycle:
            continue
        lifecycle_status = str(lifecycle.get("status") or "").casefold()
        current_phase = str(lifecycle.get("current_phase") or "").casefold()
        trace_status = str(trace.get("status") or "").casefold()
        terminal_disposition = str(
            lifecycle.get("terminal_disposition")
            or trace.get("terminal_disposition")
            or ""
        ).casefold()
        if trace_status == "expired" or terminal_disposition == "expired":
            trace["status"] = "expired"
        elif lifecycle_status == "completed":
            trace["status"] = "completed"
        elif lifecycle_status == "blocked":
            trace["status"] = "blocked"
        elif lifecycle_status == "active" and trace_status != "failed":
            trace["status"] = "active"
        if current_phase:
            stages = lifecycle.get("stages")
            stages = stages if isinstance(stages, list) else []
            current_stage = next(
                (
                    stage
                    for stage in stages
                    if isinstance(stage, dict)
                    and str(stage.get("phase") or "").casefold() == current_phase
                ),
                {},
            )
            trace["current_step"] = {
                "phase": current_phase,
                "title": current_phase.replace("_", " ").title(),
                "status": str(current_stage.get("status") or lifecycle_status or "active"),
                "actor": "hirda",
            }

    persistent_events.sort(
        key=lambda item: (
            str(item.get("timestamp") or ""),
            str(item.get("trace_id") or ""),
        )
    )
    for index, event in enumerate(persistent_events, start=1):
        event["sequence"] = index

    ordered_traces = sorted(
        traces.values(),
        key=lambda item: str(item.get("updated_at") or item.get("started_at") or ""),
        reverse=True,
    )
    terminal_trace_states = {"completed", "expired", "failed", "blocked"}
    active_trace = next(
        (
            item
            for item in ordered_traces
            if str(item.get("status") or "").casefold() not in terminal_trace_states
            and str((item.get("lifecycle") or {}).get("status") or "").casefold()
            not in {"completed", "blocked"}
        ),
        None,
    )
    cursor = max(0, int(since or 0))
    bounded_limit = max(1, min(int(limit or 100), 500))
    visible_events = [
        event
        for event in persistent_events
        if int(event.get("sequence") or 0) > cursor
    ][-bounded_limit:]
    return {
        "schema": "hirda-pstack-activity-v2",
        "available": True,
        "persistent": True,
        "runtime_authority": "hirda",
        "model_selection": "deferred_to_hirda",
        "latest_sequence": len(persistent_events),
        "event_count": len(persistent_events),
        "trace_count": len(ordered_traces),
        "active_trace": dict(active_trace) if active_trace else None,
        "traces": [dict(item) for item in ordered_traces[:100]],
        "events": visible_events,
    }


@app.get("/api/pstack/activity")
async def pstack_activity_status(since: int = 0, limit: int = 100):
    return await _pstack_activity_snapshot(
        since=max(0, since),
        limit=limit,
    )


@app.get("/api/pstack/activity/stream")
async def pstack_activity_stream(request: Request, since: int = 0):
    async def activity_events():
        cursor = max(0, since)
        first = True
        while True:
            if await request.is_disconnected():
                break
            snapshot = await _pstack_activity_snapshot(
                since=0,
                limit=200,
            )
            latest = int(snapshot.get("latest_sequence") or 0)
            if first or latest > cursor:
                cursor = latest
                payload = json.dumps(
                    snapshot,
                    separators=(",", ":"),
                    ensure_ascii=False,
                )
                yield f"id: {cursor}\nevent: snapshot\ndata: {payload}\n\n"
                first = False
            else:
                yield ": keepalive\n\n"
            await asyncio.sleep(1.0)

    return StreamingResponse(
        activity_events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


async def _prepare_pstack_cognitive_payload(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    prepared = dict(payload or {})
    context = prepared.get("context")
    if context is None:
        context = {}
    if not isinstance(context, dict):
        return prepared, None

    capability = str(prepared.get("capability") or "reflection").strip().casefold()
    workflow_provider = str(context.get("workflow_provider") or "").strip().casefold()
    if bool(context.get("pstack_disabled")):
        return prepared, None
    if bool(context.get("pstack_methodology_applied")):
        return prepared, {
            "applied": True,
            "trace_id": context.get("pstack_trace_id"),
            "workflow": context.get("workflow"),
            "source": context.get("pstack_source"),
            "reused": True,
            "context": {
                "task_id": prepared.get("request_id"),
                "workspace_key": context.get("workspace_key"),
                "managed_session_id": context.get("managed_session_id"),
                "gateway_session_id": context.get("gateway_session_id"),
                "goal": str(prepared.get("prompt") or ""),
            },
        }
    if capability != "coding" and workflow_provider != "pstack":
        return prepared, None

    goal = str(prepared.get("prompt") or "").strip()
    if not goal:
        return prepared, None

    try:
        registration = integration_manager.get("pstack")
        if registration.stage != "ready":
            await integration_manager.reconcile("pstack")
        arguments: dict[str, Any] = {"goal": goal, "workflow_hint": "engineering_task"}
        requested_workflow = str(context.get("workflow") or "").strip().casefold()
        if requested_workflow in {"bug-fix", "feature", "review"}:
            arguments["workflow"] = requested_workflow

        call_context = {
            "task_id": str(prepared.get("request_id") or "").strip() or None,
            "managed_session_id": context.get("managed_session_id"),
            "capsule_id": context.get("capsule_id"),
            "turn_id": context.get("turn_id"),
            "workspace_key": context.get("workspace_key"),
            "gateway_session_id": context.get("gateway_session_id"),
        }
        call_context = {key: value for key, value in call_context.items() if value}

        routed = await integration_manager.call_backend_tool(
            "hirda__pstack__route_task",
            arguments,
            context=call_context,
        )
    except (IntegrationError, KeyError, RuntimeError) as exc:
        prepared["context"] = {
            **context,
            "pstack_methodology_applied": False,
            "pstack_methodology_error": f"{type(exc).__name__}: {exc}",
        }
        return prepared, {
            "applied": False,
            "error": f"{type(exc).__name__}: {exc}",
        }

    coordinator = dict(routed.get("coordinator") or {})
    hirda_request = dict(coordinator.get("hirda_request") or {})
    pstack_context = dict(hirda_request.get("context") or {})
    trace_id = str(routed.get("trace_id") or pstack_context.get("pstack_trace_id") or "").strip()
    source = dict(routed.get("source") or {})
    workflow = str(routed.get("workflow") or pstack_context.get("workflow") or "").strip() or None

    merged_context = {
        **context,
        **pstack_context,
        "pstack_methodology_applied": True,
        "pstack_trace_id": trace_id or None,
        "pstack_source": source,
    }
    prepared["context"] = merged_context
    methodology_prompt = str(hirda_request.get("prompt") or "").strip()
    if methodology_prompt:
        prepared["prompt"] = methodology_prompt

    return prepared, {
        "applied": True,
        "trace_id": trace_id or None,
        "workflow": workflow,
        "source": source,
        "runtime_authority": "hirda",
        "model_selection": "deferred_to_hirda",
        "reused": False,
        "context": {
            "task_id": prepared.get("request_id"),
            "workspace_key": context.get("workspace_key"),
            "managed_session_id": context.get("managed_session_id"),
            "gateway_session_id": context.get("gateway_session_id"),
            "goal": goal,
        },
    }


async def _record_pstack_hirda_event(
    pstack: dict[str, Any] | None,
    *,
    phase: str,
    title: str,
    detail: str,
    status: str,
    trace_status: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    trace_id = str((pstack or {}).get("trace_id") or "").strip()
    if not trace_id:
        return
    event_id = uuid4().hex
    event_metadata = {**dict(metadata or {}), "activity_event_id": event_id}
    event_timestamp = datetime.now(timezone.utc).isoformat()
    try:
        adapter = _pstack_activity_adapter()
        recorder = getattr(adapter, "record_external_event", None)
        if callable(recorder):
            activity_event = recorder(
                trace_id,
                actor="hirda",
                phase=phase,
                title=title,
                detail=detail,
                status=status,
                trace_status=trace_status,
                metadata=event_metadata,
            )
            if isinstance(activity_event, dict) and activity_event.get("timestamp"):
                event_timestamp = str(activity_event["timestamp"])
    except Exception:
        pass
    context = dict((pstack or {}).get("context") or {})
    event_data = {
        "trace_id": trace_id,
        "request_id": context.get("task_id"),
        "workspace_key": context.get("workspace_key"),
        "managed_session_id": context.get("managed_session_id"),
        "gateway_session_id": context.get("gateway_session_id"),
        "goal": context.get("goal"),
        "workflow": (pstack or {}).get("workflow"),
        "actor": "hirda",
        "phase": phase,
        "title": title,
        "detail": detail,
        "status": status,
        "trace_status": trace_status,
        "metadata": event_metadata,
        "activity_event_id": event_id,
        "activity_timestamp": event_timestamp,
    }
    await db.add_event(
        f"pstack.{phase}",
        title,
        severity="warning" if status == "failed" else "info",
        data=event_data,
        stream_id=f"pstack:{trace_id}",
        correlation_id=trace_id,
    )
    task_status = {
        "executing": "active",
        "completed": "completed",
        "failed": "failed",
        "blocked": "blocked",
    }.get(str(trace_status or "").casefold())
    if task_status:
        gateway_session_id = str(context.get("gateway_session_id") or "").strip()
        if not gateway_session_id:
            try:
                opened = await db.list_events(
                    kind_prefix="pstack.task.opened",
                    correlation_id=trace_id,
                    limit=1,
                    ascending=False,
                )
                if opened:
                    gateway_session_id = str(
                        (opened[0].get("data") or {}).get("gateway_session_id") or ""
                    ).strip()
            except Exception:
                gateway_session_id = ""
        if gateway_session_id:
            try:
                gateway = await db.get_gateway_session(gateway_session_id)
                task = dict((gateway.get("metadata") or {}).get("active_pstack_task") or {})
                if str(task.get("trace_id") or "").strip() == trace_id:
                    task["status"] = task_status
                    await db.update_gateway_session_metadata(
                        gateway_session_id,
                        {"active_pstack_task": task},
                    )
                    studio_session_id = str(gateway.get("studio_session_id") or "").strip()
                    if studio_session_id:
                        await db.heartbeat_session(
                            studio_session_id,
                            {"metadata": {"active_pstack_task": task}},
                        )
            except Exception:
                # The durable trace event remains authoritative if metadata is unavailable.
                pass


@app.get("/api/action-adapter/registry")
async def action_adapter_registry_status():
    return await action_provider_registry.snapshot()


@app.get("/api/action-adapter/providers")
async def action_adapter_providers():
    return await action_provider_registry.snapshot()


@app.get("/api/action-adapter/providers/{provider_id}")
async def action_adapter_provider(provider_id: str):
    try:
        return await action_provider_registry.provider_detail(provider_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Unknown action provider") from exc
    except ActionProviderRegistryError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/action-adapter/providers/{provider_id}/evaluate")
async def action_adapter_evaluate(provider_id: str):
    try:
        registration = action_provider_registry.get(provider_id)
        envelope = await action_provider_registry.build_envelope(provider_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Unknown action provider") from exc
    except ActionProviderRegistryError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    judgment = await evaluate_action_envelope(settings.studio, envelope)
    return {
        "schema": "hirda-action-playground-result-v1",
        "provider": registration.summary(action_count=len(envelope.actions)),
        "envelope": envelope.as_dict(),
        "judgment": judgment.as_dict(),
        "executor_attached": False,
        "execution_performed": False,
    }


@app.post("/api/visual-reflex/evaluate")
async def visual_reflex_evaluate(request: Request):
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Request body must be valid JSON") from exc
    try:
        return await evaluate_visual_reflex(settings.studio, payload)
    except VisualReflexError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/visual-reflex/collect-evaluate")
async def visual_reflex_collect_evaluate(request: Request):
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Request body must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Request body must be an object")

    try:
        max_targets = int(payload.get("max_targets", 32))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="max_targets must be an integer") from exc

    try:
        collection = await visual_target_collector.collect(
            machine_id=str(payload.get("machine_id") or ""),
            request_id=str(payload.get("request_id") or ""),
            goal=str(payload.get("goal") or ""),
            workspace_key=str(payload.get("workspace_key") or "mcp-studio"),
            session_id=str(
                payload.get("session_id") or "visual-reflex-target-collector"
            ),
            max_targets=max_targets,
        )
        result = await evaluate_visual_reflex(settings.studio, collection.request)
    except (TargetCollectorError, VisualReflexError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        **result,
        "collector": collection.summary(),
    }


@app.get("/api/cognition/status")
async def cognitive_router_status():
    runtime_backoff = await cognitive_router.runtime_backoff_status()
    snapshot = cognitive_router.decorate_runtime_snapshot_with_backoff(
        agent_runtimes.snapshot(force=False),
        runtime_backoff,
    )
    return {
        "schema": "hirda-cognitive-status-v1",
        "enabled": bool(settings.studio.cognitive_router_enabled),
        "execute_enabled": bool(settings.studio.cognitive_router_execute_enabled),
        "semantic_routing": {
            "mode": settings.studio.cognitive_semantic_routing_mode,
            "runtime_capability_override_count": len(
                settings.studio.cognitive_runtime_capability_overrides
            ),
        },
        "supervisor_policy": {
            "mode": settings.studio.cognitive_supervisor_policy_mode,
            "runtime": settings.studio.cognitive_supervisor_runtime,
            "semantic_authority": "unchanged",
        },
        "team_delegation": {
            "mode": settings.studio.cognitive_hermes_team_delegation_mode,
            "topology": "claude>hermes>bots>hermes>claude",
            "require_opt_in": bool(
                settings.studio.cognitive_hermes_team_require_opt_in
            ),
            "profiles": dict(settings.studio.cognitive_hermes_team_profiles),
            "timeout_seconds": settings.studio.cognitive_hermes_team_timeout_seconds,
            "group_runtime": hermes_groups.status(),
            "execution_authority": (
                "hirda_canary"
                if settings.studio.cognitive_hermes_team_delegation_mode == "canary"
                else "external"
            ),
        },
        "node_routing": {
            "mode": settings.studio.cognitive_node_routing_mode,
            "capability_mapping_count": len(
                settings.studio.cognitive_capability_routes
            ),
            "runtime_binding_count": len(
                settings.studio.cognitive_runtime_bindings
            ),
        },
        "pane_routing": cognitive_router.pane_routing_status(),
        "runtime_backoff": runtime_backoff,
        "runtimes": snapshot,
        "identity_authority": "external",
        "semantic_authority": "external",
    }


@app.post("/api/cognition/plan")
async def cognitive_router_plan(payload: dict[str, Any]):
    if not settings.studio.cognitive_router_enabled:
        raise HTTPException(status_code=503, detail="HIRDA cognitive router is disabled")
    started = time.perf_counter()
    prepared_payload, pstack = await _prepare_pstack_cognitive_payload(payload)
    try:
        plan = await cognitive_router.plan(prepared_payload)
        if pstack and pstack.get("applied"):
            candidates = list(plan.get("candidates") or [])
            selected = candidates[0] if candidates else {}
            await _record_pstack_hirda_event(
                pstack,
                phase="hirda_plan_ready",
                title="HIRDA plan ready",
                detail=(
                    "HIRDA evaluated the Pstack methodology request and prepared "
                    "runtime candidates under semantic routing."
                ),
                status="passed",
                trace_status="planned",
                metadata={
                    "lane": plan.get("lane"),
                    "runtime": selected.get("runtime"),
                    "provider": selected.get("provider"),
                    "model": selected.get("model"),
                    "semantic_routing_applied": bool(
                        (plan.get("semantic_routing") or {}).get("applied")
                    ),
                },
            )
            plan["pstack_methodology"] = dict(pstack)
        await cognitive_observability.record_plan(
            prepared_payload,
            plan,
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
        )
        return plan
    except CognitiveRouterError as exc:
        await _record_pstack_hirda_event(
            pstack,
            phase="hirda_plan_failed",
            title="HIRDA planning failed",
            detail=str(exc),
            status="failed",
            trace_status="failed",
        )
        await cognitive_observability.record_error(
            "plan",
            prepared_payload,
            exc,
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        await _record_pstack_hirda_event(
            pstack,
            phase="hirda_plan_failed",
            title="HIRDA planning failed",
            detail=f"{type(exc).__name__}: {exc}",
            status="failed",
            trace_status="failed",
        )
        await cognitive_observability.record_error(
            "plan",
            prepared_payload,
            exc,
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
        )
        raise


def _cognitive_execute_fingerprint(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _prune_cognitive_execute_cache(now: float) -> None:
    expired = [
        request_id
        for request_id, (stored_at, _fingerprint, _result) in
        _cognitive_execute_completed.items()
        if now - stored_at > _COGNITIVE_EXECUTE_IDEMPOTENCY_TTL_SECONDS
    ]
    for request_id in expired:
        _cognitive_execute_completed.pop(request_id, None)

    overflow = len(_cognitive_execute_completed) - _COGNITIVE_EXECUTE_IDEMPOTENCY_MAX_ENTRIES
    if overflow > 0:
        oldest = sorted(
            _cognitive_execute_completed.items(),
            key=lambda item: item[1][0],
        )[:overflow]
        for request_id, _entry in oldest:
            _cognitive_execute_completed.pop(request_id, None)


async def _execute_cognitive_request_once(
    payload: dict[str, Any],
) -> dict[str, Any]:
    started = time.perf_counter()
    prepared_payload, pstack = await _prepare_pstack_cognitive_payload(payload)
    if pstack and pstack.get("applied"):
        await _record_pstack_hirda_event(
            pstack,
            phase="hirda_execution_started",
            title="HIRDA execution started",
            detail=(
                "HIRDA accepted the methodology handoff and started runtime "
                "selection and execution."
            ),
            status="running",
            trace_status="executing",
        )
    try:
        result = (await cognitive_router.execute(prepared_payload)).as_dict()
        if pstack and pstack.get("applied"):
            result["pstack_methodology"] = dict(pstack)
            await _record_pstack_hirda_event(
                pstack,
                phase="hirda_execution_completed",
                title="HIRDA execution completed",
                detail=(
                    f"Execution finished with status {result.get('status') or 'unknown'} "
                    f"on runtime {result.get('runtime') or 'unknown'}."
                ),
                status=(
                    "passed"
                    if str(result.get("status") or "").casefold()
                    in {"completed", "success", "ok"}
                    else "ready"
                ),
                trace_status=(
                    "completed"
                    if str(result.get("status") or "").casefold()
                    in {"completed", "success", "ok"}
                    else str(result.get("status") or "completed").casefold()
                ),
                metadata={
                    "runtime": result.get("runtime"),
                    "provider": result.get("provider"),
                    "model": result.get("model"),
                    "lane": result.get("lane"),
                    "fallback_used": bool(result.get("fallback_used")),
                    "attempt_count": len(result.get("attempts") or []),
                },
            )
        await cognitive_observability.record_execute(
            prepared_payload,
            result,
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
        )
        return result
    except CognitiveRouterError as exc:
        await _record_pstack_hirda_event(
            pstack,
            phase="hirda_execution_failed",
            title="HIRDA execution failed",
            detail=str(exc),
            status="failed",
            trace_status="failed",
        )
        await cognitive_observability.record_error(
            "execute",
            prepared_payload,
            exc,
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        await _record_pstack_hirda_event(
            pstack,
            phase="hirda_execution_failed",
            title="HIRDA execution failed",
            detail=f"{type(exc).__name__}: {exc}",
            status="failed",
            trace_status="failed",
        )
        await cognitive_observability.record_error(
            "execute",
            prepared_payload,
            exc,
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
        )
        raise


@app.post("/api/cognition/execute")
async def cognitive_router_execute(payload: dict[str, Any]):
    if not settings.studio.cognitive_router_enabled:
        raise HTTPException(status_code=503, detail="HIRDA cognitive router is disabled")
    if not settings.studio.cognitive_router_execute_enabled:
        raise HTTPException(status_code=503, detail="HIRDA cognitive execution is disabled")

    request_id = str(payload.get("request_id") or "").strip()
    if not request_id:
        return await _execute_cognitive_request_once(payload)

    fingerprint = _cognitive_execute_fingerprint(payload)
    task: asyncio.Task[dict[str, Any]]

    async with _cognitive_execute_idempotency_lock:
        now = time.monotonic()
        _prune_cognitive_execute_cache(now)

        cached = _cognitive_execute_completed.get(request_id)
        if cached is not None:
            _stored_at, cached_fingerprint, cached_result = cached
            if cached_fingerprint != fingerprint:
                raise HTTPException(
                    status_code=409,
                    detail="cognitive_request_id_conflict",
                )
            return dict(cached_result)

        inflight = _cognitive_execute_inflight.get(request_id)
        if inflight is not None:
            inflight_fingerprint, task = inflight
            if inflight_fingerprint != fingerprint:
                raise HTTPException(
                    status_code=409,
                    detail="cognitive_request_id_conflict",
                )
        else:
            task = asyncio.create_task(
                _execute_cognitive_request_once(dict(payload))
            )
            _cognitive_execute_inflight[request_id] = (fingerprint, task)

    try:
        result = await asyncio.shield(task)
    except Exception:
        if task.done():
            async with _cognitive_execute_idempotency_lock:
                current = _cognitive_execute_inflight.get(request_id)
                if current is not None and current[1] is task:
                    _cognitive_execute_inflight.pop(request_id, None)
        raise

    async with _cognitive_execute_idempotency_lock:
        current = _cognitive_execute_inflight.get(request_id)
        if current is not None and current[1] is task:
            _cognitive_execute_inflight.pop(request_id, None)
        _cognitive_execute_completed[request_id] = (
            time.monotonic(),
            fingerprint,
            dict(result),
        )
        _prune_cognitive_execute_cache(time.monotonic())

    return dict(result)


@app.get("/api/cognition/observability")
async def cognitive_observability_report(window_minutes: int | None = None):
    return await cognitive_observability.report(
        window_minutes=window_minutes,
    )


def _loopback_request(request: Request) -> bool:
    host = request.client.host if request.client is not None else ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@app.post("/api/internal/video-forge/owner-session")
async def video_forge_owner_session_api(request: Request, payload: dict[str, Any]):
    if not _loopback_request(request):
        raise HTTPException(status_code=404, detail="not_found")
    owner_token = payload.get("owner_token")
    if not isinstance(owner_token, str) or not owner_token:
        raise HTTPException(status_code=403, detail="Owner approval is required")
    approved = oauth.verify_owner_token(owner_token)
    if not approved:
        approved = await oauth.consume_owner_approval_token(
            owner_token,
            purpose="owner_approval",
        )
    if not approved:
        raise HTTPException(status_code=403, detail="Owner approval is required")
    session = oauth.issue_owner_session()
    await db.add_audit(
        "video_forge.owner_session.issued",
        actor="video-forge/http",
        outcome="success",
        data={"raw_owner_token_persisted": False},
    )
    return JSONResponse(
        {
            "owner_session": session,
            "expires_in": int(settings.studio.oauth_owner_session_ttl_seconds),
        },
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


@app.post("/api/internal/video-forge/profile-approval")
async def video_forge_profile_approval_api(
    request: Request,
    payload: dict[str, Any],
):
    if not _loopback_request(request):
        raise HTTPException(status_code=404, detail="not_found")
    owner_session = str(
        request.headers.get("X-HIRDA-Owner-Session") or ""
    ).strip()
    if not oauth.verify_owner_session(owner_session):
        raise HTTPException(status_code=403, detail="owner_session_required")

    profile = str(payload.get("profile") or "").strip()
    allowed = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
    if (
        not profile
        or len(profile) > 64
        or any(char not in allowed for char in profile)
    ):
        raise HTTPException(status_code=400, detail="invalid_profile")

    pool_id = str(payload.get("pool_id") or "video-motion").strip()
    if not pool_id or len(pool_id) > 96:
        raise HTTPException(status_code=400, detail="invalid_pool_id")

    raw_models = payload.get("allowed_models")
    if not isinstance(raw_models, list):
        raise HTTPException(status_code=400, detail="allowed_models_required")
    models = list(
        dict.fromkeys(
            str(value).strip()
            for value in raw_models
            if str(value).strip()
        )
    )
    if not models or len(models) > 32:
        raise HTTPException(status_code=400, detail="invalid_allowed_models")

    approval = await db.approve_video_profile(
        profile,
        pool_id=pool_id,
        allowed_models=models,
        approved_by="owner/session",
    )
    await db.add_audit(
        "video_forge.profile.approved",
        actor="owner/session",
        target_type="video_profile",
        target_id=profile,
        outcome="success",
        data={
            "pool_id": pool_id,
            "allowed_models": models,
            "authority": "HIRDA",
        },
    )
    return JSONResponse(
        {
            "approved": True,
            "authority": "HIRDA",
            "profile": profile,
            "pool_id": pool_id,
            "allowed_models": models,
            "approved_at": approval.get("approved_at"),
        },
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


@app.post("/api/world-authoring/preview")
async def world_authoring_preview_api(request: Request, payload: dict[str, Any]):
    if not _loopback_request(request):
        raise HTTPException(status_code=403, detail="World authoring API is loopback-only")
    proposal = payload.get("proposal")
    providers = payload.get("providers") or []
    if not isinstance(proposal, dict):
        raise HTTPException(status_code=400, detail="proposal must be an object")
    if not isinstance(providers, list) or any(not isinstance(item, dict) for item in providers):
        raise HTTPException(status_code=400, detail="providers must be an array of objects")
    try:
        return await gateway_sessions.world_authoring_preview(
            workspace=str(payload.get("workspace") or ""),
            proposal=proposal,
            providers=[dict(item) for item in providers],
            actor="hirda/http",
        )
    except WorldAuthoringError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/world-authoring/promote")
async def world_authoring_promote_api(request: Request, payload: dict[str, Any]):
    if not _loopback_request(request):
        raise HTTPException(status_code=403, detail="World authoring API is loopback-only")
    proposal = payload.get("proposal")
    commands = payload.get("commands")
    if not isinstance(proposal, dict):
        raise HTTPException(status_code=400, detail="proposal must be an object")
    if not isinstance(commands, list) or any(not isinstance(item, dict) for item in commands):
        raise HTTPException(status_code=400, detail="commands must be an array of objects")
    try:
        return await gateway_sessions.world_authoring_promote(
            workspace=str(payload.get("workspace") or ""),
            proposal=proposal,
            commands=[dict(item) for item in commands],
            validation_id=str(payload.get("validationId") or ""),
            rationale=str(payload.get("rationale") or ""),
            actor="hirda/http",
        )
    except WorldAuthoringError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/visual-assets/generate")
async def visual_asset_generate_api(request: Request, payload: dict[str, Any]):
    if not _loopback_request(request):
        raise HTTPException(status_code=403, detail="Visual asset API is loopback-only")
    proposal = payload.get("proposal")
    commands = payload.get("commands")
    if not isinstance(proposal, dict):
        raise HTTPException(status_code=400, detail="proposal must be an object")
    if not isinstance(commands, list) or any(not isinstance(item, dict) for item in commands):
        raise HTTPException(status_code=400, detail="commands must be an array of objects")
    try:
        result = await visual_assets.generate_and_promote(
            proposal=proposal,
            commands=[dict(item) for item in commands],
            validation_id=str(payload.get("validationId") or ""),
            rationale=str(payload.get("rationale") or ""),
        )
    except PixelArtStudioError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    candidate = result.get("candidate", {})
    promotion = result.get("earthPromotion", {})
    await db.add_audit(
        "visual_asset.promote",
        actor="hirda/http",
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


@app.post("/api/health/poll")
async def force_poll():
    await health.poll_once()
    return {"ok": True, "servers": [x.model_dump(mode="json") for x in health.snapshots.values()]}


@app.get("/api/events")
async def events(limit: int = 100):
    return {"events": await db.recent_events(limit)}

@app.get("/api/capsules")
async def capsule_list(limit: int = 100):
    return await capsules.overview(limit)


@app.get("/api/capsules/{capsule_id}")
async def capsule_get(capsule_id: str):
    try:
        return await capsules.get(capsule_id)
    except CapsuleNotFound:
        raise HTTPException(status_code=404, detail="capsule not found")


@app.get("/api/capsules/{capsule_id}/context")
async def capsule_context_projection(capsule_id: str):
    try:
        return await capsules.context_projection(capsule_id)
    except CapsuleNotFound:
        raise HTTPException(status_code=404, detail="capsule not found")


@app.post("/api/capsules/{capsule_id}/context/compact")
async def capsule_context_compact(capsule_id: str):
    try:
        return await capsules.compact_context(capsule_id)
    except CapsuleNotFound:
        raise HTTPException(status_code=404, detail="capsule not found")


@app.post("/api/capsules")
async def capsule_create(body: CapsuleCreate):
    try:
        return await capsules.create(
            title=body.title,
            workspace=body.workspace,
            source_pane=body.source_pane,
            agent=body.agent,
            metadata=body.metadata,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/capsules/{capsule_id}/contract")
async def capsule_contract_update(capsule_id: str, body: CapsuleContractUpdate):
    try:
        return await capsules.update_contract(capsule_id, body.contract)
    except CapsuleNotFound:
        raise HTTPException(status_code=404, detail="capsule not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/capsules/{capsule_id}/stage")
async def capsule_stage(capsule_id: str, body: CapsuleStageUpdate):
    try:
        return await capsules.set_stage(
            capsule_id,
            stage=body.stage,
            agent=body.agent,
            metadata=body.metadata,
        )
    except CapsuleNotFound:
        raise HTTPException(status_code=404, detail="capsule not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/capsules/{capsule_id}/handoff")
async def capsule_handoff(capsule_id: str, body: CapsuleHandoff):
    try:
        return await capsules.handoff(
            capsule_id,
            from_agent=body.from_agent,
            to_agent=body.to_agent,
            reason=body.reason,
            from_stage=body.from_stage,
            to_stage=body.to_stage,
            metadata=body.metadata,
        )
    except CapsuleNotFound:
        raise HTTPException(status_code=404, detail="capsule not found")
    except CapsuleDeliveryError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/capsules/{capsule_id}/handoff/{handoff_id}/ack")
async def capsule_handoff_ack(capsule_id: str, handoff_id: str, body: CapsuleHandoffAck):
    try:
        return await capsules.acknowledge_handoff(
            capsule_id,
            handoff_id,
            agent=body.agent,
            delivery_token=body.delivery_token,
            receipt=body.receipt,
        )
    except CapsuleNotFound:
        raise HTTPException(status_code=404, detail="capsule not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/capsules/{capsule_id}/handoff/{handoff_id}/complete")
async def capsule_handoff_complete(capsule_id: str, handoff_id: str, body: CapsuleHandoffCompletion):
    try:
        return await capsules.complete_handoff(
            capsule_id,
            handoff_id,
            agent=body.agent,
            delivery_token=body.delivery_token,
            sentinel=body.sentinel,
            receipt=body.receipt,
        )
    except CapsuleNotFound:
        raise HTTPException(status_code=404, detail="capsule not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/capsules/{capsule_id}/handoff/{handoff_id}/validate")
async def capsule_handoff_validate(capsule_id: str, handoff_id: str, body: CapsuleHandoffValidation):
    try:
        return await capsules.validate_handoff(
            capsule_id,
            handoff_id,
            agent=body.agent,
            delivery_token=body.delivery_token,
            passed=body.passed,
            checks=body.checks,
            plan=body.plan,
            evidence=body.evidence,
        )
    except CapsuleNotFound:
        raise HTTPException(status_code=404, detail="capsule not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/capsules/{capsule_id}/handoff/{handoff_id}/approve")
async def capsule_handoff_approve(capsule_id: str, handoff_id: str, body: CapsuleHandoffApproval):
    try:
        return await capsules.approve_handoff(
            capsule_id,
            handoff_id,
            approved_by=body.approved_by,
        )
    except CapsuleNotFound:
        raise HTTPException(status_code=404, detail="capsule not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/capsules/{capsule_id}/complete")
async def capsule_complete(capsule_id: str, body: CapsuleComplete):
    try:
        return await capsules.complete(
            capsule_id,
            agent=body.agent,
            metadata=body.metadata,
        )
    except CapsuleNotFound:
        raise HTTPException(status_code=404, detail="capsule not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/agents/runtimes")
async def agent_runtime_list(refresh: bool = False):
    if refresh:
        await herdr.refresh()
    return agent_runtimes.snapshot(force=refresh)


@app.get("/api/lanes")
async def lane_state_list():
    return {"lanes": await capsules.lane_states()}


@app.post("/api/lanes/{agent}/state")
async def lane_state_update(agent: str, body: LaneStateUpdate):
    try:
        return await capsules.set_lane_state(
            agent,
            body.state,
            reason=body.reason,
            actor=body.actor,
            auto_handoff=body.auto_handoff,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))



@app.get("/api/sessions")
async def sessions(limit: int = 100):
    return {"sessions": await db.list_sessions(limit)}


@app.get("/api/managed/status")
async def managed_status():
    return {**(await managed_sessions.status()), "cutover": await db.managed_cutover_summary()}


@app.get("/api/managed/workspaces")
async def managed_workspace_list():
    return {"workspaces": await managed_sessions.list_workspaces()}


@app.post("/api/managed/workspaces")
async def managed_workspace_register(payload: ManagedWorkspaceRegister):
    try:
        item = await managed_sessions.register_workspace(
            key=payload.key, project_path=payload.project_path, name=payload.name, actor="ui/api"
        )
        return {"workspace": item}
    except WorkspaceNotAllowed as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except ManagedSessionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.get("/api/managed/workspaces/{workspace_key}/graft")
async def managed_workspace_graft_status(workspace_key: str):
    try:
        return await graft.status(workspace_key)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown managed workspace")


@app.patch("/api/managed/workspaces/{workspace_key}/graft")
async def managed_workspace_graft_configure(
    workspace_key: str, payload: GraftConfigureRequest
):
    try:
        result = await graft.configure(
            workspace_key,
            enabled=payload.enabled,
            rollout_percent=payload.rollout_percent,
            actor="ui/api",
        )
        # Graft is a sidecar context plane. Serena keeps its existing
        # managed-session write/lease policy and remains the only editor.
        return result
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown managed workspace")
    except GraftError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/managed/workspaces/{workspace_key}/graft/query")
async def managed_workspace_graft_query(
    workspace_key: str, payload: GraftQueryRequest
):
    try:
        return await graft.query(
            workspace_key,
            question=payload.question,
            tool=payload.tool,
            arguments=payload.arguments,
            request_id=payload.request_id,
            actor="ui/api",
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown managed workspace")
    except GraftError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/managed/workspaces/{workspace_key}/graft/rollback")
async def managed_workspace_graft_rollback(
    workspace_key: str, payload: GraftRollbackRequest
):
    try:
        return await graft.rollback(
            workspace_key, reason=payload.reason, actor="ui/api"
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown managed workspace")


@app.post("/api/managed/workspaces/{workspace_key}/graft/rearm")
async def managed_workspace_graft_rearm(workspace_key: str):
    try:
        return await graft.rearm(workspace_key, actor="ui/api")
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown managed workspace")


@app.get("/api/managed/sessions")
async def managed_session_list():
    return {"sessions": await managed_sessions.list_sessions(), "status": await managed_sessions.status()}


@app.post("/api/managed/sessions")
async def managed_session_create(payload: ManagedSessionCreate):
    try:
        item = await managed_sessions.create_session(
            name=payload.name, workspace_key=payload.workspace_key, actor="ui/api"
        )
        return {"session": item}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown workspace: {exc.args[0]}")
    except WorkspaceNotAllowed as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except ManagedSessionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ManagedSessionError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@app.post("/api/managed/sessions/{session_id}/rename")
async def managed_session_rename(session_id: str, payload: ManagedSessionRename):
    try:
        return {"session": await managed_sessions.rename_session(session_id, name=payload.name, actor="ui/api")}
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown managed session")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/managed/sessions/{session_id}/resume")
async def managed_session_resume(session_id: str):
    try:
        return {"session": await managed_sessions.resume_session(session_id, actor="ui/api")}
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown managed session")
    except ManagedSessionError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


def _require_device_owner(request: Request) -> None:
    owner_token = request.headers.get("x-hirda-owner-token", "")
    owner_session = request.cookies.get("mcpstudio_owner_session", "")
    if oauth.verify_owner_token(owner_token) or oauth.verify_owner_session(owner_session):
        return
    raise HTTPException(status_code=403, detail="Owner approval is required")


def _device_pairing_error(exc: DevicePairingError) -> HTTPException:
    message = str(exc)
    if message == "device_pairing_disabled":
        return HTTPException(status_code=503, detail=message)
    if message == "device_already_trusted":
        return HTTPException(status_code=409, detail=message)
    if message == "pairing_verification_failed":
        return HTTPException(status_code=401, detail=message)
    if message.startswith("pairing_session_not_"):
        return HTTPException(status_code=409, detail=message)
    return HTTPException(status_code=400, detail=message)



@app.get("/pair/{session_id}")
async def device_pairing_page(
    request: Request,
    session_id: str,
    nonce: str = "",
):
    try:
        session = device_pairing.status(session_id)
    except KeyError:
        return HTMLResponse("<h1>Pairing session not found</h1>", status_code=404)
    except DevicePairingError as exc:
        raise _device_pairing_error(exc)
    if session.get("state") == "pairing" and not nonce:
        return HTMLResponse(
            render_pairing_page(
                session,
                error="Open the QR code again. Its one-time pairing nonce is missing.",
            ),
            status_code=400,
            headers={"Cache-Control": "no-store"},
        )
    return HTMLResponse(
        render_pairing_page(
            session,
            nonce=nonce,
            owner_session_valid=oauth.verify_owner_session(
                request.cookies.get("mcpstudio_owner_session", "")
            ),
        ),
        headers={"Cache-Control": "no-store"},
    )


@app.post("/pair/{session_id}/verify")
async def device_pairing_page_verify(request: Request, session_id: str):
    body = await request.body()
    if len(body) > 4096:
        raise HTTPException(status_code=413, detail="Pairing form too large")
    parsed = parse_qs(body.decode("utf-8"), keep_blank_values=True)
    code = (parsed.get("code") or [""])[-1].replace(" ", "")
    nonce = (parsed.get("nonce") or [""])[-1]
    try:
        device_pairing.verify(session_id, code=code, nonce=nonce)
    except KeyError:
        return HTMLResponse("<h1>Pairing session not found</h1>", status_code=404)
    except DevicePairingError:
        try:
            session = device_pairing.status(session_id)
        except Exception:
            return HTMLResponse("<h1>Pairing failed</h1>", status_code=401)
        return HTMLResponse(
            render_pairing_page(
                session,
                nonce=nonce,
                error="The code or QR session did not match.",
            ),
            status_code=401,
            headers={"Cache-Control": "no-store"},
        )
    return RedirectResponse(f"/pair/{session_id}", status_code=303)


@app.post("/pair/{session_id}/approve")
async def device_pairing_page_approve(request: Request, session_id: str):
    body = await request.body()
    if len(body) > 4096:
        raise HTTPException(status_code=413, detail="Pairing form too large")
    parsed = parse_qs(body.decode("utf-8"), keep_blank_values=True)
    owner_token = (parsed.get("owner_token") or [""])[-1]
    owner_session = request.cookies.get("mcpstudio_owner_session", "")
    approved_by_token = oauth.verify_owner_token(owner_token)
    if not (approved_by_token or oauth.verify_owner_session(owner_session)):
        try:
            session = device_pairing.status(session_id)
        except KeyError:
            return HTMLResponse("<h1>Pairing session not found</h1>", status_code=404)
        return HTMLResponse(
            render_pairing_page(
                session,
                error="Owner approval is required.",
            ),
            status_code=403,
            headers={"Cache-Control": "no-store"},
        )
    policy = pairing_policy_from_form(parsed)
    try:
        device_pairing.approve(session_id, actor="owner/web", policy=policy)
        machine_registry.sync_trusted_devices()
    except KeyError:
        return HTMLResponse("<h1>Pairing session not found</h1>", status_code=404)
    except DevicePairingError as exc:
        raise _device_pairing_error(exc)
    response = RedirectResponse(f"/pair/{session_id}", status_code=303)
    response.headers["Cache-Control"] = "no-store"
    if approved_by_token:
        response.set_cookie(
            "mcpstudio_owner_session",
            oauth.issue_owner_session(),
            max_age=settings.studio.oauth_owner_session_ttl_seconds,
            path="/",
            secure=True,
            httponly=True,
            samesite="lax",
        )
    return response


def _device_broker_http_error(exc: DeviceBrokerError) -> HTTPException:
    message = str(exc)
    if message == "device_broker_disabled":
        return HTTPException(status_code=503, detail=message)
    if message in {"unknown_device", "device_not_trusted"}:
        return HTTPException(status_code=403, detail="device_authentication_failed")
    return HTTPException(status_code=400, detail=message)


@app.get("/api/device-broker")
async def device_broker_status(request: Request):
    _require_device_owner(request)
    return await device_broker.snapshot()


@app.websocket("/api/device-broker/ws")
async def device_broker_ws(websocket: WebSocket):
    await websocket.accept()
    device_id = str(websocket.query_params.get("device_id") or "").strip()
    connection_id: str | None = None
    try:
        if not device_id:
            await websocket.close(code=4400, reason="device_id required")
            return
        try:
            challenge = device_broker.issue_challenge(device_id)
        except DeviceBrokerError:
            await websocket.close(code=4403, reason="Device authentication failed")
            return

        await websocket.send_json(challenge)
        try:
            raw = await asyncio.wait_for(
                websocket.receive_text(),
                timeout=device_broker.challenge_ttl_seconds,
            )
        except TimeoutError:
            await websocket.close(code=4408, reason="Device challenge timeout")
            return
        if len(raw.encode("utf-8")) > device_broker.max_message_bytes:
            await websocket.close(code=1009, reason="Device message too large")
            return
        try:
            auth = json.loads(raw)
        except json.JSONDecodeError:
            await websocket.close(code=4400, reason="Invalid authentication payload")
            return
        if not isinstance(auth, dict) or auth.get("type") != "authenticate":
            await websocket.close(code=4400, reason="Authentication message required")
            return
        try:
            device_broker.verify_challenge(
                str(auth.get("challenge_id") or ""),
                str(auth.get("signature") or ""),
            )
        except DeviceBrokerError:
            await websocket.close(code=4401, reason="Device authentication failed")
            return

        remote = websocket.client.host if websocket.client else None
        connection = await device_broker.register_connection(
            device_id,
            websocket,
            remote=remote,
        )
        connection_id = str(connection["connection_id"])
        await websocket.send_json(
            {
                "schema": device_broker.SCHEMA,
                "type": "authenticated",
                **connection,
            }
        )

        while True:
            remaining = await device_broker.remaining_seconds(device_id, connection_id)
            if remaining <= 0:
                await websocket.close(code=4408, reason="Device session expired")
                return
            timeout = min(float(device_broker.heartbeat_timeout_seconds), remaining)
            try:
                raw = await asyncio.wait_for(websocket.receive_text(), timeout=timeout)
            except TimeoutError:
                await websocket.close(code=4408, reason="Device heartbeat timeout")
                return
            if len(raw.encode("utf-8")) > device_broker.max_message_bytes:
                await websocket.close(code=1009, reason="Device message too large")
                return
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json(
                    {"schema": device_broker.SCHEMA, "type": "error", "error": "invalid_json"}
                )
                continue
            if not isinstance(message, dict):
                await websocket.send_json(
                    {"schema": device_broker.SCHEMA, "type": "error", "error": "message_must_be_object"}
                )
                continue
            kind = str(message.get("type") or "")
            if kind == "node_observations":
                try:
                    await device_broker.touch_connection(device_id, connection_id)
                except DeviceBrokerError:
                    await websocket.close(code=4403, reason="Device trust revoked or session expired")
                    return
                machine_registry.sync_trusted_devices()
                if not machine_registry.contains(device_id):
                    await device_broker.send_connection_message(
                        device_id,
                        connection_id,
                        {
                            "schema": device_broker.SCHEMA,
                            "type": "error",
                            "error": "device_node_not_registered",
                        },
                    )
                    continue
                accepted = ingest_node_observations(
                    machine_registry,
                    device_id,
                    message.get("observations"),
                    source="device_runtime",
                )
                await device_broker.send_connection_message(
                    device_id,
                    connection_id,
                    {
                        "schema": device_broker.SCHEMA,
                        "type": "node_observations_ack",
                        "facets": sorted(accepted),
                    },
                )
                continue
            if kind == "rpc_response":
                try:
                    await device_broker.touch_connection(device_id, connection_id)
                except DeviceBrokerError:
                    await websocket.close(code=4403, reason="Device trust revoked or session expired")
                    return
                accepted = await device_broker.resolve_rpc_response(
                    device_id,
                    connection_id,
                    message,
                )
                if not accepted:
                    await device_broker.send_connection_message(
                        device_id,
                        connection_id,
                        {
                            "schema": device_broker.SCHEMA,
                            "type": "error",
                            "error": "unknown_rpc_request",
                        },
                    )
                continue
            if kind not in {"heartbeat", "ping"}:
                await device_broker.send_connection_message(
                    device_id,
                    connection_id,
                    {
                        "schema": device_broker.SCHEMA,
                        "type": "error",
                        "error": "unsupported_message",
                    },
                )
                continue
            try:
                current = await device_broker.touch_connection(device_id, connection_id)
            except DeviceBrokerError:
                await websocket.close(code=4403, reason="Device trust revoked or session expired")
                return
            await device_broker.send_connection_message(
                device_id,
                connection_id,
                {
                    "schema": device_broker.SCHEMA,
                    "type": "heartbeat_ack" if kind == "heartbeat" else "pong",
                    "connection_id": connection_id,
                    "last_seen_at": current["last_seen_at"],
                    "expires_at": current["expires_at"],
                },
            )
    except WebSocketDisconnect:
        pass
    finally:
        if connection_id:
            await device_broker.unregister_connection(device_id, connection_id)
            status = await device_broker.connection_status(device_id)
            if not status.get("connected") and machine_registry.contains(device_id):
                mark_node_unavailable(
                    machine_registry,
                    device_id,
                    source="device_broker",
                    state="offline",
                )


@app.post("/api/device-pairing/sessions")
async def device_pairing_start(payload: DevicePairingStart):
    try:
        result = device_pairing.start(
            name=payload.name,
            platform=payload.platform,
            architecture=payload.architecture,
            public_key=payload.public_key,
            capabilities=payload.capabilities,
            runtime_version=payload.runtime_version,
        )
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
    except DevicePairingError as exc:
        raise _device_pairing_error(exc)


@app.get("/api/device-pairing/sessions/{session_id}")
async def device_pairing_status(session_id: str):
    try:
        return JSONResponse(
            device_pairing.status(session_id),
            headers={"Cache-Control": "no-store"},
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown pairing session")
    except DevicePairingError as exc:
        raise _device_pairing_error(exc)


@app.post("/api/device-pairing/sessions/{session_id}/verify")
async def device_pairing_verify(session_id: str, payload: DevicePairingVerify):
    try:
        result = device_pairing.verify(
            session_id,
            code=payload.code,
            nonce=payload.nonce,
        )
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown pairing session")
    except DevicePairingError as exc:
        raise _device_pairing_error(exc)


@app.post("/api/device-pairing/sessions/{session_id}/approve")
async def device_pairing_approve(
    session_id: str,
    payload: DevicePairingApprove,
    request: Request,
):
    _require_device_owner(request)
    try:
        result = device_pairing.approve(
            session_id,
            actor="owner/api",
            policy=payload.policy,
        )
        machine_registry.sync_trusted_devices()
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown pairing session")
    except DevicePairingError as exc:
        raise _device_pairing_error(exc)


@app.post("/api/device-pairing/sessions/{session_id}/reject")
async def device_pairing_reject(
    session_id: str,
    payload: DevicePairingReject,
    request: Request,
):
    _require_device_owner(request)
    try:
        result = device_pairing.reject(
            session_id,
            actor="owner/api",
            reason=payload.reason,
        )
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown pairing session")
    except DevicePairingError as exc:
        raise _device_pairing_error(exc)


@app.get("/api/device-pairing/devices")
async def device_pairing_devices(request: Request):
    _require_device_owner(request)
    try:
        return JSONResponse(
            device_pairing.list_devices(),
            headers={"Cache-Control": "no-store"},
        )
    except DevicePairingError as exc:
        raise _device_pairing_error(exc)


@app.post("/api/device-pairing/devices/{device_id}/revoke")
async def device_pairing_revoke(
    device_id: str,
    payload: DeviceRevoke,
    request: Request,
):
    _require_device_owner(request)
    try:
        result = device_pairing.revoke(
            device_id,
            actor="owner/api",
            reason=payload.reason,
        )
        machine_registry.sync_trusted_devices()
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown trusted device")
    except DevicePairingError as exc:
        raise _device_pairing_error(exc)


@app.get("/api/machines")
async def machine_registry_status():
    return await machine_router.machine_snapshot()


@app.post("/api/machines/routes/resolve")
async def machine_capability_route_resolve(payload: MachineCapabilityRouteRequest):
    try:
        return machine_router.resolve_capability_route(
            payload.capability,
            permission=payload.permission,
            machine_id=payload.machine_id,
            backend_id=payload.backend_id,
            model_id=payload.model_id,
        )
    except MachineRegistryError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.get("/api/machines/enrollments")
async def machine_enrollment_status():
    return machine_enrollment.snapshot()


@app.post("/api/machines/discover")
async def machine_enrollment_discover():
    try:
        return await machine_enrollment.discover()
    except MachineEnrollmentError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@app.post("/api/machines/enrollments/{machine_id}/approve")
async def machine_enrollment_approve(machine_id: str, payload: MachineEnrollmentApprove):
    try:
        return machine_enrollment.approve(
            machine_id,
            name=payload.name,
            workspace_map=payload.workspace_map,
            policy=payload.policy,
            actor="ui/api",
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown pending machine")
    except (MachineEnrollmentError, MachineRegistryError) as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.post("/api/machines/enrollments/{machine_id}/reject")
async def machine_enrollment_reject(machine_id: str, payload: MachineEnrollmentReject):
    try:
        return machine_enrollment.reject(machine_id, reason=payload.reason, actor="ui/api")
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown pending machine")


@app.get("/api/machines/{machine_id}/policy")
async def machine_policy(machine_id: str):
    try:
        return machine_router.machine_policy(machine_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown machine")
    except MachineRegistryError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.put("/api/managed/sessions/{session_id}/machine")
async def managed_session_machine_update(session_id: str, payload: ManagedSessionMachineUpdate):
    try:
        return await machine_router.bind_session(
            db, session_id, payload.machine_id, actor="ui/api"
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown managed session or machine")
    except MachineRegistryError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.get("/api/managed/sessions/{session_id}/permissions")
async def managed_session_permissions(session_id: str):
    try:
        return await managed_sessions.permissions(session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown managed session")


@app.patch("/api/managed/sessions/{session_id}/permissions")
async def managed_session_permissions_update(session_id: str, payload: ManagedSessionPermissionsUpdate):
    try:
        changes = payload.model_dump(exclude_none=True)
        return {"session": await managed_sessions.update_permissions(session_id, changes, actor="ui/api")}
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown managed session")


@app.get("/api/managed/sessions/{session_id}/history")
async def managed_session_history(session_id: str):
    try:
        return await managed_sessions.history(session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown managed session")


@app.post("/api/managed/sessions/{session_id}/restart")
async def managed_session_restart(session_id: str):
    try:
        return {"session": await managed_sessions.restart_session(session_id, actor="ui/api")}
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown managed session")
    except ManagedSessionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ManagedSessionError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@app.post("/api/managed/sessions/{session_id}/stop")
async def managed_session_stop(session_id: str):
    try:
        return {"session": await managed_sessions.stop_session(session_id, actor="ui/api")}
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown managed session")
    except ManagedSessionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.post("/api/gateway/sessions/{gateway_session_id}/context")
async def gateway_context_usage_report(gateway_session_id: str, payload: GatewayContextUsageReport):
    try:
        return await gateway_sessions.report_context_usage(
            gateway_session_id,
            context_usage_percent=payload.context_usage_percent,
            source=payload.source,
            actor="ui/api",
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown gateway session")
    except ManagedSessionError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/gateway/sessions/{gateway_session_id}/handoff")
async def gateway_session_handoff_prepare(gateway_session_id: str, payload: SessionHandoffPrepareRequest):
    try:
        return await gateway_sessions.prepare_session_handoff(
            gateway_session_id,
            summary=payload.summary,
            reason=payload.reason,
            ttl_seconds=payload.ttl_seconds,
            actor="ui/api",
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown gateway session")
    except ManagedSessionError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/gateway/sessions/{gateway_session_id}/managed/attach")
async def managed_gateway_attach(gateway_session_id: str, payload: ManagedGatewayAttach):
    try:
        return await gateway_sessions.bind_managed_session(gateway_session_id, payload.managed_session_id, actor="ui/api")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown session: {exc.args[0]}")
    except ManagedSessionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.post("/api/gateway/sessions/{gateway_session_id}/managed/detach")
async def managed_gateway_detach(gateway_session_id: str):
    try:
        return await gateway_sessions.detach_managed_session(gateway_session_id, actor="ui/api")
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown gateway session")
    except ManagedSessionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.get("/api/gateway/sessions")
async def gateway_session_list(limit: int = 200, tunnel_id: str | None = None):
    return {
        "sessions": await db.list_gateway_sessions(limit, tunnel_id=tunnel_id),
        "summary": await db.gateway_session_summary(),
        "tunnel_summary": await db.tunnel_session_summary(),
        "filter": {"tunnel_id": tunnel_id},
    }


@app.get("/api/connectivity/tunnels/{tunnel_id}/sessions")
async def tunnel_sessions(tunnel_id: str, limit: int = 200):
    try:
        tunnel = await db.get_tunnel(tunnel_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown tunnel")
    sessions = await db.list_gateway_sessions(limit, tunnel_id=tunnel_id)
    all_summary = await db.tunnel_session_summary()
    return {
        "tunnel": tunnel,
        "sessions": sessions,
        "summary": all_summary.get("by_tunnel", {}).get(tunnel_id, {"total": 0, "counts": {}, "reconnects": 0}),
    }


@app.get("/api/openai/compatibility")
async def openai_compatibility():
    sessions = await db.list_gateway_sessions(500)
    observations = []
    scope_counts: dict[str, int] = {}
    openai_like = 0
    for item in sessions:
        meta = item.get("metadata") or {}
        obs = meta.get("client_observation") if isinstance(meta.get("client_observation"), dict) else {}
        scope = str(meta.get("identity_scope") or obs.get("identity_scope") or "unknown")
        scope_counts[scope] = scope_counts.get(scope, 0) + 1
        if obs.get("openai_like"):
            openai_like += 1
        if obs:
            observations.append({
                "gateway_session_id": item.get("id"),
                "studio_session_id": item.get("studio_session_id"),
                "status": item.get("status"),
                "created_at": item.get("created_at"),
                "last_seen_at": item.get("last_seen_at"),
                "generation": item.get("generation"),
                "reconnect_count": item.get("reconnect_count"),
                "reclaimed_studio_session": bool(meta.get("reclaimed_studio_session")),
                "identity_scope": scope,
                "identity_source": meta.get("identity_source") or obs.get("identity_source"),
                "openai_like": bool(obs.get("openai_like")),
                "client_info_name": obs.get("client_info_name"),
                "client_info_version": obs.get("client_info_version"),
                "user_agent": obs.get("user_agent"),
                "conversation_identity_available": bool(obs.get("conversation_identity_available")),
                "observed_header_names": obs.get("observed_header_names", []),
            })
    observations.sort(key=lambda x: x.get("last_seen_at") or "", reverse=True)
    return {
        "enabled": settings.studio.openai_compatibility_enabled,
        "connector_reclaim_enabled": settings.studio.openai_connector_reclaim_enabled,
        "identity_model": {
            "explicit": "caller-supplied stable client id; may represent a chat/session if the client supplies one",
            "connector": "bearer credential fingerprint; stable per configured connector credential, not per ChatGPT conversation",
            "anonymous": "user-agent fingerprint; compatibility-only and not reclaimed",
        },
        "summary": {
            "observed": len(observations),
            "openai_like": openai_like,
            "identity_scopes": scope_counts,
        },
        "observations": observations[:100],
    }


@app.get("/api/certification/m5.3.2")
async def m5_3_2_certification_status():
    """Read-only evidence surface for real ChatGPT reconnect certification.

    It intentionally exposes no credentials or token values. Public Cloudflare
    routing should continue to publish only the MCP/OAuth paths, not /api/*.
    """
    sessions = await db.list_gateway_sessions(500)
    chatgpt = []
    for item in sessions:
        meta = item.get("metadata") or {}
        obs = meta.get("client_observation") if isinstance(meta.get("client_observation"), dict) else {}
        if not obs.get("openai_like"):
            continue
        chatgpt.append({
            "gateway_session_id": item.get("id"),
            "studio_session_id": item.get("studio_session_id"),
            "client_id": item.get("client_id"),
            "status": item.get("status"),
            "created_at": item.get("created_at"),
            "last_seen_at": item.get("last_seen_at"),
            "generation": item.get("generation"),
            "reconnect_count": item.get("reconnect_count"),
            "reclaimed_studio_session": bool(meta.get("reclaimed_studio_session")),
            "identity_scope": meta.get("identity_scope") or obs.get("identity_scope"),
            "identity_source": meta.get("identity_source") or obs.get("identity_source"),
            "client_info_name": obs.get("client_info_name"),
            "client_info_version": obs.get("client_info_version"),
        })
    chatgpt.sort(key=lambda x: x.get("last_seen_at") or "", reverse=True)
    return {
        "milestone": "M5.3.2",
        "version": "0.9.10-m6.2.5",
        "gateway_session_test_mode": settings.studio.gateway_session_test_mode,
        "gateway_session_auto_reconnect": settings.studio.gateway_session_auto_reconnect,
        "gateway_session_replay_on_404": settings.studio.gateway_session_replay_on_404,
        "openai_connector_reclaim_enabled": settings.studio.openai_connector_reclaim_enabled,
        "oauth": await db.oauth_runtime_summary(),
        "chatgpt_sessions": chatgpt[:100],
    }


@app.post("/api/gateway/sessions/{gateway_session_id}/fault/drop-upstream")
async def gateway_session_drop_upstream_for_test(gateway_session_id: str):
    if settings.studio.production_mode:
        raise HTTPException(status_code=404, detail="Not found")
    try:
        return await gateway_sessions.drop_upstream_for_test(gateway_session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown gateway session")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.post("/api/sessions")
async def create_session(payload: SessionCreate):
    if not any(s.id == payload.server_id for s in settings.servers):
        raise HTTPException(status_code=400, detail="Unknown server_id")
    item = await db.create_session(payload.model_dump())
    await db.add_event(
        "session.connected",
        f"Client {payload.client_id} registered as {item['id']}",
        server_id=payload.server_id,
        data={"studio_session_id": item["id"], "client_type": payload.client_type},
    )
    return item


@app.post("/api/sessions/reclaim")
async def reclaim_session(payload: SessionReclaim):
    if not any(s.id == payload.server_id for s in settings.servers):
        raise HTTPException(status_code=400, detail="Unknown server_id")
    item, reclaimed = await db.reclaim_session(payload.model_dump())
    await db.add_event(
        "session.reclaimed" if reclaimed else "session.connected",
        f"Client {payload.client_id} {'reclaimed' if reclaimed else 'created'} {item['id']}",
        server_id=payload.server_id,
        data={"studio_session_id": item["id"], "client_type": payload.client_type, "reclaimed": reclaimed},
    )
    return {"session": item, "reclaimed": reclaimed}


@app.post("/api/sessions/{session_id}/heartbeat")
async def session_heartbeat(session_id: str, payload: SessionHeartbeat):
    try:
        return await db.heartbeat_session(session_id, payload.model_dump())
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown session")


@app.delete("/api/sessions/{session_id}")
async def disconnect_session(session_id: str):
    try:
        item = await db.disconnect_session(session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown session")
    await db.add_event(
        "session.disconnected",
        f"Session {session_id} disconnected",
        server_id=item["server_id"],
        data={"studio_session_id": session_id},
    )
    return item


@app.get("/api/connectivity")
async def connectivity_status():
    return {
        "supervisor": connectivity.snapshot,
        "tunnels": await db.list_tunnels(),
        "sessions": await db.session_summary(),
        "gateway_sessions": await db.gateway_session_summary(),
    }


@app.post("/api/connectivity/run")
async def connectivity_run_once():
    return await connectivity.poll_once(allow_reconnect=True)


@app.post("/api/connectivity/tunnels")
async def register_tunnel(payload: TunnelRegister):
    if payload.provider != "cloudflare" and payload.managed:
        raise HTTPException(status_code=400, detail="managed process control is only supported for cloudflare")
    if payload.provider == "cloudflare" and payload.managed and not payload.tunnel_name:
        raise HTTPException(status_code=400, detail="managed cloudflare tunnel needs a pre-provisioned named tunnel")
    item = await db.upsert_tunnel(payload.model_dump())
    await db.add_event(
        "connectivity.tunnel.registered",
        f"Tunnel {item['id']} registered",
        data={"tunnel_id": item["id"], "provider": item["provider"]},
    )
    connectivity.kick()
    return item


@app.delete("/api/connectivity/tunnels/{tunnel_id}")
async def delete_tunnel(tunnel_id: str):
    try:
        item = await db.get_tunnel(tunnel_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown tunnel")
    if item.get("managed") and item.get("pid"):
        raise HTTPException(status_code=409, detail="Stop managed tunnel before deleting it")
    await db.delete_tunnel(tunnel_id)
    await db.add_event("connectivity.tunnel.deleted", f"Tunnel {tunnel_id} deleted", data={"tunnel_id": tunnel_id})
    connectivity.kick()
    return {"ok": True, "id": tunnel_id}


@app.get("/api/connectivity/tunnels/{tunnel_id}/preflight")
async def tunnel_preflight(tunnel_id: str):
    try:
        return await connectivity.cloudflare_preflight(tunnel_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown tunnel")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.post("/api/connectivity/tunnels/{tunnel_id}/fault/terminate")
async def terminate_tunnel_for_test(tunnel_id: str):
    if settings.studio.production_mode:
        raise HTTPException(status_code=404, detail="Not found")
    try:
        item = await connectivity.terminate_tunnel_for_test(tunnel_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown tunnel")
    except (ValueError, ProcessLookupError) as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    connectivity.kick()
    return item


@app.post("/api/connectivity/tunnels/{tunnel_id}/start")
async def start_tunnel(tunnel_id: str):
    try:
        item = await connectivity.start_tunnel(tunnel_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown tunnel")
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    connectivity.kick()
    return item


@app.post("/api/connectivity/tunnels/{tunnel_id}/stop")
async def stop_tunnel(tunnel_id: str):
    try:
        item = await connectivity.stop_tunnel(tunnel_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown tunnel")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    connectivity.kick()
    return item


@app.post("/api/connectivity/tunnels/{tunnel_id}/restart")
async def restart_tunnel(tunnel_id: str):
    try:
        item = await connectivity.restart_tunnel(tunnel_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown tunnel")
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    connectivity.kick()
    return item


@app.get("/api/workers")
async def list_workers():
    return {
        "workers": await db.list_workers(),
        "leases": await db.list_workspace_leases(),
        "summary": await workers.summary(),
    }


@app.post("/api/workers/{worker_id}/bind")
async def bind_worker(worker_id: str, payload: WorkerBind):
    if payload.session_id:
        try:
            await db.get_session(payload.session_id)
        except KeyError:
            raise HTTPException(status_code=400, detail="Unknown session_id")
    try:
        item = await db.bind_worker(worker_id, payload.model_dump())
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown worker")
    except LeaseConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={"error": "workspace_lease_conflict", "workspace": exc.workspace, "holder": exc.worker_id},
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    await db.add_event(
        "worker.bound",
        f"{worker_id} bound to {payload.workspace}",
        server_id=item["server_id"],
        data={
            "worker_id": worker_id,
            "workspace": payload.workspace,
            "pane": payload.pane,
            "agent": payload.agent,
            "lease_mode": payload.lease_mode,
        },
    )
    return item


@app.post("/api/workers/{worker_id}/heartbeat")
async def worker_heartbeat(worker_id: str, payload: WorkerHeartbeat):
    try:
        return await db.heartbeat_worker(worker_id, payload.model_dump())
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown worker")
    except LeaseConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={"error": "workspace_lease_conflict", "workspace": exc.workspace, "holder": exc.worker_id},
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.post("/api/workers/{worker_id}/state")
async def worker_state(worker_id: str, payload: WorkerStatePatch):
    try:
        item = await db.set_worker_state(worker_id, payload.state, payload.work_label)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown worker")
    except LeaseConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={"error": "workspace_lease_conflict", "workspace": exc.workspace, "holder": exc.worker_id},
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    await db.add_event(
        "worker.state",
        f"{worker_id} -> {payload.state}",
        severity="warning" if payload.state in {"degraded", "dead"} else "info",
        server_id=item["server_id"],
        data={"worker_id": worker_id, "state": payload.state, "work_label": payload.work_label},
    )
    return item


@app.post("/api/workers/{worker_id}/release")
async def release_worker(worker_id: str):
    try:
        before = await db.get_worker(worker_id)
        item = await db.release_worker(worker_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown worker")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    await db.add_event(
        "worker.released",
        f"{worker_id} released from {before.get('workspace') or 'unbound'}",
        server_id=item["server_id"],
        data={"worker_id": worker_id, "workspace": before.get("workspace")},
    )
    return item


@app.get("/api/herdr")
async def herdr_status():
    return herdr.snapshot


@app.post("/api/herdr/refresh")
async def refresh_herdr():
    return await herdr.refresh()


@app.get("/api/work")
async def list_work(state: str | None = None, limit: int = 200):
    valid = {None, "queued", "running", "completed", "failed", "cancelled", "detached"}
    if state not in valid:
        raise HTTPException(status_code=400, detail="Invalid work state")
    return {"work": await db.list_work(state=state, limit=limit), "summary": await db.work_summary()}


@app.get("/api/work/{work_id}")
async def get_work(work_id: str):
    try:
        return await db.get_work(work_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown work item")


@app.post("/api/work")
async def submit_work(payload: WorkSubmit):
    if payload.dispatch_mode == "herdr" and not (payload.instruction or "").strip():
        raise HTTPException(status_code=400, detail="Herdr dispatch requires instruction")
    if payload.session_id:
        try:
            await db.get_session(payload.session_id)
        except KeyError:
            raise HTTPException(status_code=400, detail="Unknown session_id")
    server_id = payload.server_id or settings.studio.worker_server_id
    if server_id != settings.studio.worker_server_id:
        raise HTTPException(
            status_code=400,
            detail="Scheduler currently supports only studio.worker_server_id",
        )
    summary = await db.work_summary()
    if summary["queue"] >= settings.studio.scheduler_max_queue:
        raise HTTPException(status_code=429, detail="Scheduler queue is full")
    item = await db.create_work(payload.model_dump(), settings.studio.worker_server_id or server_id)
    await db.add_event(
        "work.queued",
        f"{item['id']} queued: {item['label']}",
        server_id=item["server_id"],
        data={
            "work_id": item["id"],
            "workspace": item["workspace"],
            "priority": item["priority"],
            "session_id": item.get("session_id"),
        },
    )
    await db.add_audit(
        "work.submit", actor="api", target_type="work", target_id=item["id"],
        data={"workspace": item["workspace"], "dispatch_mode": item["dispatch_mode"], "priority": item["priority"]},
    )
    scheduler.kick()
    execution.kick()
    return item


@app.post("/api/work/{work_id}/complete")
async def complete_work(work_id: str, payload: WorkFinish):
    try:
        item = await db.finish_work(work_id, state="completed", result=payload.result)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown work item")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    await db.add_event(
        "work.completed",
        f"{work_id} completed",
        server_id=item["server_id"],
        data={"work_id": work_id, "workspace": item["workspace"]},
    )
    await db.add_audit("work.complete", actor="api", target_type="work", target_id=work_id, data={"workspace": item["workspace"]})
    scheduler.kick()
    return item


@app.post("/api/work/{work_id}/fail")
async def fail_work(work_id: str, payload: WorkFail):
    try:
        item = await db.finish_work(
            work_id, state="failed", result=payload.result, error=payload.error
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown work item")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    await db.add_event(
        "work.failed",
        f"{work_id} failed: {payload.error}",
        severity="warning",
        server_id=item["server_id"],
        data={"work_id": work_id, "workspace": item["workspace"]},
    )
    await db.add_audit("work.fail", actor="api", target_type="work", target_id=work_id, outcome="failure", data={"workspace": item["workspace"], "error": payload.error})
    scheduler.kick()
    return item


@app.post("/api/work/{work_id}/cancel")
async def cancel_work(work_id: str):
    try:
        item, disposition = await db.request_cancel_work(work_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown work item")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if disposition == "cancelled":
        kind, message = "work.cancelled", f"{work_id} cancelled before upstream dispatch"
    else:
        kind, message = "work.cancel_pending", f"{work_id} cancellation pending; upstream execution may continue"
    await db.add_event(
        kind, message, severity="warning", server_id=item["server_id"],
        data={"work_id": work_id, "workspace": item["workspace"], "disposition": disposition},
    )
    await db.add_audit(
        "work.cancel_request", actor="api", target_type="work", target_id=work_id,
        data={"workspace": item["workspace"], "disposition": disposition},
    )
    scheduler.kick()
    return {**item, "cancel_disposition": disposition}


@app.post("/api/work/{work_id}/detach")
async def detach_work(work_id: str, payload: WorkDetach):
    try:
        item = await db.detach_work(work_id, reason=payload.reason)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown work item")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    await db.add_event(
        "work.detached",
        f"{work_id} detached from Studio tracking; upstream execution may continue",
        severity="warning", server_id=item["server_id"],
        data={"work_id": work_id, "workspace": item["workspace"], "reason": payload.reason},
    )
    await db.add_audit(
        "work.detach", actor="api", target_type="work", target_id=work_id,
        data={"workspace": item["workspace"], "reason": payload.reason},
    )
    if settings.studio.operations_alerts_enabled:
        await db.open_alert(
            f"detached:{work_id}", "work.detached",
            f"Work {work_id} manually detached from Studio tracking",
            severity="warning", data={"work_id": work_id, "reason": payload.reason},
        )
    scheduler.kick()
    return item


@app.get("/api/operations")
async def operations_status():
    return {
        "supervisor": operations.snapshot,
        "summary": await db.operations_summary(),
        "schema": await db.schema_status(),
    }


@app.post("/api/operations/run")
async def run_operations_once():
    result = await operations.run_once(force_cleanup=False)
    return {"supervisor": result, "summary": await db.operations_summary()}


@app.get("/api/observability")
async def observability_status():
    if not settings.studio.observability_enabled:
        raise HTTPException(status_code=404, detail="Observability disabled")
    return await observability.report()


@app.post("/api/observability/run")
async def observability_run_once():
    if not settings.studio.observability_enabled:
        raise HTTPException(status_code=404, detail="Observability disabled")
    supervisor = await observability.run_once()
    report = await observability.report()
    report["manager"] = supervisor
    return report


@app.get("/api/audit")
async def audit_log(limit: int = 200, action: str | None = None):
    return {"audit": await db.list_audit(limit=limit, action=action)}


@app.get("/api/alerts")
async def alerts(status: str | None = None, limit: int = 200):
    if status not in {None, "open", "acknowledged", "resolved"}:
        raise HTTPException(status_code=400, detail="Invalid alert status")
    return {"alerts": await db.list_alerts(status=status, limit=limit)}


@app.post("/api/alerts/{alert_id}/ack")
async def acknowledge_alert(alert_id: int, payload: AlertAcknowledge):
    try:
        item = await db.acknowledge_alert(alert_id, payload.actor, payload.note)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown alert")
    await db.add_audit(
        "alert.acknowledge", actor=payload.actor, target_type="alert", target_id=str(alert_id),
        data={"note": payload.note},
    )
    return item


@app.post("/api/scheduler/run")
async def run_scheduler_once():
    result = await scheduler.schedule_once()
    execution.kick()
    return result


@app.get("/api/execution")
async def execution_status():
    running = [w for w in await db.running_work() if w.get("dispatch_mode") == "herdr"]
    return {"supervisor": execution.snapshot, "running": running}


@app.post("/api/execution/run")
async def run_execution_once():
    return await execution.run_once()


@app.post("/api/work/{work_id}/dispatch")
async def dispatch_work(work_id: str):
    try:
        return await execution.dispatch(work_id, explicit=True)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown work item")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@app.post("/api/work/{work_id}/retry-dispatch")
async def retry_dispatch(work_id: str, payload: RetryDispatch):
    try:
        return await execution.retry_dispatch(
            work_id, acknowledge_duplicate_risk=payload.acknowledge_duplicate_risk
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown work item")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@app.post("/api/work/{work_id}/recover")
async def recover_work(work_id: str):
    try:
        return await execution.recover_work(work_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown work item")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@app.get("/api/telemetry")
async def telemetry():
    return await db.runtime_metrics()


@app.post("/api/execution/dispatch-ready")
async def dispatch_ready(payload: BatchDispatch):
    try:
        return await execution.dispatch_ready(payload.work_ids)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@app.post("/api/work/{work_id}/fault")
async def inject_fault(work_id: str, payload: FaultInject):
    if settings.studio.production_mode:
        raise HTTPException(status_code=404, detail="Not found")
    try:
        return await execution.inject_fault(
            work_id, kind=payload.kind, ticks=payload.ticks, note=payload.note, stage=payload.stage
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown work item")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.delete("/api/work/{work_id}/fault")
async def clear_fault(work_id: str):
    if settings.studio.production_mode:
        raise HTTPException(status_code=404, detail="Not found")
    return await execution.clear_fault(work_id)

# --------------------------------------------------------------------------
# Computer Use / Web VNC
# --------------------------------------------------------------------------

def _computer_ws_subprotocol(websocket: WebSocket) -> str | None:
    offered = websocket.scope.get("subprotocols") or []
    return "binary" if "binary" in offered else None


def _computer_token_ok(websocket: WebSocket) -> bool:
    expected = settings.studio.computer_auth_token
    if not expected:
        return True
    auth = websocket.headers.get("authorization", "")
    if auth.startswith("Bearer ") and auth[7:] == expected:
        return True
    return websocket.query_params.get("token") == expected


def _require_machine_permission(session: dict[str, Any], category: str) -> None:
    decision = machine_router.authorize_session(session, category)
    if not decision.allowed:
        raise HTTPException(
            status_code=403,
            detail={
                "error": decision.code,
                "message": decision.message,
                "machine_id": decision.machine_id,
                "permission_class": decision.category,
                "machine_policy": decision.policy,
            },
        )


@app.get("/api/computer/status")
async def computer_status():
    return await computer.status()


@app.get("/api/computer/descriptor/{managed_session_id}")
async def computer_descriptor(managed_session_id: str):
    try:
        session = await managed_sessions.get_session(managed_session_id)
        _require_machine_permission(session, "read")
        return await machine_router.computer_descriptor(managed_session_id, session)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown managed session")
    except (RuntimeError, ValueError, OSError, MachineRegistryError) as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@app.get("/api/computer/monitors/{managed_session_id}")
async def computer_monitors(managed_session_id: str):
    try:
        session = await managed_sessions.get_session(managed_session_id)
        _require_machine_permission(session, "read")
        return await machine_router.computer_monitors(session)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown managed session")
    except (RuntimeError, ValueError, OSError, MachineRegistryError) as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@app.post("/api/computer/monitor/{managed_session_id}")
async def computer_monitor_select(managed_session_id: str, request: Request):
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="Request body must be an object")
        mode = str(body.get("mode") or "").strip().lower()
        valid = mode in {"active", "primary", "follow-cursor"}
        if mode.startswith("monitor:"):
            suffix = mode.split(":", 1)[1]
            valid = suffix.isdigit() and int(suffix) >= 1
        if not valid:
            raise HTTPException(
                status_code=400,
                detail="mode must be active, primary, follow-cursor, or monitor:N",
            )
        session = await managed_sessions.get_session(managed_session_id)
        _require_machine_permission(session, "execute")
        return await machine_router.select_computer_monitor(session, mode)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown managed session")
    except (RuntimeError, ValueError, OSError, MachineRegistryError) as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@app.get("/api/computer/browser/profiles/{managed_session_id}")
async def computer_browser_profiles(managed_session_id: str):
    try:
        session = await managed_sessions.get_session(managed_session_id)
        _require_machine_permission(session, "read")
        return await machine_router.browser_profiles(session)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown managed session")
    except (RuntimeError, ValueError, OSError, MachineRegistryError) as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@app.post("/api/computer/browser/profile-alias/{managed_session_id}")
async def computer_browser_profile_alias(managed_session_id: str, request: Request):
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="Request body must be an object")
        alias = str(body.get("alias") or "").strip()
        profile = str(body.get("profile") or "").strip()
        if not alias or not profile:
            raise HTTPException(status_code=400, detail="alias and profile are required")
        session = await managed_sessions.get_session(managed_session_id)
        _require_machine_permission(session, "write")
        return await machine_router.set_browser_profile_alias(
            session,
            alias=alias,
            profile=profile,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown managed session")
    except (RuntimeError, ValueError, OSError, MachineRegistryError) as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@app.post("/api/computer/browser/profile-alias/rename/{managed_session_id}")
async def computer_browser_profile_alias_rename(managed_session_id: str, request: Request):
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="Request body must be an object")
        alias = str(body.get("alias") or "").strip()
        new_alias = str(body.get("new_alias") or "").strip()
        if not alias or not new_alias:
            raise HTTPException(status_code=400, detail="alias and new_alias are required")
        session = await managed_sessions.get_session(managed_session_id)
        _require_machine_permission(session, "write")
        return await machine_router.rename_browser_profile_alias(
            session,
            alias=alias,
            new_alias=new_alias,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown managed session")
    except (RuntimeError, ValueError, OSError, MachineRegistryError) as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@app.post("/api/computer/browser/launch/{managed_session_id}")
async def computer_browser_launch(managed_session_id: str, request: Request):
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="Request body must be an object")
        profile = str(body.get("profile") or "").strip()
        if not profile:
            raise HTTPException(status_code=400, detail="profile is required")
        raw_url = body.get("url")
        url = str(raw_url).strip() if raw_url is not None else None
        raw_monitor = body.get("monitor")
        monitor = str(raw_monitor).strip().lower() if raw_monitor is not None else None
        session = await managed_sessions.get_session(managed_session_id)
        _require_machine_permission(session, "execute")
        return await machine_router.launch_browser_profile(
            session,
            profile=profile,
            url=url,
            monitor=monitor,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown managed session")
    except (RuntimeError, ValueError, OSError, MachineRegistryError) as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@app.get("/api/computer/re-pair-targets/{managed_session_id}")
async def computer_re_pair_targets(managed_session_id: str):
    try:
        session = await managed_sessions.get_session(managed_session_id)
        _require_machine_permission(session, "read")
        machine_router.require_local_computer(session)
        return await computer.repair_targets(managed_session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown managed session")
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except (RuntimeError, ValueError, OSError, MachineRegistryError) as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@app.post("/api/computer/re-pair/{managed_session_id}")
async def computer_re_pair(managed_session_id: str, payload: ComputerRepairRequest):
    try:
        session = await managed_sessions.get_session(managed_session_id)
        _require_machine_permission(session, "execute")
        machine_router.require_local_computer(session)
        return await computer.repair_runtime(
            managed_session_id,
            payload.mode,
            payload.target_display,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown managed session")
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except (RuntimeError, ValueError, OSError, MachineRegistryError) as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@app.get("/computer")
async def computer_view():
    return RedirectResponse(url="/#computer", status_code=307)


@app.websocket("/api/computer/vnc/ws/{managed_session_id}")
async def computer_vnc_ws(websocket: WebSocket, managed_session_id: str):
    if not _computer_token_ok(websocket):
        await websocket.accept(subprotocol=_computer_ws_subprotocol(websocket))
        await websocket.close(code=4401, reason="Computer auth token required")
        return

    try:
        session = await managed_sessions.get_session(managed_session_id)
        _require_machine_permission(session, "execute")
        await machine_router.computer_descriptor(managed_session_id, session)
        transport = await machine_router.computer_transport(managed_session_id, session)
    except KeyError:
        await websocket.accept(subprotocol=_computer_ws_subprotocol(websocket))
        await websocket.close(code=4404, reason="Unknown managed session")
        return
    except (RuntimeError, ValueError, MachineRegistryError):
        await websocket.accept(subprotocol=_computer_ws_subprotocol(websocket))
        await websocket.close(code=1011, reason="Computer runtime unavailable")
        return

    await websocket.accept(subprotocol=_computer_ws_subprotocol(websocket))

    if transport.get("mode") == "tcp":
        try:
            host = str(transport["host"])
            port = int(transport["port"])
            reader, writer = await asyncio.open_connection(host, port)

            async def client_to_vnc() -> None:
                try:
                    while True:
                        event = await websocket.receive()
                        event_type = event.get("type")
                        if event_type == "websocket.disconnect":
                            return
                        if event_type != "websocket.receive":
                            continue
                        payload = event.get("bytes")
                        if payload is None and event.get("text") is not None:
                            payload = event["text"].encode("latin-1")
                        if payload is not None:
                            writer.write(payload)
                            await writer.drain()
                except (WebSocketDisconnect, ConnectionError, asyncio.IncompleteReadError):
                    return

            async def vnc_to_client() -> None:
                try:
                    while True:
                        chunk = await reader.read(65536)
                        if not chunk:
                            return
                        await websocket.send_bytes(chunk)
                except (WebSocketDisconnect, ConnectionError, asyncio.IncompleteReadError):
                    return

            tasks = {
                asyncio.create_task(client_to_vnc()),
                asyncio.create_task(vnc_to_client()),
            }
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*done, *pending, return_exceptions=True)
            writer.close()
            await writer.wait_closed()
        except WebSocketDisconnect:
            pass
        except Exception:
            try:
                await websocket.close(code=1011, reason="VNC runtime unavailable")
            except Exception:
                pass
        return

    if transport.get("mode") != "websocket":
        try:
            await websocket.close(code=1011, reason="Unsupported Computer Use transport")
        except Exception:
            pass
        return

    target = str(transport.get("url") or "")
    if not target:
        try:
            await websocket.close(code=1011, reason="Computer websocket unavailable")
        except Exception:
            pass
        return

    async def client_to_upstream(upstream):
        try:
            while True:
                event = await websocket.receive()
                event_type = event.get("type")
                if event_type == "websocket.disconnect":
                    return
                if event_type != "websocket.receive":
                    continue
                if event.get("bytes") is not None:
                    await upstream.send(event["bytes"])
                elif event.get("text") is not None:
                    await upstream.send(event["text"])
        except WebSocketDisconnect:
            return

    async def upstream_to_client(upstream):
        async for message in upstream:
            if isinstance(message, str):
                await websocket.send_text(message)
            else:
                await websocket.send_bytes(message)

    try:
        async with websockets.connect(target, max_size=None) as upstream:
            tasks = {
                asyncio.create_task(client_to_upstream(upstream)),
                asyncio.create_task(upstream_to_client(upstream)),
            }
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*done, *pending, return_exceptions=True)
    except WebSocketDisconnect:
        pass
    except Exception:
        try:
            await websocket.close(code=1011, reason="VNC bridge unavailable")
        except Exception:
            pass
