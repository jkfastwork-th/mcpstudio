from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(slots=True)
class StudioConfig:
    bind_host: str = "127.0.0.1"
    production_mode: bool = False
    bind_port: int = 8100
    database: str = "./data/mcp-studio.sqlite3"
    poll_interval_seconds: int = 15
    request_timeout_seconds: int = 8
    gateway_enabled: bool = False
    gateway_auth_mode: str = "bearer"
    gateway_token_env: str = "MCP_STUDIO_GATEWAY_TOKEN"
    gateway_allowed_servers: list[str] = field(default_factory=list)
    max_workers: int = 4
    worker_server_id: str | None = None
    worker_heartbeat_timeout_seconds: int = 45
    worker_monitor_interval_seconds: int = 5
    herdr_poll_interval_seconds: int = 30
    scheduler_interval_seconds: float = 1.0
    scheduler_max_queue: int = 1000

    # M3 execution supervisor. Automatic prompt dispatch remains opt-in so an
    # upgrade cannot unexpectedly send work to a live agent. Explicit dispatch
    # via the API/certification script is still available.
    execution_enabled: bool = False
    execution_interval_seconds: float = 2.0
    execution_stall_seconds: int = 180
    execution_max_recoveries: int = 6
    execution_read_result: bool = True
    execution_safe_redispatch: bool = False

    # M4 parallel execution and resilience certification. Fault injection is
    # disabled by default and only mutates MCP Studio's in-memory supervisor
    # state; it never stops panes, Serena, systemd, or host processes.
    execution_parallelism: int = 4
    resilience_test_mode: bool = False

    # M5 connectivity plane. Tunnel supervision is intentionally independent
    # from the execution plane; losing a tunnel never stops workers or Serena.
    connectivity_enabled: bool = True
    connectivity_interval_seconds: float = 5.0
    connectivity_auto_reconnect: bool = True
    tunnel_restart_backoff_seconds: int = 10
    session_stale_seconds: int = 45
    connectivity_test_mode: bool = False

    # M5.2/M5.3 gateway/client session continuity. The gateway virtualizes upstream
    # Streamable HTTP session ids so client reconnects do not erase Studio
    # runtime state and an upstream session 404 can be healed in-place.
    gateway_session_enabled: bool = True
    gateway_session_stale_seconds: int = 120
    gateway_session_auto_reconnect: bool = True
    gateway_session_replay_on_404: bool = True
    gateway_session_test_mode: bool = False

    # M5.3 OpenAI/ChatGPT client compatibility. ChatGPT may not send a
    # conversation-specific identifier to a remote MCP server, so bearer-based
    # continuity is explicitly modeled as connector-scoped, not chat-scoped.
    openai_compatibility_enabled: bool = True
    openai_connector_reclaim_enabled: bool = True
    openai_observation_header_prefixes: list[str] = field(default_factory=lambda: [
        "openai-", "x-openai-", "chatgpt-", "x-chatgpt-", "cf-"
    ])

    # M6.2.3 Phase A unified ingress. OpenAI Secure MCP Tunnel connects to a
    # loopback-only Studio route instead of bypassing Studio to Serena. The
    # route intentionally does not use public OAuth/bearer auth; its security
    # boundary is the local host plus the outbound OpenAI tunnel process.
    openai_local_ingress_enabled: bool = False
    openai_local_ingress_id: str = "openai-serena"
    openai_local_ingress_allowed_hosts: list[str] = field(default_factory=lambda: ["127.0.0.1", "::1"])

    # M6.2.3 Phase B managed sessions. Serena's active project is process-scoped,
    # so each durable managed session owns a dedicated loopback Serena process.
    managed_session_enabled: bool = False
    managed_session_require_binding_for_tools: bool = True
    managed_session_workspace_roots: list[str] = field(default_factory=list)
    managed_session_workspaces: dict[str, str] = field(default_factory=dict)
    managed_session_serena_executable: str = "/home/alfred/ghq/github.com/oraios/serena/.venv/bin/serena"
    managed_session_serena_working_directory: str = "/home/alfred/ghq/github.com/oraios/serena"
    managed_session_context: str = "chatgpt"
    managed_session_port_start: int = 8210
    managed_session_port_end: int = 8299
    managed_session_start_timeout_seconds: float = 30.0
    managed_session_stop_timeout_seconds: float = 8.0
    managed_session_monitor_interval_seconds: float = 5.0
    managed_session_auto_restore: bool = True
    managed_session_auto_restart: bool = True
    managed_session_require_existing_path: bool = True
    managed_session_home: str = "/home/alfred"
    managed_session_path: str = "/home/alfred/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin"
    managed_session_log_dir: str = "./data/managed-sessions"
    managed_session_blocked_tools: list[str] = field(default_factory=lambda: ["activate_project"])
    # M6.2.4 lifecycle polish. Zero keeps idle auto-stop disabled by default;
    # operators can opt in once their workload pattern is understood.
    managed_session_idle_stop_seconds: int = 0
    managed_session_history_limit: int = 100

    # M6.2.3 Phase C production cutover. When enabled, the shared/base Serena
    # instance is discovery/control-only. All Serena tools/call traffic must be
    # routed through a pinned managed session (dedicated Serena process).
    managed_session_cutover_enabled: bool = False
    managed_session_cutover_block_legacy_tools: bool = True

    # M6.2.5 Computer Use / Web VNC MVP. Shared loopback VNC desktop bridged
    # through Studio's authenticated web app. No raw VNC port is exposed to
    # remote clients; the only path in is the Studio websocket route which
    # validates the managed session and optional bearer token.
    computer_use_enabled: bool = False
    computer_vnc_host: str = "127.0.0.1"
    computer_vnc_port: int = 5902
    computer_websockify_host: str = "127.0.0.1"
    computer_websockify_port: int = 6080
    computer_cdp_port: int = 9222
    computer_novnc_dir: str = "/usr/share/novnc"
    computer_auth_token: str | None = None

    def __post_init__(self) -> None:
        if self.computer_use_enabled:
            for label, host in (
                ("computer_vnc_host", self.computer_vnc_host),
                ("computer_websockify_host", self.computer_websockify_host),
            ):
                if not _is_loopback_strict(host):
                    raise ValueError(f"studio.{label} must be loopback only (got {host!r})")
            if not (1024 <= self.computer_vnc_port <= 65535):
                raise ValueError("studio.computer_vnc_port must be between 1024 and 65535")
            if not (1024 <= self.computer_websockify_port <= 65535):
                raise ValueError("studio.computer_websockify_port must be between 1024 and 65535")
            if not (1024 <= self.computer_cdp_port <= 65535):
                raise ValueError("studio.computer_cdp_port must be between 1024 and 65535")
            if self.computer_auth_token and len(self.computer_auth_token) < 8:
                raise ValueError("studio.computer_auth_token must be at least 8 characters when set")

    # M6.2.3 Phase C production cutover. When enabled, the shared/base Serena
    # instance is discovery/control-only. All Serena tools/call traffic must be
    # routed through a pinned managed session (dedicated Serena process).

    # M5.3.1 OAuth facade for ChatGPT custom MCP apps. The public client uses
    # Authorization Code + PKCE S256 and rotating refresh tokens. Client
    # registrations are local/admin-managed; dynamic registration is not
    # exposed in this milestone.
    oauth_enabled: bool = False
    oauth_issuer: str = ""
    oauth_resource_url: str = ""
    oauth_resource_server_id: str | None = None
    oauth_signing_secret_env: str = "MCP_STUDIO_OAUTH_SIGNING_SECRET"
    oauth_owner_token_env: str = "MCP_STUDIO_OAUTH_OWNER_TOKEN"
    oauth_require_pkce: bool = True
    oauth_access_token_ttl_seconds: int = 3600
    oauth_refresh_token_ttl_seconds: int = 2592000
    oauth_authorization_code_ttl_seconds: int = 300
    # Browser-side remembered owner approval. The raw owner token is never stored.
    oauth_owner_session_ttl_seconds: int = 2592000
    oauth_scopes_supported: list[str] = field(default_factory=lambda: ["mcp:serena", "offline_access"])
    oauth_default_scopes: list[str] = field(default_factory=lambda: ["mcp:serena", "offline_access"])

    # M6.1 operations hardening. Reconciliation is conservative: when Studio
    # cannot prove that an already-dispatched upstream action stopped, it
    # detaches local tracking instead of claiming cancellation.
    operations_enabled: bool = True
    operations_interval_seconds: float = 15.0
    cancel_pending_detach_seconds: int = 300
    operations_alerts_enabled: bool = True
    operations_ephemera_retention_days: int = 7
    audit_retention_days: int = 90

    # M6.2 observability and SLOs. Metrics are low-cardinality and local;
    # request bodies and credential values are never persisted.
    observability_enabled: bool = True
    observability_interval_seconds: float = 60.0
    observability_window_minutes: int = 60
    observability_retention_days: int = 30
    observability_alerts_enabled: bool = True
    slo_availability_target_percent: float = 99.9
    slo_mcp_success_target_percent: float = 99.5
    slo_queue_p95_limit_ms: float = 5000.0
    slo_worker_saturation_warn_percent: float = 85.0
    slo_reconnect_rate_warn_per_100_requests: float = 5.0
    slo_oauth_refresh_failures_max: int = 0
    def __post_init__(self) -> None:
        if self.computer_use_enabled:
            if (self.computer_vnc_host or "").strip().lower() not in {"127.0.0.1", "::1"}:
                raise ValueError(f"studio.computer_vnc_host must be loopback only (got {self.computer_vnc_host!r})")
            if (self.computer_websockify_host or "").strip().lower() not in {"127.0.0.1", "::1"}:
                raise ValueError(f"studio.computer_websockify_host must be loopback only (got {self.computer_websockify_host!r})")
            if not (1024 <= self.computer_vnc_port <= 65535):
                raise ValueError("studio.computer_vnc_port must be between 1024 and 65535")
            if not (1024 <= self.computer_websockify_port <= 65535):
                raise ValueError("studio.computer_websockify_port must be between 1024 and 65535")
            if not (1024 <= self.computer_cdp_port <= 65535):
                raise ValueError("studio.computer_cdp_port must be between 1024 and 65535")
            if self.computer_auth_token and len(self.computer_auth_token) < 8:
                raise ValueError("studio.computer_auth_token must be at least 8 characters when set")


