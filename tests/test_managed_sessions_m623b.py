from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from starlette.requests import Request

from mcp_studio.db import Database
from mcp_studio.gateway import GatewaySessionManager
from mcp_studio.managed_sessions import ManagedSessionManager, ManagedSessionConflict, WorkspaceNotAllowed
from mcp_studio.settings import ServerConfig, Settings, StudioConfig


def git_repo_with_worktree(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    sibling = tmp_path / "repo-ux"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "HIRDA Test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "hirda@example.invalid"], cwd=repo, check=True)
    (repo / "README.md").write_text("test\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "worktree", "add", "-b", "ux-ui", str(sibling)],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return repo, sibling


def settings_for(tmp_path: Path, *, enabled: bool = True) -> Settings:
    project_root = tmp_path / "projects"
    project_root.mkdir(exist_ok=True)
    serena_root = tmp_path / "serena"
    serena_root.mkdir(exist_ok=True)
    exe = serena_root / "serena"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    return Settings(
        studio=StudioConfig(
            database=str(tmp_path / "studio.sqlite3"),
            gateway_enabled=True,
            gateway_allowed_servers=["serena-8001"],
            worker_server_id="serena-8001",
            managed_session_enabled=enabled,
            managed_session_workspace_roots=[str(project_root)] if enabled else [],
            managed_session_serena_executable=str(exe),
            managed_session_serena_working_directory=str(serena_root),
            managed_session_port_start=43110,
            managed_session_port_end=43120,
            managed_session_start_timeout_seconds=2,
            managed_session_stop_timeout_seconds=1,
            managed_session_require_existing_path=True,
            managed_session_log_dir=str(tmp_path / "logs"),
        ),
        servers=[ServerConfig(id="serena-8001", name="Serena", url="http://127.0.0.1:8001/mcp")],
        tunnels=[],
        config_path=tmp_path / "config.yaml",
    )


@pytest.mark.asyncio
async def test_schema_v6_managed_session_tables_and_gateway_columns(tmp_path: Path):
    db = Database(str(tmp_path / "db.sqlite3"))
    await db.init()
    status = await db.schema_status()
    assert status["current_version"] == 10
    assert status["expected_version"] == 10

    def inspect():
        with db._connect() as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            gateway = {r[1] for r in conn.execute("PRAGMA table_info(gateway_sessions)")}
            studio = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
            return tables, gateway, studio

    tables, gateway, studio = await db._run(inspect)
    assert {"managed_sessions", "managed_workspaces"} <= tables
    assert {"managed_session_id", "upstream_url"} <= gateway
    assert "managed_session_id" in studio


@pytest.mark.asyncio
async def test_managed_runtime_update_rejects_stale_pid_writer(tmp_path: Path):
    db = Database(str(tmp_path / "db.sqlite3"))
    await db.init()
    project = tmp_path / "project"
    project.mkdir()
    await db.upsert_managed_workspace(
        key="race", name="race", project_path=str(project), metadata={"source": "test"}
    )
    session = await db.create_managed_session(
        name="race",
        workspace_key="race",
        project_path=str(project),
        server_id="serena-8001",
        port=43110,
    )
    session_id = session["id"]

    await db.update_managed_session_runtime(session_id, status="ready", pid=200)
    stale = await db.update_managed_session_runtime(
        session_id,
        status="stopped",
        pid=None,
        error=None,
        expected_pid=100,
    )
    assert stale["status"] == "ready"
    assert stale["pid"] == 200

    current = await db.update_managed_session_runtime(
        session_id,
        status="stopped",
        pid=None,
        error=None,
        expected_pid=200,
    )
    assert current["status"] == "stopped"
    assert current["pid"] is None


@pytest.mark.asyncio
async def test_workspace_registry_is_confined_to_approved_roots(tmp_path: Path):
    settings = settings_for(tmp_path)
    db = Database(settings.studio.database); await db.init()
    manager = ManagedSessionManager(settings, db)
    good = tmp_path / "projects" / "alpha"; good.mkdir()
    item = await manager.register_workspace(key="alpha", project_path=str(good))
    assert item["project_path"] == str(good.resolve())
    outside = tmp_path / "outside"; outside.mkdir()
    with pytest.raises(Exception, match="outside approved roots"):
        await manager.register_workspace(key="outside", project_path=str(outside))


@pytest.mark.asyncio
async def test_workspace_registry_allows_registered_git_worktree_sibling_when_opted_in(tmp_path: Path):
    settings = settings_for(tmp_path)
    repo, sibling = git_repo_with_worktree(tmp_path)
    settings.studio.managed_session_workspace_roots = [str(repo)]
    settings.studio.managed_session_allow_git_worktree_siblings = True
    db = Database(settings.studio.database); await db.init()
    manager = ManagedSessionManager(settings, db)

    item = await manager.register_workspace(key="repo-ux", project_path=str(sibling))
    assert item["project_path"] == str(sibling.resolve())

    fake = tmp_path / "repo-copy"
    fake.mkdir()
    with pytest.raises(WorkspaceNotAllowed, match="outside approved roots"):
        await manager.register_workspace(key="repo-copy", project_path=str(fake))


@pytest.mark.asyncio
async def test_use_workspace_selector_accepts_new_absolute_path_and_persists_registration(tmp_path: Path, monkeypatch):
    settings = settings_for(tmp_path)
    db = Database(settings.studio.database); await db.init()
    manager = ManagedSessionManager(settings, db)
    project = tmp_path / "projects" / "oriverse-demo"; project.mkdir()

    async def fake_ensure(session_id):
        item = await db.get_managed_session(session_id)
        return await db.update_managed_session_runtime(
            session_id, status="ready", pid=123, endpoint=f"http://127.0.0.1:{item['port']}/mcp"
        )
    monkeypatch.setattr(manager, "ensure_running", fake_ensure)

    first = await manager.ensure_workspace_selector_session(
        workspace=str(project), actor="chatgpt/mcp"
    )
    assert first["workspace_key"] == "oriverse-demo"
    registered = await db.get_managed_workspace("oriverse-demo")
    assert registered["project_path"] == str(project.resolve())
    assert registered["metadata"]["source"] == "chatgpt/mcp:auto-path"

    # A fresh manager over the same durable DB must resolve the same workspace
    # and managed session rather than registering/allocating again.
    reloaded = ManagedSessionManager(settings, db)
    monkeypatch.setattr(reloaded, "ensure_running", fake_ensure)
    second = await reloaded.ensure_workspace_selector_session(
        workspace=str(project), actor="chatgpt/mcp"
    )
    assert second["id"] == first["id"]
    assert second["port"] == first["port"]
    assert len(await db.list_managed_workspaces()) == 1
    assert len(await db.list_managed_sessions()) == 1


@pytest.mark.asyncio
async def test_use_workspace_selector_reuses_registered_key_for_same_path(tmp_path: Path, monkeypatch):
    settings = settings_for(tmp_path)
    db = Database(settings.studio.database); await db.init()
    manager = ManagedSessionManager(settings, db)
    project = tmp_path / "projects" / "oriverse-demo"; project.mkdir()
    await manager.register_workspace(key="oriverse", project_path=str(project), actor="config-test")

    async def fake_ensure(session_id):
        item = await db.get_managed_session(session_id)
        return await db.update_managed_session_runtime(
            session_id, status="ready", pid=123, endpoint=f"http://127.0.0.1:{item['port']}/mcp"
        )
    monkeypatch.setattr(manager, "ensure_running", fake_ensure)

    by_key = await manager.ensure_workspace_selector_session(workspace="oriverse")
    by_path = await manager.ensure_workspace_selector_session(workspace=str(project))
    assert by_path["id"] == by_key["id"]
    assert by_path["workspace_key"] == "oriverse"
    assert {x["key"] for x in await db.list_managed_workspaces()} == {"oriverse"}


@pytest.mark.asyncio
async def test_use_workspace_selector_allocates_collision_safe_key(tmp_path: Path, monkeypatch):
    settings = settings_for(tmp_path)
    db = Database(settings.studio.database); await db.init()
    manager = ManagedSessionManager(settings, db)
    first_path = tmp_path / "projects" / "a" / "demo"; first_path.mkdir(parents=True)
    second_path = tmp_path / "projects" / "b" / "demo"; second_path.mkdir(parents=True)
    await manager.register_workspace(key="demo", project_path=str(first_path))

    async def fake_ensure(session_id):
        item = await db.get_managed_session(session_id)
        return await db.update_managed_session_runtime(
            session_id, status="ready", pid=123, endpoint=f"http://127.0.0.1:{item['port']}/mcp"
        )
    monkeypatch.setattr(manager, "ensure_running", fake_ensure)

    session = await manager.ensure_workspace_selector_session(workspace=str(second_path))
    assert session["workspace_key"].startswith("demo-")
    assert session["workspace_key"] != "demo"
    generated = await db.get_managed_workspace(session["workspace_key"])
    assert generated["project_path"] == str(second_path.resolve())

    again = await manager.ensure_workspace_selector_session(workspace=str(second_path))
    assert again["workspace_key"] == session["workspace_key"]
    assert again["id"] == session["id"]


@pytest.mark.asyncio
async def test_use_workspace_selector_fails_closed_on_escape_and_nonexistent_path(tmp_path: Path):
    settings = settings_for(tmp_path)
    db = Database(settings.studio.database); await db.init()
    manager = ManagedSessionManager(settings, db)
    project_root = tmp_path / "projects"
    outside = tmp_path / "outside"; outside.mkdir()

    traversal = project_root / ".." / "outside"
    with pytest.raises(WorkspaceNotAllowed, match="outside approved roots"):
        await manager.ensure_workspace_selector_session(workspace=str(traversal))

    link = project_root / "escape-link"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(WorkspaceNotAllowed, match="outside approved roots"):
        await manager.ensure_workspace_selector_session(workspace=str(link))

    missing = project_root / "missing-project"
    with pytest.raises(WorkspaceNotAllowed, match="does not exist"):
        await manager.ensure_workspace_selector_session(workspace=str(missing))


@pytest.mark.asyncio
async def test_config_seed_does_not_overwrite_runtime_workspace_registration(tmp_path: Path):
    settings = settings_for(tmp_path)
    runtime = tmp_path / "projects" / "runtime"; runtime.mkdir()
    configured = tmp_path / "projects" / "configured"; configured.mkdir()
    db = Database(settings.studio.database); await db.init()
    manager = ManagedSessionManager(settings, db)
    await manager.register_workspace(
        key="shared", project_path=str(runtime), actor="chatgpt/mcp:auto-path"
    )
    settings.studio.managed_session_workspaces = {"shared": str(configured)}
    settings.studio.managed_session_auto_restore = False

    await manager.start()
    try:
        durable = await db.get_managed_workspace("shared")
        assert durable["project_path"] == str(runtime.resolve())
        assert durable["metadata"]["source"] == "chatgpt/mcp:auto-path"
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_manager_start_keeps_running_sessions_dormant_by_default(tmp_path: Path, monkeypatch):
    settings = settings_for(tmp_path)
    assert settings.studio.managed_session_auto_restore is False
    # Legacy deployments may still explicitly carry the old eager-restore flag.
    # Cold-start safety must win over that setting.
    settings.studio.managed_session_auto_restore = True
    db = Database(settings.studio.database); await db.init()
    manager = ManagedSessionManager(settings, db)
    project = tmp_path / "projects" / "cold-start"; project.mkdir()
    await manager.register_workspace(key="cold-start", project_path=str(project))
    raw = await db.create_managed_session(
        name="Cold Start", workspace_key="cold-start", project_path=str(project.resolve()),
        server_id="serena-8001", port=43110, desired_state="running",
    )
    ensure_calls = []

    async def unexpected_ensure(session_id):
        ensure_calls.append(session_id)
        raise AssertionError("service startup must not eagerly wake managed Serena runtimes")

    monkeypatch.setattr(manager, "ensure_running", unexpected_ensure)
    await manager.start()
    try:
        assert ensure_calls == []
        dormant = await db.get_managed_session(raw["id"])
        assert dormant["desired_state"] == "running"
        assert dormant["status"] == "stopped"
        assert dormant["pid"] is None
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_spawn_uses_certified_serena_template_and_project_pin(tmp_path: Path, monkeypatch):
    settings = settings_for(tmp_path)
    db = Database(settings.studio.database); await db.init()
    manager = ManagedSessionManager(settings, db)
    project = tmp_path / "projects" / "alpha"; project.mkdir()
    await manager.register_workspace(key="alpha", project_path=str(project))
    raw = await db.create_managed_session(
        name="Alpha", workspace_key="alpha", project_path=str(project.resolve()),
        server_id="serena-8001", port=43110, desired_state="running",
    )
    captured = {}

    class FakeProcess:
        pid = 98765
        returncode = None
        async def wait(self):
            self.returncode = 0
            return 0

    async def fake_create(*args, **kwargs):
        captured["args"] = args; captured["kwargs"] = kwargs
        return FakeProcess()

    async def fake_wait_ready(item, process):
        return None

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_create)
    monkeypatch.setattr(manager, "_wait_ready", fake_wait_ready)
    monkeypatch.setattr(manager, "_port_available", lambda port: True)
    ready = await manager.ensure_running(raw["id"])
    args = list(captured["args"])
    assert args[0] == settings.studio.managed_session_serena_executable
    assert args[1:4] == ["start-mcp-server", "--transport", "streamable-http"]
    assert args[args.index("--port") + 1] == "43110"
    assert args[args.index("--context") + 1] == "chatgpt"
    assert args[args.index("--project") + 1] == str(project.resolve())
    env = captured["kwargs"]["env"]
    runtime_root = (tmp_path / "logs" / ".runtime" / raw["id"]).resolve()
    assert env["XDG_CACHE_HOME"] == str(runtime_root / "cache")
    assert env["XDG_DATA_HOME"] == str(runtime_root / "data")
    assert env["UV_CACHE_DIR"] == str(runtime_root / "cache" / "uv")
    assert env["UV_TOOL_DIR"] == str(runtime_root / "data" / "uv" / "tools")
    assert env["UV_TOOL_BIN_DIR"] == str(runtime_root / "bin")
    assert env["TMPDIR"] == str(runtime_root / "tmp")
    assert (runtime_root / "cache" / "uv").is_dir()
    assert (runtime_root / "data" / "uv" / "tools").is_dir()
    assert (runtime_root / "bin").is_dir()
    assert (runtime_root / "tmp").is_dir()
    assert ready["status"] == "ready"
    assert ready["pid"] == 98765


@pytest.mark.asyncio
async def test_one_running_managed_session_per_project(tmp_path: Path, monkeypatch):
    settings = settings_for(tmp_path)
    db = Database(settings.studio.database); await db.init()
    manager = ManagedSessionManager(settings, db)
    project = tmp_path / "projects" / "alpha"; project.mkdir()
    await manager.register_workspace(key="alpha", project_path=str(project))

    async def fake_ensure(session_id):
        return await db.update_managed_session_runtime(
            session_id, status="ready", pid=123, endpoint="http://127.0.0.1:43110/mcp"
        )
    monkeypatch.setattr(manager, "ensure_running", fake_ensure)
    first = await manager.create_session(name="Alpha", workspace_key="alpha")
    assert first["status"] == "ready"
    with pytest.raises(ManagedSessionConflict):
        await manager.create_session(name="Alpha 2", workspace_key="alpha")


class FakeManagedPool:
    enabled = True
    async def ensure_running(self, session_id):
        return {
            "id": session_id, "name": "Alpha", "workspace_key": "alpha",
            "project_path": "/tmp/alpha", "port": 43110,
            "endpoint": "http://127.0.0.1:43110/mcp", "status": "ready",
        }
    async def get_session(self, session_id):
        if session_id != "ms-alpha": raise KeyError(session_id)
        return await self.ensure_running(session_id)
    async def list_sessions(self):
        return [await self.ensure_running("ms-alpha")]
    async def ensure_workspace_session(self, *, workspace_key, name=None, actor="operator"):
        assert workspace_key == "alpha"
        return await self.ensure_running("ms-alpha")
    async def ensure_workspace_selector_session(self, *, workspace, name=None, actor="operator"):
        assert workspace == "alpha"
        return await self.ensure_running("ms-alpha")
    async def list_workspaces(self): return []


def request_for(body: bytes, gateway_id: str) -> Request:
    headers = [
        (b"content-type", b"application/json"),
        (b"accept", b"application/json, text/event-stream"),
        (b"mcp-session-id", gateway_id.encode()),
    ]
    sent = False
    async def receive():
        nonlocal sent
        if sent: return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}
    return Request({
        "type": "http", "http_version": "1.1", "method": "POST", "scheme": "http",
        "path": "/mcp/serena-8001", "raw_path": b"/mcp/serena-8001", "query_string": b"",
        "headers": headers, "client": ("127.0.0.1", 1), "server": ("test", 80),
    }, receive)


