"""Exercise the real subprocess/JSON boundary with an absent Unix socket."""
import os
import subprocess
import sys

import pytest

from mcp_studio.db import Database
from mcp_studio.gateway import GatewaySessionManager
from mcp_studio.health import HealthManager
from mcp_studio.herdr import HerdrManager, HerdrUnavailable
from mcp_studio.settings import Settings, StudioConfig, ServerConfig


@pytest.fixture
def offline_cli(tmp_path):
    # A subprocess reproduces the CLI protocol; it attempts a real absent socket.
    cli = tmp_path / "herdr"
    cli.write_text("#!/usr/bin/env python3\n" +
        "import json, socket, sys\n" +
        f"with open({str(tmp_path / 'calls')!r}, 'a') as calls: calls.write('probe\\n')\n" +
        f"sock = socket.socket(socket.AF_UNIX); path = {str(tmp_path / 'absent.sock')!r}\n" +
        "try: sock.connect(path)\n" +
        "except FileNotFoundError:\n" +
        " print(json.dumps({'id':'cli:tab:list','error':{'code':'server_not_running','message':'no herdr server'}})); sys.exit(1)\n")
    cli.chmod(0o755)
    return cli


async def make_boundary(tmp_path, cli, monkeypatch):
    settings = Settings(studio=StudioConfig(database=str(tmp_path / "test.sqlite"),
        worker_server_id="offline"), servers=[ServerConfig(id="offline", name="offline",
        url="http://127.0.0.1:1/mcp")], tunnels=[], config_path=tmp_path / "config.yaml")
    db = Database(settings.studio.database)
    await db.init()
    herdr = HerdrManager(settings, db, HealthManager(settings, db))
    monkeypatch.setattr(herdr, "_herdr_cli_binary", lambda: str(cli))
    gateway = GatewaySessionManager(settings, db)
    gateway.herdr = herdr
    return db, herdr, gateway


@pytest.mark.asyncio
@pytest.mark.parametrize("task_status", ["active", "expired"])
async def test_absent_socket_defers_without_replacing_or_mutating_task(tmp_path, offline_cli, monkeypatch, task_status):
    db, herdr, gateway = await make_boundary(tmp_path, offline_cli, monkeypatch)
    task = {"task_id":"TASK-6CD46979BC14", "trace_id":"trace-original",
            "status":task_status, "execution_state":"running", "work_id":"work-original",
            "lifecycle":{"revision":1}, "herdr_tab":{"owner":"pstack", "status":"open",
            "tab_id":"original-tab"}}
    await db.create_session({"client_id":"original", "server_id":"offline",
        "metadata":{"active_pstack_task":task}})
    before = await db.list_sessions()
    for _ in range(3):
        result = await gateway.reconcile_pstack_terminal_tabs()
        assert result["status"] == "deferred"
        assert result["reason"] == "server_not_running"
        assert await db.list_sessions() == before
        assert await db.list_gateway_sessions() == []
        assert await db.list_managed_sessions() == []
    with pytest.raises(HerdrUnavailable, match="server_not_running"):
        await herdr.reconcile_disposable_task_tabs()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError("programming error"), ValueError("invariant")])
async def test_unexpected_reconciliation_failure_surfaces(tmp_path, offline_cli, monkeypatch, error):
    _, herdr, gateway = await make_boundary(tmp_path, offline_cli, monkeypatch)
    async def broken():
        raise error
    monkeypatch.setattr(herdr, "probe_execution", broken)
    with pytest.raises(type(error), match=str(error)):
        await gateway.reconcile_pstack_terminal_tabs()


@pytest.mark.asyncio
async def test_bad_canonical_revision_surfaces_even_when_offline(tmp_path, offline_cli, monkeypatch):
    db, _, gateway = await make_boundary(tmp_path, offline_cli, monkeypatch)
    await db.create_session({"client_id":"bad", "server_id":"offline", "metadata":{
        "active_pstack_task":{"task_id":"bad", "lifecycle":{"revision":"corrupt"}}}})
    with pytest.raises(ValueError):
        await gateway.reconcile_pstack_terminal_tabs()


