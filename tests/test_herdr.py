import json

import pytest

from mcp_studio.herdr import HerdrManager, _count_items, _decode_text_content


def test_decode_json_text_content_and_count():
    raw = {"content": [{"type": "text", "text": '{"agents":[{"id":"a"},{"id":"b"}]}'}]}
    decoded = _decode_text_content(raw)
    assert _count_items(decoded, ("agents",)) == 2


def test_decode_serena_herdr_nested_result_string():
    inner = {
        "id": "cli:agent:list",
        "result": {
            "agents": [
                {"agent": "claude", "pane_id": "wF:p1"},
                {"agent": "hermes", "pane_id": "w1:p7"},
                {"agent": "claude", "pane_id": "wF:p4"},
                {"agent": "claude", "pane_id": "w1:p1"},
            ],
            "type": "agent_list",
        },
    }
    raw = {"result": json.dumps(inner)}
    decoded = _decode_text_content(raw)
    assert isinstance(decoded, dict)
    assert _count_items(decoded, ("agents",)) == 4


def test_decode_nested_panes_count():
    inner = {
        "id": "cli:pane:list",
        "result": {
            "panes": [{"pane_id": f"p{i}"} for i in range(6)],
            "type": "pane_list",
        },
    }
    raw = {"result": json.dumps(inner)}
    decoded = _decode_text_content(raw)
    assert _count_items(decoded, ("panes",)) == 6


@pytest.mark.asyncio
async def test_start_agent_in_pane_forwards_runtime_agent_args():
    manager = object.__new__(HerdrManager)
    calls = []
    pane = {
        "pane_id": "wH:p9",
        "agent": "claude",
        "agent_status": "idle",
        "agent_session": {"value": "session-1"},
    }

    async def run_cli(*args, timeout_seconds=15.0):
        calls.append(args)
        return {"result": {"ok": True}}

    async def refresh():
        return {"status": "healthy"}

    manager._run_cli_json = run_cli
    manager.refresh = refresh
    manager.find_pane = lambda **kwargs: pane

    result = await manager.start_agent_in_pane(
        name="pstack-task-1",
        kind="claude",
        pane_id="wH:p9",
        agent_args=["--permission-mode", "acceptEdits"],
    )

    assert calls[0][-3:] == (
        "--",
        "--permission-mode",
        "acceptEdits",
    )
    assert result["agent_args"] == ["--permission-mode", "acceptEdits"]


def test_pane_list_enriches_agent_name_and_session_from_agent_inventory():
    snapshot = {
        "panes": {
            "panes": [
                {
                    "pane_id": "w1:p1",
                    "agent": "hermes",
                    "agent_status": "idle",
                }
            ]
        },
        "agents": {
            "agents": [
                {
                    "pane_id": "w1:p1",
                    "agent": "hermes",
                    "name": "hirda-fern-hermes",
                    "agent_session": {"value": "session-fern"},
                }
            ]
        },
    }

    panes = HerdrManager.pane_list(snapshot)

    assert panes == [
        {
            "pane_id": "w1:p1",
            "agent": "hermes",
            "agent_status": "idle",
            "agent_name": "hirda-fern-hermes",
            "agent_session": {"value": "session-fern"},
        }
    ]


@pytest.mark.asyncio
async def test_create_task_tab_uses_workspace_containing_fern_and_nova():
    manager = object.__new__(HerdrManager)
    calls = []

    async def run_cli(*args, timeout_seconds=15.0):
        calls.append(args)
        if args == ("tab", "list"):
            return {
                "result": {
                    "tabs": [
                        {"tab_id": "wH:t2", "workspace_id": "wH", "label": "Fern"},
                        {"tab_id": "wH:t3", "workspace_id": "wH", "label": "Nova"},
                        {"tab_id": "wF:t1", "workspace_id": "wF", "label": "Other"},
                    ]
                }
            }
        return {
            "result": {
                "tab": {
                    "tab_id": "wH:t4",
                    "workspace_id": "wH",
                    "label": "[PStack] TASK-123",
                },
                "root_pane": {"pane_id": "wH:p7"},
            }
        }

    manager._run_cli_json = run_cli
    tab = await manager.create_task_tab(
        {
            "task_id": "TASK-123",
            "trace_id": "TRACE-123",
            "workspace_key": "alpha",
            "goal": "Implement task scoped tabs",
        },
        cwd="/work/alpha",
    )

    assert tab["tab_id"] == "wH:t4"
    assert tab["workspace_id"] == "wH"
    assert tab["root_pane_id"] == "wH:p7"
    assert tab["persistent"] is False
    create_call = calls[1]
    assert create_call[:5] == (
        "tab",
        "create",
        "--workspace",
        "wH",
        "--label",
    )
    assert "--no-focus" in create_call
    assert "--cwd" in create_call
    assert "PSTACK_TASK_ID=TASK-123" in create_call
    assert "PSTACK_TRACE_ID=TRACE-123" in create_call
    assert "HIRDA_WORKSPACE_KEY=alpha" in create_call