@pytest.mark.asyncio
async def test_project_tool_fails_closed_until_managed_session_is_bound(tmp_path: Path, monkeypatch):
    settings = settings_for(tmp_path)
    db = Database(settings.studio.database); await db.init()
    studio = await db.create_session({"client_id":"c", "client_type":"openai", "server_id":"serena-8001"})
    gw = await db.create_gateway_session(
        studio_session_id=studio["id"], client_id="c", client_type="openai", server_id="serena-8001",
        upstream_session_id="up", protocol_version="2025-06-18",
        init_payload={"jsonrpc":"2.0","id":1,"method":"initialize","params":{}},
    )
    manager = GatewaySessionManager(settings, db, managed_sessions=FakeManagedPool())
    called = False
    async def fake_send(**kwargs):
        nonlocal called; called = True; raise AssertionError("must not reach upstream")
    monkeypatch.setattr(manager, "_send", fake_send)
    body = json.dumps({"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"list_dir","arguments":{}}}).encode()
    response = await manager.proxy_existing(
        request=request_for(body, gw["id"]), server=settings.servers[0], body=body, gateway_session_id=gw["id"]
    )
    payload = json.loads(response.body)
    assert payload["result"]["isError"] is True
    assert "NO_MANAGED_SESSION_BOUND" in payload["result"]["content"][0]["text"]
    assert called is False


@pytest.mark.asyncio
async def test_binding_routes_gateway_to_dedicated_managed_instance(tmp_path: Path, monkeypatch):
    settings = settings_for(tmp_path)
    db = Database(settings.studio.database); await db.init()
    studio = await db.create_session({"client_id":"c", "client_type":"openai", "server_id":"serena-8001"})
    gw = await db.create_gateway_session(
        studio_session_id=studio["id"], client_id="c", client_type="openai", server_id="serena-8001",
        upstream_session_id="base-up", protocol_version="2025-06-18",
        init_payload={"jsonrpc":"2.0","id":1,"method":"initialize","params":{}},
    )
    manager = GatewaySessionManager(settings, db, managed_sessions=FakeManagedPool())
    opened=[]; closed=[]
    async def fake_open(session, url): opened.append(url); return "managed-up"
    async def fake_close(url, upstream): closed.append((url, upstream))
    monkeypatch.setattr(manager, "_open_upstream_session", fake_open)
    monkeypatch.setattr(manager, "_close_upstream_session", fake_close)
    result = await manager.bind_managed_session(gw["id"], "ms-alpha")
    updated = await db.get_gateway_session(gw["id"])
    assert updated["managed_session_id"] == "ms-alpha"
    assert updated["upstream_url"] == "http://127.0.0.1:43110/mcp"
    assert updated["upstream_session_id"] == "managed-up"
    assert opened == ["http://127.0.0.1:43110/mcp"]
    assert closed == [("http://127.0.0.1:8001/mcp", "base-up")]
    assert result["managed_session"]["workspace_key"] == "alpha"


def test_tools_list_injects_session_manager_tools(tmp_path: Path):
    settings = settings_for(tmp_path)
    manager = GatewaySessionManager(settings, Database(settings.studio.database), managed_sessions=FakeManagedPool())
    body = json.dumps({"jsonrpc":"2.0","id":1,"result":{"tools":[{"name":"serena_tool","inputSchema":{"type":"object"}}]}}).encode()
    augmented = json.loads(manager._augment_tools_content(body, "application/json"))
    names = {x["name"] for x in augmented["result"]["tools"]}
    assert "serena_tool" in names
    assert "mcpstudio_create_session" in names
    assert "mcpstudio_use_session" in names
    assert "mcpstudio_use_workspace" in names
    assert "mcpstudio_register_workspace" in names


class FakeHttpClient:
    async def aclose(self):
        return None


def init_request(body: bytes) -> Request:
    sent = False
    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}
    return Request({
        "type": "http", "http_version": "1.1", "method": "POST", "scheme": "http",
        "path": "/mcp/serena-8001", "raw_path": b"/mcp/serena-8001", "query_string": b"",
        "headers": [(b"content-type", b"application/json"), (b"accept", b"application/json, text/event-stream")],
        "client": ("127.0.0.1", 2), "server": ("test", 80),
    }, receive)


@pytest.mark.asyncio
async def test_reclaimed_logical_session_preserves_pin_without_waking_runtime_during_initialize(tmp_path: Path, monkeypatch):
    settings = settings_for(tmp_path)
    db = Database(settings.studio.database); await db.init()
    studio = await db.create_session({"client_id":"stable", "client_type":"chatgpt", "server_id":"serena-8001"})

    def pin():
        with db._connect() as conn:
            conn.execute("UPDATE sessions SET managed_session_id=? WHERE id=?", ("ms-alpha", studio["id"]))
    await db._run(pin)

    manager = GatewaySessionManager(settings, db, managed_sessions=FakeManagedPool())
    targets = []
    ensure_calls = []

    async def unexpected_ensure(session_id):
        ensure_calls.append(session_id)
        raise AssertionError("initialize must not wake a managed Serena runtime")

    async def fake_send(**kwargs):
        targets.append(kwargs["server_url"])
        return FakeHttpClient(), httpx.Response(
            200,
            headers={"content-type":"application/json", "mcp-session-id":"base-up"},
            content=b'{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-06-18","capabilities":{},"serverInfo":{"name":"Serena","version":"1"}}}',
            request=httpx.Request("POST", kwargs["server_url"]),
        )

    monkeypatch.setattr(manager.managed_sessions, "ensure_running", unexpected_ensure)
    monkeypatch.setattr(manager, "_send", fake_send)
    payload = {"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"test","version":"1"}}}
    body = json.dumps(payload).encode()
    response = await manager.initialize(
        request=init_request(body), server=settings.servers[0], body=body, jsonrpc=payload,
        client_id="stable", client_type="chatgpt", identity_scope="explicit", identity_source="test",
    )

    assert ensure_calls == []
    assert targets == ["http://127.0.0.1:8001/mcp"]
    gateway = await db.get_gateway_session(response.headers["mcp-session-id"])
    assert gateway["studio_session_id"] == studio["id"]
    assert gateway["managed_session_id"] is None
    assert gateway["upstream_url"] == "http://127.0.0.1:8001/mcp"
    assert gateway["upstream_session_id"] == "base-up"
    assert response.headers.get("x-mcp-studio-managed-session-id") is None

    logical = await db.get_session(studio["id"])
    assert logical["managed_session_id"] == "ms-alpha"


@pytest.mark.asyncio
async def test_gateway_does_not_converge_logical_pin_for_tools_list(tmp_path: Path, monkeypatch):
    settings = settings_for(tmp_path)
    db = Database(settings.studio.database); await db.init()
    studio = await db.create_session({"client_id":"c", "client_type":"chatgpt", "server_id":"serena-8001"})
    gateway = await db.create_gateway_session(
        studio_session_id=studio["id"], client_id="c", client_type="chatgpt", server_id="serena-8001",
        upstream_session_id="base-up", protocol_version="2025-06-18",
        init_payload={"jsonrpc":"2.0","id":1,"method":"initialize","params":{}},
        upstream_url="http://127.0.0.1:8001/mcp",
    )

    def pin():
        with db._connect() as conn:
            conn.execute("UPDATE sessions SET managed_session_id=? WHERE id=?", ("ms-alpha", studio["id"]))
    await db._run(pin)

    manager = GatewaySessionManager(settings, db, managed_sessions=FakeManagedPool())
    opened=[]; closed=[]; proxied=[]; ensure_calls=[]
    original_ensure = manager.managed_sessions.ensure_running

    async def tracked_ensure(session_id):
        ensure_calls.append(session_id)
        return await original_ensure(session_id)

    async def fake_open(session, url):
        opened.append(url); return "managed-up"
    async def fake_close(url, upstream):
        closed.append((url, upstream))
    async def fake_send(**kwargs):
        proxied.append(kwargs["server_url"])
        return FakeHttpClient(), httpx.Response(
            200, headers={"content-type":"application/json"},
            content=b'{"jsonrpc":"2.0","id":2,"result":{"tools":[]}}',
            request=httpx.Request("POST", kwargs["server_url"]),
        )

    monkeypatch.setattr(manager.managed_sessions, "ensure_running", tracked_ensure)
    monkeypatch.setattr(manager, "_open_upstream_session", fake_open)
    monkeypatch.setattr(manager, "_close_upstream_session", fake_close)
    monkeypatch.setattr(manager, "_send", fake_send)
    body=b'{"jsonrpc":"2.0","id":2,"method":"tools/list"}'
    response=await manager.proxy_existing(
        request=request_for(body, gateway["id"]), server=settings.servers[0], body=body,
        gateway_session_id=gateway["id"],
    )

    assert response.status_code == 200
    updated=await db.get_gateway_session(gateway["id"])
    assert updated["managed_session_id"] is None
    assert updated["upstream_url"] == "http://127.0.0.1:8001/mcp"
    assert updated["upstream_session_id"] == "base-up"
    assert ensure_calls == []
    assert opened == []
    assert closed == []
    assert proxied == ["http://127.0.0.1:8001/mcp"]

    logical = await db.get_session(studio["id"])
    assert logical["managed_session_id"] == "ms-alpha"


@pytest.mark.asyncio
async def test_gateway_lazily_converges_to_logical_session_pin_on_serena_tool_call(tmp_path: Path, monkeypatch):
    settings = settings_for(tmp_path)
    db = Database(settings.studio.database); await db.init()
    studio = await db.create_session({"client_id":"c", "client_type":"chatgpt", "server_id":"serena-8001"})
    gateway = await db.create_gateway_session(
        studio_session_id=studio["id"], client_id="c", client_type="chatgpt", server_id="serena-8001",
        upstream_session_id="base-up", protocol_version="2025-06-18",
        init_payload={"jsonrpc":"2.0","id":1,"method":"initialize","params":{}},
        upstream_url="http://127.0.0.1:8001/mcp",
    )

    def pin():
        with db._connect() as conn:
            conn.execute("UPDATE sessions SET managed_session_id=? WHERE id=?", ("ms-alpha", studio["id"]))
    await db._run(pin)

    manager = GatewaySessionManager(settings, db, managed_sessions=FakeManagedPool())
    opened=[]; closed=[]; proxied=[]; ensure_calls=[]
    original_ensure = manager.managed_sessions.ensure_running

    async def tracked_ensure(session_id):
        ensure_calls.append(session_id)
        return await original_ensure(session_id)

    async def fake_open(session, url):
        opened.append(url); return "managed-up"
    async def fake_close(url, upstream):
        closed.append((url, upstream))
    async def fake_send(**kwargs):
        proxied.append(kwargs["server_url"])
        return FakeHttpClient(), httpx.Response(
            200, headers={"content-type":"application/json"},
            content=b'{"jsonrpc":"2.0","id":3,"result":{"content":[{"type":"text","text":"ok"}],"isError":false}}',
            request=httpx.Request("POST", kwargs["server_url"]),
        )

    monkeypatch.setattr(manager.managed_sessions, "ensure_running", tracked_ensure)
    monkeypatch.setattr(manager, "_open_upstream_session", fake_open)
    monkeypatch.setattr(manager, "_close_upstream_session", fake_close)
    monkeypatch.setattr(manager, "_send", fake_send)
    body = json.dumps({
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "list_dir", "arguments": {"relative_path": "."}},
    }).encode()
    response = await manager.proxy_existing(
        request=request_for(body, gateway["id"]), server=settings.servers[0], body=body,
        gateway_session_id=gateway["id"],
    )

    assert response.status_code == 200
    updated = await db.get_gateway_session(gateway["id"])
    assert updated["managed_session_id"] == "ms-alpha"
    assert updated["upstream_url"] == "http://127.0.0.1:43110/mcp"
    assert updated["upstream_session_id"] == "managed-up"
    assert ensure_calls == ["ms-alpha"]
    assert opened == ["http://127.0.0.1:43110/mcp"]
    assert closed == [("http://127.0.0.1:8001/mcp", "base-up")]
    assert proxied == ["http://127.0.0.1:43110/mcp"]


@pytest.mark.asyncio
async def test_stopped_managed_session_is_resumed_without_port_collision(tmp_path: Path, monkeypatch):
    settings = settings_for(tmp_path)
    db = Database(settings.studio.database); await db.init()
    manager = ManagedSessionManager(settings, db)
    project = tmp_path / "projects" / "alpha"; project.mkdir()
    await manager.register_workspace(key="alpha", project_path=str(project))

    async def fake_ensure(session_id):
        item = await db.get_managed_session(session_id)
        return await db.update_managed_session_runtime(
            session_id, status="ready", pid=123, endpoint=f"http://127.0.0.1:{item['port']}/mcp"
        )
    monkeypatch.setattr(manager, "ensure_running", fake_ensure)

    first = await manager.create_session(name="Alpha", workspace_key="alpha")
    first_id, first_port = first["id"], first["port"]
    await db.set_managed_session_desired_state(first_id, "stopped")
    await db.update_managed_session_runtime(first_id, status="stopped", pid=None)

    resumed = await manager.create_session(name="Alpha again", workspace_key="alpha")
    assert resumed["id"] == first_id
    assert resumed["port"] == first_port
    rows = [x for x in await db.list_managed_sessions() if x["project_path"] == str(project.resolve())]
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_allocator_reserves_ports_owned_by_stopped_durable_sessions(tmp_path: Path, monkeypatch):
    settings = settings_for(tmp_path)
    db = Database(settings.studio.database); await db.init()
    manager = ManagedSessionManager(settings, db)
    alpha = tmp_path / "projects" / "alpha"; alpha.mkdir()
    beta = tmp_path / "projects" / "beta"; beta.mkdir()
    await manager.register_workspace(key="alpha", project_path=str(alpha))
    await manager.register_workspace(key="beta", project_path=str(beta))
    await db.create_managed_session(
        name="Alpha", workspace_key="alpha", project_path=str(alpha.resolve()),
        server_id="serena-8001", port=43110, desired_state="stopped",
    )
    monkeypatch.setattr(manager, "_port_available", lambda port: True)
    assert await manager._allocate_port() == 43111


@pytest.mark.asyncio
async def test_phase_c_use_workspace_routes_and_pins_logical_session(tmp_path: Path, monkeypatch):
    settings = settings_for(tmp_path)
    settings.studio.managed_session_cutover_enabled = True
    db = Database(settings.studio.database); await db.init()
    studio = await db.create_session({"client_id":"c", "client_type":"chatgpt", "server_id":"serena-8001"})
    gw = await db.create_gateway_session(
        studio_session_id=studio["id"], client_id="c", client_type="chatgpt", server_id="serena-8001",
        upstream_session_id="base-up", protocol_version="2025-06-18",
        init_payload={"jsonrpc":"2.0","id":1,"method":"initialize","params":{}},
        upstream_url="http://127.0.0.1:8001/mcp",
    )
    manager = GatewaySessionManager(settings, db, managed_sessions=FakeManagedPool())
    monkeypatch.setattr(manager, "_open_upstream_session", lambda *args, **kwargs: None)
    async def fake_open(session, url): return "managed-up"
    async def fake_close(url, upstream): return None
    monkeypatch.setattr(manager, "_open_upstream_session", fake_open)
    monkeypatch.setattr(manager, "_close_upstream_session", fake_close)
    result = await manager._handle_management_tool(gw["id"], "mcpstudio_use_workspace", {"workspace":"alpha"})
    assert result["managed_session"]["id"] == "ms-alpha"
    updated = await db.get_gateway_session(gw["id"])
    logical = await db.get_session(studio["id"])
    assert updated["managed_session_id"] == "ms-alpha"
    assert logical["managed_session_id"] == "ms-alpha"


@pytest.mark.asyncio
async def test_phase_c_legacy_base_tools_call_is_defensively_blocked(tmp_path: Path, monkeypatch):
    settings = settings_for(tmp_path)
    settings.studio.managed_session_cutover_enabled = True
    settings.studio.managed_session_require_binding_for_tools = False  # defense-in-depth path
    db = Database(settings.studio.database); await db.init()
    studio = await db.create_session({"client_id":"c", "client_type":"openai", "server_id":"serena-8001"})
    gw = await db.create_gateway_session(
        studio_session_id=studio["id"], client_id="c", client_type="openai", server_id="serena-8001",
        upstream_session_id="up", protocol_version="2025-06-18",
        init_payload={"jsonrpc":"2.0","id":1,"method":"initialize","params":{}},
        upstream_url="http://127.0.0.1:8001/mcp",
    )
    manager = GatewaySessionManager(settings, db, managed_sessions=FakeManagedPool())
    called = False
    async def fake_send(**kwargs):
        nonlocal called; called = True; raise AssertionError("legacy upstream must not receive coding tool")
    monkeypatch.setattr(manager, "_send", fake_send)
    body = json.dumps({"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"list_dir","arguments":{}}}).encode()
    response = await manager.proxy_existing(
        request=request_for(body, gw["id"]), server=settings.servers[0], body=body, gateway_session_id=gw["id"]
    )
    payload = json.loads(response.body)
    assert payload["result"]["isError"] is True
    assert "LEGACY_UPSTREAM_BLOCKED" in payload["result"]["content"][0]["text"]
    assert called is False


@pytest.mark.asyncio
async def test_cutover_summary_reports_bound_and_unbound_connected_transports(tmp_path: Path):
    settings = settings_for(tmp_path)
    db = Database(settings.studio.database); await db.init()
    a = await db.create_session({"client_id":"a", "client_type":"chatgpt", "server_id":"serena-8001"})
    b = await db.create_session({"client_id":"b", "client_type":"chatgpt", "server_id":"serena-8001"})
    ga = await db.create_gateway_session(studio_session_id=a["id"], client_id="a", client_type="chatgpt", server_id="serena-8001", upstream_session_id="u1", protocol_version="2025-06-18", init_payload={})
    await db.create_gateway_session(studio_session_id=b["id"], client_id="b", client_type="chatgpt", server_id="serena-8001", upstream_session_id="u2", protocol_version="2025-06-18", init_payload={})
    def pin():
        with db._connect() as conn:
            conn.execute("UPDATE gateway_sessions SET managed_session_id=? WHERE id=?", ("ms-x", ga["id"]))
            conn.execute("UPDATE sessions SET managed_session_id=? WHERE id=?", ("ms-x", a["id"]))
    await db._run(pin)
    summary = await db.managed_cutover_summary()
    assert summary["connected_transports"] == 2
    assert summary["bound_transports"] == 1
    assert summary["unbound_transports"] == 1
    assert summary["pinned_logical_sessions"] == 1


def test_chatgpt_control_tools_are_first_in_catalog(tmp_path: Path):
    settings = settings_for(tmp_path)
    manager = GatewaySessionManager(settings, Database(settings.studio.database), managed_sessions=FakeManagedPool())
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "result": {"tools": [
            {"name": "serena_a", "inputSchema": {"type": "object"}},
            {"name": "serena_b", "inputSchema": {"type": "object"}},
        ]},
    }).encode()
    augmented = json.loads(manager._augment_tools_content(body, "application/json"))
    names = [x["name"] for x in augmented["result"]["tools"]]
    assert names[:3] == [
        "mcpstudio_use_workspace",
        "mcpstudio_create_session",
        "mcpstudio_use_session",
    ]
    assert names[-2:] == ["serena_a", "serena_b"]


def test_sse_tools_list_injection_skips_non_tool_events(tmp_path: Path):
    settings = settings_for(tmp_path)
    manager = GatewaySessionManager(settings, Database(settings.studio.database), managed_sessions=FakeManagedPool())
    sse = (
        'event: message\n'
        'data: {"jsonrpc":"2.0","method":"notifications/progress","params":{"progress":1}}\n\n'
        'event: message\n'
        'data: {"jsonrpc":"2.0","id":9,"result":{"tools":[{"name":"serena_tool","inputSchema":{"type":"object"}}]}}\n\n'
    ).encode()
    augmented = manager._augment_tools_content(sse, "text/event-stream; charset=utf-8").decode()
    data_lines = [line[5:].strip() for line in augmented.splitlines() if line.startswith("data:")]
    first = json.loads(data_lines[0])
    second = json.loads(data_lines[1])
    assert first["method"] == "notifications/progress"
    names = [x["name"] for x in second["result"]["tools"]]
    assert names[:3] == [
        "mcpstudio_use_workspace",
        "mcpstudio_create_session",
        "mcpstudio_use_session",
    ]
    assert "serena_tool" in names


@pytest.mark.asyncio
async def test_tools_list_falls_back_to_local_controls_when_upstream_unreachable(tmp_path: Path, monkeypatch):
    settings = settings_for(tmp_path)
    db = Database(settings.studio.database); await db.init()
    studio = await db.create_session({"client_id":"chatgpt", "client_type":"openai", "server_id":"serena-8001"})
    gw = await db.create_gateway_session(
        studio_session_id=studio["id"], client_id="chatgpt", client_type="openai", server_id="serena-8001",
        upstream_session_id="up", protocol_version="2025-06-18",
        init_payload={"jsonrpc":"2.0","id":1,"method":"initialize","params":{}},
    )
    manager = GatewaySessionManager(settings, db, managed_sessions=FakeManagedPool())

    async def broken_send(**kwargs):
        raise httpx.ConnectError("neutral Serena unavailable")

    monkeypatch.setattr(manager, "_send", broken_send)
    body = json.dumps({"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}).encode()
    response = await manager.proxy_existing(
        request=request_for(body, gw["id"]), server=settings.servers[0], body=body, gateway_session_id=gw["id"]
    )
    assert response.status_code == 200
    payload = json.loads(response.body)
    names = [x["name"] for x in payload["result"]["tools"]]
    assert names[:3] == [
        "mcpstudio_use_workspace",
        "mcpstudio_create_session",
        "mcpstudio_use_session",
    ]
    assert response.headers["mcp-session-id"] == gw["id"]


async def _permission_bound_gateway(tmp_path: Path, *, metadata: dict | None = None):
    settings = settings_for(tmp_path)
    settings.studio.managed_session_tool_permissions_enabled = True
    db = Database(settings.studio.database)
    await db.init()
    project = tmp_path / "projects" / "permission-alpha"
    project.mkdir(parents=True, exist_ok=True)
    managed = await db.create_managed_session(
        name="Permission Alpha",
        workspace_key="permission-alpha",
        project_path=str(project.resolve()),
        server_id="serena-8001",
        port=43110,
        metadata=metadata or {},
    )
    studio = await db.create_session({"client_id": "perm", "client_type": "openai", "server_id": "serena-8001"})
    gw = await db.create_gateway_session(
        studio_session_id=studio["id"],
        client_id="perm",
        client_type="openai",
        server_id="serena-8001",
        upstream_session_id="managed-up",
        protocol_version="2025-06-18",
        init_payload={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    )

    def pin() -> None:
        with db._connect() as conn:
            conn.execute(
                "UPDATE gateway_sessions SET managed_session_id=?, upstream_url=? WHERE id=?",
                (managed["id"], "http://127.0.0.1:43110/mcp", gw["id"]),
            )
            conn.execute("UPDATE sessions SET managed_session_id=? WHERE id=?", (managed["id"], studio["id"]))

    await db._run(pin)
    return settings, db, gw, managed


@pytest.mark.asyncio
async def test_activate_project_cannot_escape_pin_when_blocked_tools_empty(tmp_path: Path, monkeypatch):
    settings, db, gw, _ = await _permission_bound_gateway(tmp_path)
    settings.studio.managed_session_blocked_tools = []
    manager = GatewaySessionManager(settings, db, managed_sessions=FakeManagedPool())
    called = False

    async def fake_send(**kwargs):
        nonlocal called
        called = True
        raise AssertionError("cross-project activate_project must not reach upstream")

    monkeypatch.setattr(manager, "_send", fake_send)
    body = json.dumps({
        "jsonrpc": "2.0", "id": 19, "method": "tools/call",
        "params": {"name": "activate_project", "arguments": {"project": "/tmp/not-the-pinned-project"}},
    }).encode()
    response = await manager.proxy_existing(
        request=request_for(body, gw["id"]),
        server=settings.servers[0],
        body=body,
        gateway_session_id=gw["id"],
    )
    text = json.loads(response.body)["result"]["content"][0]["text"]
    assert "SESSION_PROJECT_PINNED" in text
    assert called is False


@pytest.mark.asyncio
async def test_gateway_permission_blocks_write_for_read_only_session(tmp_path: Path, monkeypatch):
    settings, db, gw, managed = await _permission_bound_gateway(
        tmp_path,
        metadata={"tool_permissions": {"read": True, "write": False, "execute": False, "destructive": False}},
    )
    manager = GatewaySessionManager(settings, db, managed_sessions=FakeManagedPool())
    called = False

    async def fake_send(**kwargs):
        nonlocal called
        called = True
        raise AssertionError("denied write must not reach upstream")

    monkeypatch.setattr(manager, "_send", fake_send)
    body = json.dumps({
        "jsonrpc": "2.0", "id": 20, "method": "tools/call",
        "params": {"name": "replace_content", "arguments": {"relative_path": "src/x.py", "needle": "a", "repl": "b", "mode": "literal"}},
    }).encode()
    response = await manager.proxy_existing(
        request=request_for(body, gw["id"]), server=settings.servers[0], body=body, gateway_session_id=gw["id"]
    )
    payload = json.loads(response.body)
    text = payload["result"]["content"][0]["text"]
    assert payload["result"]["isError"] is True
    assert "TOOL_PERMISSION_DENIED" in text
    assert '"permission_class": "write"' in text
    assert managed["workspace_key"] in text
    assert called is False


@pytest.mark.asyncio
async def test_gateway_permission_blocks_workspace_escape(tmp_path: Path, monkeypatch):
    settings, db, gw, _ = await _permission_bound_gateway(tmp_path)
    manager = GatewaySessionManager(settings, db, managed_sessions=FakeManagedPool())
    called = False

    async def fake_send(**kwargs):
        nonlocal called
        called = True
        raise AssertionError("scope escape must not reach upstream")

    monkeypatch.setattr(manager, "_send", fake_send)
    body = json.dumps({
        "jsonrpc": "2.0", "id": 21, "method": "tools/call",
        "params": {"name": "read_file", "arguments": {"relative_path": "../outside.txt"}},
    }).encode()
    response = await manager.proxy_existing(
        request=request_for(body, gw["id"]), server=settings.servers[0], body=body, gateway_session_id=gw["id"]
    )
    text = json.loads(response.body)["result"]["content"][0]["text"]
    assert "TOOL_SCOPE_VIOLATION" in text
    assert called is False


@pytest.mark.asyncio
async def test_gateway_permission_blocks_unknown_tool_fail_closed(tmp_path: Path, monkeypatch):
    settings, db, gw, _ = await _permission_bound_gateway(tmp_path)
    manager = GatewaySessionManager(settings, db, managed_sessions=FakeManagedPool())
    called = False

    async def fake_send(**kwargs):
        nonlocal called
        called = True
        raise AssertionError("unclassified tool must not reach upstream")

    monkeypatch.setattr(manager, "_send", fake_send)
    body = json.dumps({"jsonrpc": "2.0", "id": 22, "method": "tools/call", "params": {"name": "future_mutator", "arguments": {}}}).encode()
    response = await manager.proxy_existing(
        request=request_for(body, gw["id"]), server=settings.servers[0], body=body, gateway_session_id=gw["id"]
    )
    text = json.loads(response.body)["result"]["content"][0]["text"]
    assert "TOOL_PERMISSION_UNCLASSIFIED" in text
    assert called is False


@pytest.mark.asyncio
async def test_gateway_permission_blocks_destructive_by_default(tmp_path: Path, monkeypatch):
    settings, db, gw, _ = await _permission_bound_gateway(tmp_path)
    manager = GatewaySessionManager(settings, db, managed_sessions=FakeManagedPool())
    called = False

    async def fake_send(**kwargs):
        nonlocal called
        called = True
        raise AssertionError("destructive tool must not reach upstream")

    monkeypatch.setattr(manager, "_send", fake_send)
    body = json.dumps({
        "jsonrpc": "2.0", "id": 23, "method": "tools/call",
        "params": {"name": "safe_delete_symbol", "arguments": {"relative_path": "src/x.py", "name_path_pattern": "x"}},
    }).encode()
    response = await manager.proxy_existing(
        request=request_for(body, gw["id"]), server=settings.servers[0], body=body, gateway_session_id=gw["id"]
    )
    text = json.loads(response.body)["result"]["content"][0]["text"]
    assert "TOOL_PERMISSION_DENIED" in text
    assert '"permission_class": "destructive"' in text
    assert called is False


@pytest.mark.asyncio
async def test_managed_session_permissions_persist_and_surface_in_list(tmp_path: Path):
    settings = settings_for(tmp_path)
    settings.studio.managed_session_tool_permissions_enabled = True
    db = Database(settings.studio.database)
    await db.init()
    project = tmp_path / "projects" / "persist-perms"
    project.mkdir(parents=True, exist_ok=True)
    item = await db.create_managed_session(
        name="Persist permissions",
        workspace_key="persist-perms",
        project_path=str(project.resolve()),
        server_id="serena-8001",
        port=43111,
    )
    manager = ManagedSessionManager(settings, db)
    updated = await manager.update_permissions(
        item["id"],
        {"read": True, "write": False, "execute": True, "destructive": False, "scope": "workspace"},
        actor="test",
    )
    assert updated["metadata"]["tool_permissions"]["write"] is False
    assert updated["metadata"]["tool_permissions_updated_by"] == "test"
    view = await manager.permissions(item["id"])
    assert view["override"]["write"] is False
    assert view["effective"]["write"] is False
    assert view["effective"]["read"] is True
    listed = {x["id"]: x for x in await manager.list_sessions()}
    assert listed[item["id"]]["tool_permissions"]["write"] is False
    assert listed[item["id"]]["tool_permissions_enabled"] is True


@pytest.mark.asyncio
async def test_pinned_activate_project_allows_same_project_and_blocks_cross_project(tmp_path: Path, monkeypatch):
    settings, db, gw, managed = await _permission_bound_gateway(tmp_path)
    settings.studio.managed_session_blocked_tools = ["activate_project"]
    manager = GatewaySessionManager(settings, db, managed_sessions=FakeManagedPool())
    calls = []

    async def fake_send(**kwargs):
        calls.append(kwargs)
        return FakeHttpClient(), httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=b'{"jsonrpc":"2.0","id":30,"result":{"content":[{"type":"text","text":"ok"}],"isError":false}}',
            request=httpx.Request("POST", kwargs["server_url"]),
        )

    monkeypatch.setattr(manager, "_send", fake_send)

    same_body = json.dumps({
        "jsonrpc": "2.0", "id": 30, "method": "tools/call",
        "params": {"name": "activate_project", "arguments": {"project": managed["project_path"]}},
    }).encode()
    same_response = await manager.proxy_existing(
        request=request_for(same_body, gw["id"]),
        server=settings.servers[0],
        body=same_body,
        gateway_session_id=gw["id"],
    )
    same_payload = json.loads(same_response.body)
    assert same_payload["result"]["isError"] is False
    assert len(calls) == 1

    cross_body = json.dumps({
        "jsonrpc": "2.0", "id": 31, "method": "tools/call",
        "params": {"name": "activate_project", "arguments": {"project": str(tmp_path / "other")}},
    }).encode()
    cross_response = await manager.proxy_existing(
        request=request_for(cross_body, gw["id"]),
        server=settings.servers[0],
        body=cross_body,
        gateway_session_id=gw["id"],
    )
    cross_text = json.loads(cross_response.body)["result"]["content"][0]["text"]
    assert "SESSION_PROJECT_PINNED" in cross_text
    assert len(calls) == 1
