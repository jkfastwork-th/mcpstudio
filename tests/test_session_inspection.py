from __future__ import annotations

from types import SimpleNamespace

import pytest

from mcp_studio.session_inspection import inspect_session_bindings, inspect_session_from_db


def managed():
    return {"id": "ms-owned", "workspace_key": "approved-ws", "metadata": {}}


def gateway():
    return [{"managed_session_id": "ms-owned", "studio_session_id": "ss-owned", "status": "connected"}]


def worker(**overrides):
    row = {
        "id": "worker-1",
        "owner_session_id": "ss-owned",
        "workspace": "approved-ws",
        "pane": "wH:p1",
        "agent": "codex",
        "state": "busy",
    }
    return {**row, **overrides}


def snapshot(status="healthy", panes=None):
    return {
        "status": status,
        "last_refreshed_at": "2026-10-12T04:00:00Z",
        "panes": panes if panes is not None else [{"pane_id": "wH:p1", "agent_status": "busy"}],
        "agents": [{"pane_id": "wH:p1", "agent": "codex"}],
    }


def inspect(**kwargs):
    defaults = dict(
        gateways=gateway(), workers=[worker()],
        leases=[{"worker_id": "worker-1", "workspace": "approved-ws"}],
        herdr_snapshot=snapshot(),
    )
    return inspect_session_bindings(managed(), **{**defaults, **kwargs})


def test_exact_owner_worker_and_write_lease_are_observed():
    result = inspect()
    assert result["schema"] == "hirda-session-inspection-v1"
    assert result["authority"] == "read_only_observation"
    assert result["binding_count"] == 1
    binding = result["bindings"][0]
    assert binding["worker_id"] == "worker-1"
    assert binding["pane_id"] == "wH:p1"
    assert binding["agent"] == "codex"
    assert binding["write_lease_active"] is True
    assert binding["observation"]["state"] == "observed"


def test_work_from_another_logical_owner_is_not_exposed():
    result = inspect(workers=[worker(owner_session_id="ss-foreign")])
    assert result["bindings"] == []
    assert result["binding_count"] == 0


def test_matching_logical_owner_cannot_leak_another_workspace():
    result = inspect(workers=[worker(workspace="other-workspace")])
    assert result["bindings"] == []


def test_historical_gateway_for_other_managed_session_does_not_match():
    result = inspect(gateways=[{"managed_session_id": "ms-other", "studio_session_id": "ss-owned"}])
    assert result["bindings"] == []


def test_unrelated_lease_does_not_grant_write_authority():
    result = inspect(leases=[{"worker_id": "worker-2", "workspace": "approved-ws"}])
    assert result["bindings"][0]["write_lease_active"] is False
    assert result["authority"] == "read_only_observation"


def test_stale_snapshot_is_unverified_even_with_matching_pane():
    result = inspect(herdr_snapshot=snapshot("down"))
    assert result["bindings"][0]["observation"]["state"] == "unverified"


def test_missing_pane_in_healthy_snapshot_is_not_observed_not_assumed_connected():
    result = inspect(herdr_snapshot=snapshot(panes=[{"pane_id": "wH:pOther"}]))
    assert result["bindings"][0]["observation"]["state"] == "not_observed"


def test_no_worker_or_task_yields_empty_no_guessed_binding():
    result = inspect(workers=[], gateways=[], leases=[], herdr_snapshot=None)
    assert result["bindings"] == []
    assert result["herdr_snapshot_status"] == "unknown"


def test_explicit_pstack_tab_is_observed_with_exact_id_only():
    session = managed()
    session["metadata"]["active_pstack_task"] = {
        "task_id": "TASK-A",
        "managed_session_id": "ms-owned",
        "workspace_key": "approved-ws",
        "status": "running",
        "herdr_tab": {
            "status": "open",
            "workspace_id": "wH",
            "tab_id": "wH:t1",
            "root_pane_id": "wH:p1",
        },
    }
    result = inspect_session_bindings(
        session, gateways=[], workers=[], leases=[],
        herdr_snapshot=snapshot(panes=[{
            "pane_id": "wH:p1", "workspace_id": "wH", "tab_id": "wH:t1"
        }])
    )
    assert result["binding_count"] == 1
    item = result["bindings"][0]
    assert item["source"] == "pstack_task"
    assert (item["herdr_workspace_id"], item["tab_id"], item["pane_id"]) == (
        "wH", "wH:t1", "wH:p1"
    )
    assert item["observation"]["state"] == "observed"


def test_pstack_pane_reused_under_another_tab_is_unverified():
    session = managed()
    session["metadata"]["active_pstack_task"] = {
        "managed_session_id": "ms-owned", "workspace_key": "approved-ws", "status": "running",
        "herdr_tab": {
            "status": "open", "workspace_id": "wH",
            "tab_id": "wH:t1", "root_pane_id": "wH:p1",
        },
    }
    result = inspect_session_bindings(
        session, gateways=[], workers=[], leases=[],
        herdr_snapshot=snapshot(panes=[{
            "pane_id": "wH:p1", "workspace_id": "wH", "tab_id": "wH:t-other"
        }])
    )
    assert result["bindings"][0]["observation"]["state"] == "unverified"


def test_closed_pstack_tab_is_historical_even_if_pane_is_seen():
    session = managed()
    session["metadata"]["active_pstack_task"] = {
        "managed_session_id": "ms-owned", "workspace_key": "approved-ws", "status": "completed",
        "herdr_tab": {
            "status": "closed", "workspace_id": "wH",
            "tab_id": "wH:t1", "root_pane_id": "wH:p1",
        },
    }
    result = inspect_session_bindings(
        session, gateways=[], workers=[], leases=[],
        herdr_snapshot=snapshot(panes=[{
            "pane_id": "wH:p1", "workspace_id": "wH", "tab_id": "wH:t1"
        }])
    )
    assert result["bindings"][0]["observation"]["state"] == "historical"


def test_other_managed_session_task_does_not_leak_tab():
    session = managed()
    session["metadata"]["active_pstack_task"] = {
        "task_id": "TASK-FOREIGN",
        "managed_session_id": "ms-other",
        "herdr_tab": {"status": "open", "tab_id": "wH:t2", "root_pane_id": "wH:p1"},
    }
    result = inspect_session_bindings(
        session, gateways=[], workers=[], leases=[], herdr_snapshot=snapshot()
    )
    assert not result["bindings"]


@pytest.mark.asyncio
async def test_db_layer_filters_historical_gateway_rebound_to_other_session():
    class FakeDb:
        async def list_gateway_sessions_for_managed_session(self, _):
            return gateway()
        async def get_session(self, _):
            return {"id": "ss-owned", "managed_session_id": "ms-new"}
        async def list_workers(self):
            return [worker()]
        async def list_workspace_leases(self):
            return [{"worker_id": "worker-1", "workspace": "approved-ws"}]
    result = await inspect_session_from_db(FakeDb(), managed(), snapshot())
    assert result["bindings"] == []


@pytest.mark.asyncio
async def test_db_layer_retains_valid_durable_logical_pin():
    class FakeDb:
        async def list_gateway_sessions_for_managed_session(self, _):
            return gateway()
        async def get_session(self, _):
            return {"id": "ss-owned", "managed_session_id": "ms-owned"}
        async def list_workers(self):
            return [worker()]
        async def list_workspace_leases(self):
            return []
    result = await inspect_session_from_db(FakeDb(), managed(), snapshot())
    assert result["binding_count"] == 1
    assert result["bindings"][0]["write_lease_active"] is False
