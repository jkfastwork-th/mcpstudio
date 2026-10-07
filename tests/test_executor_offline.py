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
        "from pathlib import Path\n" +
        f"with open({str(tmp_path / 'calls')!r}, 'a') as calls: calls.write('probe\\n')\n" +
        f"sock = socket.socket(socket.AF_UNIX); path = {str(tmp_path / 'absent.sock')!r}\n" +
        f"if Path({str(tmp_path / 'available')!r}).exists():\n" +
        " print(json.dumps({'result':{'tabs':[]}})); sys.exit(0)\n" +
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
    seed = """
import asyncio, json
from pathlib import Path
from mcp_studio import main
async def seed():
    await main.db.init()
    work = await main.db.create_work({'workspace':'original', 'label':'original work',
        'dispatch_mode':'herdr', 'metadata':{'pstack_task_id':'TASK-6CD46979BC14'}}, 'offline')
    task = {'task_id':'TASK-6CD46979BC14', 'trace_id':'trace-original', 'status':'active',
        'execution_state':'waiting', 'work_id':work['id'],
        'lifecycle':{'revision':7, 'status':'active', 'current_phase':'coding',
            'checkpoints':[{'phase':'plan', 'evidence':'original checkpoint'}]},
        'herdr_tab':{'owner':'pstack', 'status':'open', 'tab_id':'original-tab'}}
    logical = await main.db.create_session({'client_id':'original', 'server_id':'offline',
        'metadata':{'active_pstack_task':task}})
    managed = await main.db.create_managed_session(name='original', workspace_key='original',
        project_path=str(Path(main.settings.studio.database).parent), server_id='offline',
        port=18001, desired_state='stopped', metadata={'active_pstack_task':task})
    await main.db.create_gateway_session(studio_session_id=logical['id'], client_id='original',
        client_type='chatgpt', server_id='offline', upstream_session_id=None,
        protocol_version=None, init_payload={}, managed_session_id=managed['id'],
        metadata={'active_pstack_task':task})
    await main.db.add_event('pstack.lifecycle.checkpoint', 'original checkpoint',
        data={'task_id':task['task_id'], 'trace_id':task['trace_id'], 'lifecycle':task['lifecycle']})
    baseline = {'task':task, 'work':await main.db.list_work(),
        'checkpoints':await main.db.list_events(kind_prefix='pstack.lifecycle.checkpoint')}
    Path(main.settings.studio.database + '.baseline').write_text(json.dumps(baseline))
asyncio.run(seed())
"""
    script = """
import asyncio, json, httpx
from pathlib import Path
from mcp_studio import main
async def assert_preserved():
    baseline = json.loads(Path(main.settings.studio.database + '.baseline').read_text())
    for lister in (main.db.list_sessions, main.db.list_gateway_sessions, main.db.list_managed_sessions):
        rows = await lister()
        assert len(rows) == 1, rows
        assert rows[0]['metadata']['active_pstack_task'] == baseline['task'], rows
    assert await main.db.list_work() == baseline['work']
    assert await main.db.list_events(kind_prefix='pstack.lifecycle.checkpoint') == baseline['checkpoints']
async def run():
    async with main.lifespan(main.app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://local') as client:
            for _ in range(3):
                assert (await client.get('/healthz')).status_code == 200
                activity = await client.get('/api/pstack/activity')
                assert activity.status_code == 200, activity.text
                assert 'TASK-6CD46979BC14' in activity.text
                assert 'TASK-934E2F1E6CC3' not in activity.text
                assert main.app.state.herdr_startup_reconciliation == {'status':'deferred', 'reason':'server_not_running'}
                await assert_preserved()
                await asyncio.sleep(0.6)
    await assert_preserved()
asyncio.run(run())
"""
    if unexpected:
        script = script.replace("async def run():", "async def broken():\n    raise RuntimeError('unexpected reconciliation bug')\nmain.gateway_sessions.reconcile_pstack_terminal_tabs = broken\nasync def run():")
    env = {**os.environ, "MCP_STUDIO_CONFIG":str(config),
           "PATH":str(offline_cli.parent) + os.pathsep + os.environ["PATH"]}
    seeded = subprocess.run([sys.executable, "-c", seed], env=env,
                            capture_output=True, text=True, timeout=30)
    assert seeded.returncode == 0, seeded.stdout + seeded.stderr
    for _ in range(1 if unexpected else 3):
        result = subprocess.run([sys.executable, "-c", script], env=env,
                                capture_output=True, text=True, timeout=30)
        if unexpected:
            assert result.returncode != 0
            assert "unexpected reconciliation bug" in result.stderr
            return
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stderr.count("startup terminal-tab reconciliation deferred") == 1
    assert (tmp_path / "calls").read_text().splitlines() == ["probe"] * 3


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


@pytest.mark.asyncio
async def test_deferred_terminal_cleanup_can_reconcile_later(tmp_path, offline_cli, monkeypatch):
    db, _, gateway = await make_boundary(tmp_path, offline_cli, monkeypatch)
    task = {"task_id":"TASK-6CD46979BC14", "trace_id":"original", "status":"completed",
            "lifecycle":{"revision":7, "checkpoints":[{"evidence":"preserved"}]},
            "herdr_tab":{"owner":"pstack", "status":"open", "tab_id":"old-tab"}}
    session = await db.create_session({"client_id":"original", "server_id":"offline",
                                      "metadata":{"active_pstack_task":task}})
    assert (await gateway.reconcile_pstack_terminal_tabs())["status"] == "deferred"
    assert (await db.get_session(session["id"]))["metadata"]["active_pstack_task"] == task
    (tmp_path / "available").touch()
    result = await gateway.reconcile_pstack_terminal_tabs()
    assert result["terminal_closed"] == 1
    preserved = (await db.get_session(session["id"]))["metadata"]["active_pstack_task"]
    assert preserved["task_id"] == task["task_id"]
    assert preserved["lifecycle"] == task["lifecycle"]
    assert preserved["herdr_tab"]["status"] == "closed"
    assert (await gateway.reconcile_pstack_terminal_tabs())["patched"] == 0
    assert len(await db.list_sessions()) == 1
    assert await db.list_work() == []


@pytest.mark.asyncio
async def test_missing_cli_is_unavailable(tmp_path, monkeypatch):
    _, _, gateway = await make_boundary(tmp_path, tmp_path / "missing-cli", monkeypatch)
    assert await gateway.reconcile_pstack_terminal_tabs() == {
        "status":"deferred", "reason":"cli_not_found"}


@pytest.mark.asyncio
async def test_cli_timeout_is_unavailable_and_process_reaped(tmp_path, monkeypatch):
    cli = tmp_path / "hung-cli"
    pid_file = tmp_path / "pid"
    cli.write_text("#!/usr/bin/env python3\nimport os, time\n" +
                   f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n" +
                   "time.sleep(30)\n")
    cli.chmod(0o755)
    _, herdr, _ = await make_boundary(tmp_path, cli, monkeypatch)
    with pytest.raises(HerdrUnavailable, match="timeout"):
        await herdr._run_cli_json("tab", "list", timeout_seconds=1)
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)