@dataclass(slots=True)
class ServerConfig:
    id: str
    name: str
    url: str
    protocol_version: str = "2025-06-18"
    expected_tools: list[str] = field(default_factory=list)
    enabled: bool = True


@dataclass(slots=True)
class TunnelConfig:
    id: str
    provider: str
    name: str
    endpoint: str | None = None
    origin: str | None = None
    health_url: str | None = None
    enabled: bool = True
    managed: bool = False
    autostart: bool = False
    auto_reconnect: bool = True
    tunnel_name: str | None = None
    config_file: str | None = None
    executable: str = "cloudflared"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Settings:
    studio: StudioConfig
    servers: list[ServerConfig]
    tunnels: list[TunnelConfig]
    config_path: Path


def _dataclass_kwargs(cls: type, values: dict[str, Any]) -> dict[str, Any]:
    allowed = set(cls.__dataclass_fields__)
    return {k: v for k, v in values.items() if k in allowed}


def _is_loopback_strict(host: str) -> bool:
    """Strict loopback-only check used by load_settings validation.

    Only real loopback addresses are accepted; 'localhost' nicknames are
    rejected in config validation so the host/port contract is unambiguous.
    """
    h = (host or "").strip().lower()
    return h in {"127.0.0.1", "::1"}


def load_settings(path: str | Path) -> Settings:
    config_path = Path(path).expanduser().resolve()
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    studio = StudioConfig(**_dataclass_kwargs(StudioConfig, raw.get("studio", {})))
    servers = [ServerConfig(**_dataclass_kwargs(ServerConfig, x)) for x in raw.get("servers", [])]
    tunnels = [TunnelConfig(**_dataclass_kwargs(TunnelConfig, x)) for x in raw.get("tunnels", [])]
    if not servers:
        raise ValueError("At least one server must be configured")
    if studio.production_mode:
        unsafe = {
            "resilience_test_mode": studio.resilience_test_mode,
            "connectivity_test_mode": studio.connectivity_test_mode,
            "gateway_session_test_mode": studio.gateway_session_test_mode,
        }
        enabled_unsafe = [name for name, value in unsafe.items() if value]
        if enabled_unsafe:
            raise ValueError(
                "production_mode=true requires all test modes disabled: " + ", ".join(enabled_unsafe)
            )
        if not studio.gateway_enabled:
            raise ValueError("production_mode=true requires gateway_enabled=true")
        if not studio.gateway_session_enabled or not studio.gateway_session_auto_reconnect:
            raise ValueError(
                "production_mode=true requires gateway_session_enabled=true and gateway_session_auto_reconnect=true"
            )
        if not studio.oauth_enabled:
            raise ValueError("production_mode=true requires oauth_enabled=true")
        if not studio.oauth_require_pkce:
            raise ValueError("production_mode=true requires oauth_require_pkce=true")
        if "example.com" in studio.oauth_issuer or "example.com" in studio.oauth_resource_url:
            raise ValueError("production_mode=true refuses example.com OAuth endpoints")
    if studio.gateway_auth_mode not in {"bearer", "none"}:
        raise ValueError("studio.gateway_auth_mode must be bearer or none")
    if studio.gateway_enabled and studio.gateway_auth_mode != "bearer":
        raise ValueError("enabled public MCP gateway requires studio.gateway_auth_mode=bearer")
    if studio.max_workers < 1 or studio.max_workers > 64:
        raise ValueError("studio.max_workers must be between 1 and 64")
    if studio.scheduler_interval_seconds < 0.2:
        raise ValueError("studio.scheduler_interval_seconds must be >= 0.2")
    if studio.scheduler_max_queue < 1 or studio.scheduler_max_queue > 100000:
        raise ValueError("studio.scheduler_max_queue must be between 1 and 100000")
    if studio.execution_interval_seconds < 0.5:
        raise ValueError("studio.execution_interval_seconds must be >= 0.5")
    if studio.execution_stall_seconds < 15:
        raise ValueError("studio.execution_stall_seconds must be >= 15")
    if studio.execution_max_recoveries < 0 or studio.execution_max_recoveries > 100:
        raise ValueError("studio.execution_max_recoveries must be between 0 and 100")
    if studio.execution_parallelism < 1 or studio.execution_parallelism > studio.max_workers:
        raise ValueError("studio.execution_parallelism must be between 1 and studio.max_workers")
    if studio.connectivity_interval_seconds < 1.0:
        raise ValueError("studio.connectivity_interval_seconds must be >= 1.0")
    if studio.tunnel_restart_backoff_seconds < 1:
        raise ValueError("studio.tunnel_restart_backoff_seconds must be >= 1")
    if studio.session_stale_seconds < 5:
        raise ValueError("studio.session_stale_seconds must be >= 5")
    if studio.gateway_session_stale_seconds < 5:
        raise ValueError("studio.gateway_session_stale_seconds must be >= 5")
    if studio.managed_session_port_start < 1024 or studio.managed_session_port_end > 65535:
        raise ValueError("managed session ports must be between 1024 and 65535")
    if studio.managed_session_port_start > studio.managed_session_port_end:
        raise ValueError("managed_session_port_start must be <= managed_session_port_end")
    if studio.managed_session_start_timeout_seconds < 1:
        raise ValueError("managed_session_start_timeout_seconds must be >= 1")
    if studio.managed_session_stop_timeout_seconds < 1:
        raise ValueError("managed_session_stop_timeout_seconds must be >= 1")
    if studio.managed_session_enabled and not studio.managed_session_workspace_roots:
        raise ValueError("managed_session_enabled=true requires managed_session_workspace_roots")
    if studio.managed_session_cutover_enabled:
        if not studio.managed_session_enabled:
            raise ValueError("managed_session_cutover_enabled=true requires managed_session_enabled=true")
        if not studio.managed_session_require_binding_for_tools:
            raise ValueError("managed_session_cutover_enabled=true requires managed_session_require_binding_for_tools=true")
        if not studio.gateway_session_enabled:
            raise ValueError("managed_session_cutover_enabled=true requires gateway_session_enabled=true")
    if studio.computer_use_enabled:
        for label, host in (
            ("computer_vnc_host", studio.computer_vnc_host),
            ("computer_websockify_host", studio.computer_websockify_host),
        ):
            if not _is_loopback_strict(host):
                raise ValueError(f"studio.{label} must be loopback only (got {host!r})")
        if not (1024 <= studio.computer_vnc_port <= 65535):
            raise ValueError("studio.computer_vnc_port must be between 1024 and 65535")
        if not (1024 <= studio.computer_websockify_port <= 65535):
            raise ValueError("studio.computer_websockify_port must be between 1024 and 65535")
        if not (1024 <= studio.computer_cdp_port <= 65535):
            raise ValueError("studio.computer_cdp_port must be between 1024 and 65535")
        if studio.computer_auth_token and len(studio.computer_auth_token) < 8:
            raise ValueError("studio.computer_auth_token must be at least 8 characters when set")
    if studio.oauth_enabled:
        if not studio.oauth_issuer.startswith("https://"):
            raise ValueError("studio.oauth_issuer must be an https:// URL when oauth_enabled=true")
        if studio.oauth_access_token_ttl_seconds < 60:
            raise ValueError("studio.oauth_access_token_ttl_seconds must be >= 60")
        if studio.oauth_refresh_token_ttl_seconds < studio.oauth_access_token_ttl_seconds:
            raise ValueError("studio.oauth_refresh_token_ttl_seconds must be >= access token TTL")
        if studio.oauth_authorization_code_ttl_seconds < 30 or studio.oauth_authorization_code_ttl_seconds > 900:
            raise ValueError("studio.oauth_authorization_code_ttl_seconds must be between 30 and 900")
        if "mcp:serena" not in studio.oauth_scopes_supported:
            raise ValueError("studio.oauth_scopes_supported must include mcp:serena")
        if not set(studio.oauth_default_scopes).issubset(set(studio.oauth_scopes_supported)):
            raise ValueError("studio.oauth_default_scopes must be a subset of oauth_scopes_supported")
    if studio.production_mode and not studio.operations_enabled:
        raise ValueError("production_mode=true requires operations_enabled=true")
    if studio.operations_interval_seconds < 2.0:
        raise ValueError("studio.operations_interval_seconds must be >= 2.0")
    if studio.cancel_pending_detach_seconds < 30:
        raise ValueError("studio.cancel_pending_detach_seconds must be >= 30")
    if studio.operations_ephemera_retention_days < 1:
        raise ValueError("studio.operations_ephemera_retention_days must be >= 1")
    if studio.audit_retention_days < 7:
        raise ValueError("studio.audit_retention_days must be >= 7")
    if studio.production_mode and not studio.observability_enabled:
        raise ValueError("production_mode=true requires observability_enabled=true")
    if studio.observability_interval_seconds < 10.0:
        raise ValueError("studio.observability_interval_seconds must be >= 10.0")
    if studio.observability_window_minutes < 5:
        raise ValueError("studio.observability_window_minutes must be >= 5")
    if studio.observability_retention_days < 1:
        raise ValueError("studio.observability_retention_days must be >= 1")
    for name, value in {
        "slo_availability_target_percent": studio.slo_availability_target_percent,
        "slo_mcp_success_target_percent": studio.slo_mcp_success_target_percent,
        "slo_worker_saturation_warn_percent": studio.slo_worker_saturation_warn_percent,
    }.items():
        if value < 0 or value > 100:
            raise ValueError(f"studio.{name} must be between 0 and 100")
    if studio.slo_queue_p95_limit_ms < 0 or studio.slo_reconnect_rate_warn_per_100_requests < 0:
        raise ValueError("SLO latency/reconnect thresholds must be >= 0")
    if studio.worker_server_id is None:
        studio.worker_server_id = next((s.id for s in servers if s.enabled), servers[0].id)
    if not any(s.id == studio.worker_server_id and s.enabled for s in servers):
        raise ValueError(f"worker_server_id is not an enabled server: {studio.worker_server_id}")
    return Settings(studio=studio, servers=servers, tunnels=tunnels, config_path=config_path)