@pytest.mark.asyncio
async def test_create_cognitive_tab_is_isolated_and_unfocused():
    manager = object.__new__(HerdrManager)
    calls = []

    async def run_cli(*args, timeout_seconds=15.0):
        calls.append(args)
        if args == ("tab", "list"):
            return {
                "result": {
                    "tabs": [
                        {"tab_id": "wH:t2", "workspace_id": "wH", "label": "Fern"},
                        {"tab_id": "wH:t3", "workspace_id": "wH", "label": "Nova"},
                    ]
                }
            }
        return {
            "result": {
                "tab": {
                    "tab_id": "wH:tC",
                    "workspace_id": "wH",
                    "label": "[Cognitive] fern:hermes · COG-123",
                },
                "root_pane": {"pane_id": "wH:pC"},
            }
        }

    manager._run_cli_json = run_cli
    tab = await manager.create_cognitive_tab(
        principal_id="fern",
        request_id="COG-123",
        runtime_id="hermes",
        cwd="/work/social",
    )

    assert tab["owner"] == "cognitive"
    assert tab["persistent"] is False
    assert tab["root_pane_id"] == "wH:pC"
    assert tab["principal_id"] == "fern"
    assert tab["runtime_id"] == "hermes"
    create_call = calls[1]
    assert create_call[:5] == (
        "tab",
        "create",
        "--workspace",
        "wH",
        "--label",
    )
    assert "--no-focus" in create_call
    assert "HIRDA_COGNITIVE_REQUEST_ID=COG-123" in create_call
    assert "HIRDA_PRINCIPAL_ID=fern" in create_call
    assert "HIRDA_RUNTIME_ID=hermes" in create_call


@pytest.mark.asyncio
async def test_close_cognitive_tab_only_closes_owned_cognitive_tab():
    manager = object.__new__(HerdrManager)
    calls = []

    async def run_cli(*args, timeout_seconds=15.0):
        calls.append(args)
        if args == ("tab", "list"):
            return {
                "result": {
                    "tabs": [
                        {
                            "tab_id": "wH:tC",
                            "workspace_id": "wH",
                            "label": "[Cognitive] fern:hermes · COG-123",
                        }
                    ]
                }
            }
        return {"result": {}}

    manager._run_cli_json = run_cli
    result = await manager.close_cognitive_tab(
        {
            "tab_id": "wH:tC",
            "label": "[Cognitive] fern:hermes · COG-123",
            "owner": "cognitive",
            "persistent": False,
        }
    )

    assert result["status"] == "closed"
    assert calls[-1] == ("tab", "close", "wH:tC")


@pytest.mark.asyncio
async def test_close_cognitive_tab_refuses_non_cognitive_owner():
    manager = object.__new__(HerdrManager)

    async def run_cli(*args, timeout_seconds=15.0):
        assert args == ("tab", "list")
        return {
            "result": {
                "tabs": [
                    {
                        "tab_id": "wH:tC",
                        "workspace_id": "wH",
                        "label": "[Cognitive] fern:hermes · COG-123",
                    }
                ]
            }
        }

    manager._run_cli_json = run_cli
    with pytest.raises(RuntimeError, match="non_cognitive_tab_close_refused"):
        await manager.close_cognitive_tab(
            {
                "tab_id": "wH:tC",
                "label": "[Cognitive] fern:hermes · COG-123",
                "owner": "pstack",
            }
        )


@pytest.mark.asyncio
async def test_close_task_tab_refuses_fern_and_nova():
    manager = object.__new__(HerdrManager)

    async def run_cli(*args, timeout_seconds=15.0):
        assert args == ("tab", "list")
        return {
            "result": {
                "tabs": [
                    {"tab_id": "wH:t2", "workspace_id": "wH", "label": "Fern"},
                    {"tab_id": "wH:t3", "workspace_id": "wH", "label": "Nova"},
                ]
            }
        }

    manager._run_cli_json = run_cli

    with pytest.raises(RuntimeError, match="persistent_tab_close_refused"):
        await manager.close_task_tab(
            {
                "tab_id": "wH:t2",
                "label": "not-trusted-metadata",
                "owner": "pstack",
                "status": "open",
            }
        )


@pytest.mark.asyncio
async def test_reconcile_disposable_task_tabs_closes_only_stale_pstack_tabs():
    manager = object.__new__(HerdrManager)
    closed = []

    async def run_cli(*args, timeout_seconds=15.0):
        if args == ("tab", "list"):
            return {
                "result": {"tabs": [
                    {"tab_id": "wH:t1", "label": "fern", "workspace_id": "wH"},
                    {"tab_id": "wH:t2", "label": "nova", "workspace_id": "wH"},
                    {"tab_id": "wH:t3", "label": "[PStack] TASK-A", "workspace_id": "wH"},
                    {"tab_id": "wH:t4", "label": "[PStack] TASK-OLD", "workspace_id": "wH"},
                    {"tab_id": "wH:t5", "label": "operator", "workspace_id": "wH"},
                ]}
            }
        if args[:2] == ("tab", "close"):
            closed.append(args[2])
            return {"ok": True}
        raise AssertionError(args)

    manager._run_cli_json = run_cli
    result = await manager.reconcile_disposable_task_tabs(
        keep_tab_ids={"wH:t3"}
    )

    assert closed == ["wH:t4"]
    assert result["closed"] == 1
    assert result["kept"] == 1
    assert result["protected"] == 2
