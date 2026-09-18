from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


Status = Literal["healthy", "degraded", "down", "unknown"]
WorkerState = Literal["idle", "busy", "degraded", "dead"]
LeaseMode = Literal["none", "write"]
WorkState = Literal["queued", "running", "completed", "failed", "cancelled", "detached"]
DispatchMode = Literal["manual", "herdr"]
ExecutionState = Literal[
    "none",
    "assigned",
    "dispatching",
    "waiting_agent",
    "reconnecting",
    "recovering",
    "stalled",
    "dispatch_uncertain",
    "completed",
    "failed",
    "cancelled",
    "cancel_pending",
    "detached",
]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class LayerStatus(BaseModel):
    name: str
    status: Status = "unknown"
    detail: str | None = None
    latency_ms: float | None = None


class ServerSnapshot(BaseModel):
    server_id: str
    server_name: str
    status: Status = "unknown"
    checked_at: datetime = Field(default_factory=utcnow)
    layers: list[LayerStatus] = Field(default_factory=list)
    tool_count: int = 0
    tool_names: list[str] = Field(default_factory=list)
    schema_hash: str | None = None
    previous_schema_hash: str | None = None
    schema_changed: bool = False
    missing_expected_tools: list[str] = Field(default_factory=list)
    error: str | None = None


class SessionCreate(BaseModel):
    client_id: str
    client_type: str = "chatgpt"
    server_id: str
    workspace: str | None = None
    pane: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SessionHeartbeat(BaseModel):
    workspace: str | None = None
    pane: str | None = None
    metadata: dict[str, Any] | None = None


class WorkerBind(BaseModel):
    workspace: str = Field(min_length=1)
    pane: str | None = None
    agent: str | None = None
    session_id: str | None = None
    lease_mode: LeaseMode = "write"
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorkerHeartbeat(BaseModel):
    state: WorkerState | None = None
    pane: str | None = None
    agent: str | None = None
    work_label: str | None = None
    metadata: dict[str, Any] | None = None


class WorkerStatePatch(BaseModel):
    state: WorkerState
    work_label: str | None = None


class WorkSubmit(BaseModel):
    label: str = Field(min_length=1, max_length=240)
    workspace: str = Field(min_length=1)
    priority: int = Field(default=50, ge=0, le=100)
    lease_mode: LeaseMode = "write"
    session_id: str | None = None
    pane: str | None = None
    agent: str | None = None
    server_id: str | None = None
    dispatch_mode: DispatchMode = "manual"
    instruction: str | None = Field(default=None, max_length=32000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorkFinish(BaseModel):
    result: dict[str, Any] = Field(default_factory=dict)


class WorkFail(BaseModel):
    error: str = Field(min_length=1, max_length=4000)
    result: dict[str, Any] = Field(default_factory=dict)


class WorkDetach(BaseModel):
    reason: str = Field(default="operator_detach", min_length=1, max_length=4000)


class AlertAcknowledge(BaseModel):
    actor: str = Field(default="operator", min_length=1, max_length=120)
    note: str | None = Field(default=None, max_length=1000)


class RetryDispatch(BaseModel):
    acknowledge_duplicate_risk: bool = False


class FaultInject(BaseModel):
    kind: Literal["pane_missing", "mcp_transport"]
    ticks: int = Field(default=1, ge=1, le=10)
    note: str | None = Field(default=None, max_length=240)
    stage: Literal["any", "post_dispatch"] = "any"


class BatchDispatch(BaseModel):
    work_ids: list[str] | None = None

class SessionReclaim(BaseModel):
    client_id: str = Field(min_length=1, max_length=240)
    client_type: str = Field(default="chatgpt", min_length=1, max_length=80)
    server_id: str
    workspace: str | None = None
    pane: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class TunnelRegister(BaseModel):
    id: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9._:-]+$")
    provider: Literal["local", "direct", "cloudflare", "openai", "external"]
    name: str = Field(min_length=1, max_length=240)
    endpoint: str | None = None
    origin: str | None = None
    health_url: str | None = None
    enabled: bool = True
    managed: bool = False
    autostart: bool = False
    auto_reconnect: bool = True
    desired_state: Literal["running", "stopped"] | None = None
    tunnel_name: str | None = None
    config_file: str | None = None
    executable: str = "cloudflared"
    metadata: dict[str, Any] = Field(default_factory=dict)

class ManagedWorkspaceRegister(BaseModel):
    key: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9._-]+$")
    project_path: str = Field(min_length=1, max_length=2000)
    name: str | None = Field(default=None, max_length=240)


class ManagedSessionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=240)
    workspace_key: str = Field(min_length=1, max_length=120)


class ManagedSessionRename(BaseModel):
    name: str = Field(min_length=1, max_length=240)


class ManagedGatewayAttach(BaseModel):
    managed_session_id: str = Field(min_length=1, max_length=120)


AgentId = Literal["claude", "codex", "hermes"]


class CapsuleCreate(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    workspace: str | None = Field(default=None, max_length=2000)
    source_pane: str | None = Field(default=None, max_length=240)
    agent: AgentId = "claude"
    metadata: dict[str, Any] = Field(default_factory=dict)


class CapsuleStageUpdate(BaseModel):
    stage: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9._-]+$")
    agent: AgentId | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class CapsuleHandoff(BaseModel):
    from_agent: AgentId
    to_agent: AgentId
    reason: str = Field(default="manual", min_length=1, max_length=240)
    from_stage: str = Field(default="agent_runtime", min_length=1, max_length=120)
    to_stage: str = Field(default="agent_runtime", min_length=1, max_length=120)
    metadata: dict[str, Any] = Field(default_factory=dict)


class CapsuleComplete(BaseModel):
    agent: AgentId | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
