from __future__ import annotations

import asyncio
import html
import os
from typing import Any

import websockets
from urllib.parse import parse_qs
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
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
    SessionReclaim, TunnelRegister, ManagedWorkspaceRegister, ManagedSessionCreate, ManagedSessionRename, ManagedSessionPermissionsUpdate,
    GraftConfigureRequest, GraftQueryRequest, GraftRollbackRequest, ComputerRepairRequest, ManagedGatewayAttach, GatewayContextUsageReport, SessionHandoffPrepareRequest,
    CapsuleCreate, CapsuleStageUpdate, CapsuleHandoff, CapsuleHandoffAck, CapsuleContractUpdate,
    CapsuleHandoffValidation, CapsuleHandoffApproval, LaneStateUpdate, CapsuleComplete,
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
from .reflex_metrics import ReflexMetrics
from .action_adapter import evaluate_action_envelope
from .action_registry import ActionProviderRegistryError, build_action_provider_registry
from .cognitive_router import CognitiveRouter, CognitiveRouterError
from .world_authoring import WorldAuthoringManager


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
world_authoring = WorldAuthoringManager(
    managed_sessions,
    validator_url=os.environ.get("HIRDA_EARTH_WORLD_VALIDATOR_URL"),
)
gateway_sessions = GatewaySessionManager(
    settings,
    db,
    oauth,
    managed_sessions,
    graft,
    capsules,
    world_authoring,
)
operations = OperationsManager(settings, db)
observability = ObservabilityManager(settings, db)
computer = ComputerUseManager(settings.studio, db)
reflex_metrics = ReflexMetrics(settings.studio)
action_provider_registry = build_action_provider_registry(settings.studio)
cognitive_router = CognitiveRouter(settings.studio, herdr, agent_runtimes, capsules)
templates = Jinja2Templates(directory=str(ROOT / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init()
    await workers.start()
    await health.start()
    await herdr.start()
    await managed_sessions.start()
    await operations.start()
    await observability.start()
    await scheduler.start()
    await execution.start()
    await connectivity.start()
    await db.add_audit("studio.start", actor="system", data={"version": "0.9.10-m6.2.5", "production_mode": settings.studio.production_mode})
    yield
    await db.add_audit("studio.stop", actor="system", data={"version": "0.9.10-m6.2.5"})
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
            path="/oauth",
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
        "servers": snapshots,
        "tunnels": await db.list_tunnels(),
    }


@app.get("/api/reflex/metrics")
async def reflex_metrics_status(window: str = "24h", recent: int = 20):
    return reflex_metrics.snapshot(window=window, recent=recent)


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


@app.get("/api/cognition/status")
async def cognitive_router_status():
    snapshot = agent_runtimes.snapshot(force=False)
    return {
        "schema": "hirda-cognitive-status-v1",
        "enabled": bool(settings.studio.cognitive_router_enabled),
        "execute_enabled": bool(settings.studio.cognitive_router_execute_enabled),
        "runtimes": snapshot,
        "identity_authority": "external",
        "semantic_authority": "external",
    }


@app.post("/api/cognition/plan")
async def cognitive_router_plan(payload: dict[str, Any]):
    if not settings.studio.cognitive_router_enabled:
        raise HTTPException(status_code=503, detail="HIRDA cognitive router is disabled")
    try:
        return await cognitive_router.plan(payload)
    except CognitiveRouterError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/cognition/execute")
async def cognitive_router_execute(payload: dict[str, Any]):
    if not settings.studio.cognitive_router_enabled:
        raise HTTPException(status_code=503, detail="HIRDA cognitive router is disabled")
    if not settings.studio.cognitive_router_execute_enabled:
        raise HTTPException(status_code=503, detail="HIRDA cognitive execution is disabled")
    try:
        return (await cognitive_router.execute(payload)).as_dict()
    except CognitiveRouterError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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


@app.get("/api/computer/status")
async def computer_status():
    return await computer.status()


@app.get("/api/computer/descriptor/{managed_session_id}")
async def computer_descriptor(managed_session_id: str):
    try:
        return await computer.descriptor(managed_session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown managed session")
    except (RuntimeError, ValueError, OSError) as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@app.get("/api/computer/re-pair-targets/{managed_session_id}")
async def computer_re_pair_targets(managed_session_id: str):
    try:
        return await computer.repair_targets(managed_session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown managed session")
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except (RuntimeError, ValueError, OSError) as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@app.post("/api/computer/re-pair/{managed_session_id}")
async def computer_re_pair(managed_session_id: str, payload: ComputerRepairRequest):
    try:
        return await computer.repair_runtime(
            managed_session_id,
            payload.mode,
            payload.target_display,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown managed session")
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except (RuntimeError, ValueError, OSError) as exc:
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
        await computer.descriptor(managed_session_id)
    except KeyError:
        await websocket.accept(subprotocol=_computer_ws_subprotocol(websocket))
        await websocket.close(code=4404, reason="Unknown managed session")
        return
    except (RuntimeError, ValueError):
        await websocket.accept(subprotocol=_computer_ws_subprotocol(websocket))
        await websocket.close(code=1011, reason="Computer runtime unavailable")
        return

    await websocket.accept(subprotocol=_computer_ws_subprotocol(websocket))

    if computer.session_isolation_enabled:
        try:
            host, port = await computer.tcp_target(managed_session_id)
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

    target = computer.websocket_target()

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
