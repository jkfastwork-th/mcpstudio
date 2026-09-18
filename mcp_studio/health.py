from __future__ import annotations

import asyncio
import socket
import time
from urllib.parse import urlparse

from .db import Database
from .mcp_client import MCPClient
from .models import LayerStatus, ServerSnapshot
from .settings import ServerConfig, Settings


class HealthManager:
    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self.snapshots: dict[str, ServerSnapshot] = {}
        self._last_hash: dict[str, str] = {}
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        await self.poll_once()
        self._task = asyncio.create_task(self._loop(), name="mcp-studio-health")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        while not self._stopping.is_set():
            try:
                await asyncio.wait_for(
                    self._stopping.wait(),
                    timeout=max(3, self.settings.studio.poll_interval_seconds),
                )
            except asyncio.TimeoutError:
                await self.poll_once()

    async def poll_once(self) -> None:
        enabled = [server for server in self.settings.servers if server.enabled]
        await asyncio.gather(*(self._probe_server(server) for server in enabled), return_exceptions=True)

    async def _tcp_probe(self, server: ServerConfig) -> LayerStatus:
        parsed = urlparse(server.url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        started = time.perf_counter()
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=3)
            writer.close()
            await writer.wait_closed()
            return LayerStatus(
                name="network",
                status="healthy",
                detail=f"TCP {host}:{port} reachable",
                latency_ms=(time.perf_counter() - started) * 1000,
            )
        except (OSError, asyncio.TimeoutError, socket.gaierror) as exc:
            return LayerStatus(
                name="network",
                status="down",
                detail=f"TCP {host}:{port} failed: {exc}",
                latency_ms=(time.perf_counter() - started) * 1000,
            )

    async def _probe_server(self, server: ServerConfig) -> None:
        previous = self.snapshots.get(server.id)
        network = await self._tcp_probe(server)
        snapshot = ServerSnapshot(server_id=server.id, server_name=server.name, layers=[network])
        if network.status != "healthy":
            snapshot.status = "down"
            snapshot.error = network.detail
            await self._record_transition(previous, snapshot)
            self.snapshots[server.id] = snapshot
            return

        started = time.perf_counter()
        try:
            client = MCPClient(
                server.url,
                timeout=self.settings.studio.request_timeout_seconds,
                protocol_version=server.protocol_version,
            )
            probe = await client.probe()
            names = sorted(t.get("name", "") for t in probe.tools if t.get("name"))
            missing = sorted(set(server.expected_tools) - set(names))
            old_hash = self._last_hash.get(server.id)
            changed = bool(old_hash and old_hash != probe.schema_hash)
            snapshot.layers.append(
                LayerStatus(
                    name="mcp",
                    status="healthy",
                    detail=f"initialize + tools/list OK ({len(names)} tools)",
                    latency_ms=(time.perf_counter() - started) * 1000,
                )
            )
            snapshot.layers.append(
                LayerStatus(
                    name="schema",
                    status="degraded" if missing else "healthy",
                    detail=(
                        "Missing expected tools: " + ", ".join(missing)
                        if missing
                        else f"sha256:{probe.schema_hash[:12]}"
                    ),
                )
            )
            snapshot.tool_count = len(names)
            snapshot.tool_names = names
            snapshot.schema_hash = probe.schema_hash
            snapshot.previous_schema_hash = old_hash
            snapshot.schema_changed = changed
            snapshot.missing_expected_tools = missing
            snapshot.status = "degraded" if missing else "healthy"
            self._last_hash[server.id] = probe.schema_hash
            if changed:
                await self.db.add_event(
                    "schema.changed",
                    f"{server.name} tool schema changed",
                    severity="warning",
                    server_id=server.id,
                    data={"previous": old_hash, "current": probe.schema_hash},
                )
                if self.settings.studio.operations_alerts_enabled:
                    await self.db.open_alert(
                        f"schema:{server.id}",
                        "schema.changed",
                        f"{server.name} MCP tool schema changed",
                        severity="warning",
                        data={"server_id": server.id, "previous": old_hash, "current": probe.schema_hash},
                    )
        except Exception as exc:
            snapshot.layers.append(
                LayerStatus(
                    name="mcp",
                    status="down",
                    detail=str(exc),
                    latency_ms=(time.perf_counter() - started) * 1000,
                )
            )
            snapshot.status = "down"
            snapshot.error = str(exc)
        await self._record_transition(previous, snapshot)
        self.snapshots[server.id] = snapshot

    async def _record_transition(self, previous: ServerSnapshot | None, current: ServerSnapshot) -> None:
        if previous is None or previous.status != current.status:
            await self.db.add_event(
                "server.status",
                f"{current.server_name}: {previous.status if previous else 'unknown'} -> {current.status}",
                severity="warning" if current.status != "healthy" else "info",
                server_id=current.server_id,
                data={"from": previous.status if previous else "unknown", "to": current.status},
            )
            if self.settings.studio.operations_alerts_enabled:
                key = f"server:{current.server_id}:health"
                if current.status == "healthy":
                    await self.db.resolve_alert(key)
                else:
                    await self.db.open_alert(
                        key,
                        "server.health",
                        f"{current.server_name} is {current.status}",
                        severity="critical" if current.status == "down" else "warning",
                        data={"server_id": current.server_id, "status": current.status, "error": current.error},
                    )