@pytest.mark.parametrize("unexpected", [False, True])
def test_real_lifespan_and_http_remain_available_across_repeated_startup(tmp_path, offline_cli, unexpected):
    config = tmp_path / "config.yaml"
    config.write_text(f"""studio:
  database: {tmp_path / 'startup.sqlite'}
  worker_server_id: offline
  connectivity_enabled: false
  cognitive_runtime_auto_recovery_enabled: false
  operations_enabled: false
  observability_enabled: false
  reflex_dataset_enabled: false
servers:
  - id: offline
    name: offline
    url: http://127.0.0.1:1/mcp
""")
    script = """
import asyncio, httpx
from mcp_studio import main
async def unexpected_execution_reconciliation():
    raise AssertionError('execution reconciliation must be skipped while Herdr is offline')
main.gateway_sessions.reconcile_pstack_execution_work = unexpected_execution_reconciliation
async def run():
    for _ in range(2):
        async with main.lifespan(main.app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://local') as client:
                response = await client.get('/healthz')
                assert response.status_code == 200, response.text
                assert response.json()['ok'] is True
                assert main.app.state.herdr_startup_reconciliation == {'status':'deferred', 'reason':'server_not_running'}
                assert (await client.get('/.well-known/oauth-authorization-server')).status_code != 500
asyncio.run(run())
"""
    if unexpected:
        script = script.replace("async def run():", "async def broken():\n    raise RuntimeError('unexpected reconciliation bug')\nmain.gateway_sessions.reconcile_pstack_terminal_tabs = broken\nasync def run():")
    env = {**os.environ, "MCP_STUDIO_CONFIG":str(config),
           "PATH":str(offline_cli.parent) + os.pathsep + os.environ["PATH"]}
    result = subprocess.run([sys.executable, "-c", script], env=env,
                            capture_output=True, text=True, timeout=60)
    if unexpected:
        assert result.returncode != 0
        assert "unexpected reconciliation bug" in result.stderr
        return
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "calls").read_text().splitlines() == ["probe", "probe"]
    assert "application startup failed" not in result.stderr.lower()
    assert result.stderr.count("startup terminal-tab reconciliation deferred") == 2


@pytest.mark.asyncio
async def test_unknown_cli_error_is_not_classified_as_offline(tmp_path, monkeypatch):
    cli = tmp_path / "bad-herdr"
    cli.write_text("#!/bin/sh\nprintf '%s\\n' '{\"error\":{\"code\":\"invariant_violation\"}}'\nexit 1\n")
    cli.chmod(0o755)
    _, _, gateway = await make_boundary(tmp_path, cli, monkeypatch)
    with pytest.raises(RuntimeError, match="herdr_cli_failed"):
        await gateway.reconcile_pstack_terminal_tabs()


@pytest.mark.asyncio
async def test_database_error_surfaces_while_executor_offline(tmp_path, offline_cli, monkeypatch):
    import sqlite3
    db, _, gateway = await make_boundary(tmp_path, offline_cli, monkeypatch)
    async def corrupt(*args, **kwargs):
        raise sqlite3.DatabaseError("database disk image is malformed")
    monkeypatch.setattr(db, "list_sessions", corrupt)
    with pytest.raises(sqlite3.DatabaseError):
        await gateway.reconcile_pstack_terminal_tabs()


@pytest.mark.asyncio
async def test_real_installed_herdr_absent_named_session(tmp_path, monkeypatch):
    import shlex
    import shutil
    import uuid
    binary = shutil.which("herdr")
    if binary is None:
        pytest.skip("Herdr CLI not installed; protocol subprocess tests still run")
    session = "hirda-p0-absent-" + uuid.uuid4().hex
    cli = tmp_path / "real-offline-herdr"
    cli.write_text("#!/bin/sh\nexec " + shlex.quote(binary) + " --session " + session + ' "$@"\n')
    cli.chmod(0o755)
    _, herdr, gateway = await make_boundary(tmp_path, cli, monkeypatch)
    result = await gateway.reconcile_pstack_terminal_tabs()
    assert result["status"] == "deferred"
    assert result["reason"] == "server_not_running"


@pytest.mark.asyncio
async def test_unexpected_terminal_tab_close_failure_surfaces(tmp_path, monkeypatch):
    cli = tmp_path / "healthy-herdr"
    cli.write_text("#!/bin/sh\nprintf '%s\\n' '{\"result\":{\"tabs\":[]}}'\n")
    cli.chmod(0o755)
    db, herdr, gateway = await make_boundary(tmp_path, cli, monkeypatch)
    await db.create_session({"client_id":"terminal", "server_id":"offline", "metadata":{
        "active_pstack_task":{"task_id":"terminal", "status":"completed",
        "herdr_tab":{"owner":"pstack", "status":"open", "tab_id":"original"}}}})
    async def broken_close(tab):
        raise ValueError("unexpected close invariant")
    monkeypatch.setattr(herdr, "close_task_tab", broken_close)
    with pytest.raises(ValueError, match="unexpected close invariant"):
        await gateway.reconcile_pstack_terminal_tabs()
