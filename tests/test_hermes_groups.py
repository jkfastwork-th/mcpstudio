import asyncio
from types import SimpleNamespace

import pytest

from mcp_studio.hermes_groups import HermesGroupClient, HermesGroupError


def studio(**overrides):
    data = {
        "cognitive_hermes_group_ws_url": "ws://127.0.0.1:9119/api/ws",
        "cognitive_hermes_group_token_env": "HIRDA_HERMES_GROUP_TOKEN",
        "cognitive_hermes_group_token_file": "",
        "cognitive_hermes_group_room_id": "hirda-cognitive-team",
        "cognitive_hermes_group_room_name": "HIRDA Cognitive Team",
        "cognitive_hermes_team_profiles": {
            "coder": "coder",
            "tester": "tester",
            "reviewer": "reviewer",
        },
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def test_group_status_reports_configuration_without_exposing_token(monkeypatch):
    monkeypatch.setenv("HIRDA_HERMES_GROUP_TOKEN", "secret")
    client = HermesGroupClient(studio())
    status = client.status()
    assert status["configured"] is True
    assert status["token_available"] is True
    assert status["token_env"] == "HIRDA_HERMES_GROUP_TOKEN"
    assert status["lifecycle"]["scope_mode"] == "workspace"
    assert status["lifecycle"]["rotation_enabled"] is True
    assert status["lifecycle"]["max_events"] == 96
    assert status["lifecycle"]["max_age_seconds"] == 21600.0
    assert "secret" not in repr(status)


def test_group_ws_url_adds_token_query_without_overwriting_existing(monkeypatch):
    monkeypatch.setenv("HIRDA_HERMES_GROUP_TOKEN", "abc123")
    client = HermesGroupClient(
        studio(cognitive_hermes_group_ws_url="ws://127.0.0.1:9119/api/ws?x=1")
    )
    url = client._ws_url()
    assert "x=1" in url
    assert "token=abc123" in url


def test_group_token_file_is_supported_and_must_be_private(monkeypatch, tmp_path):
    monkeypatch.delenv("HIRDA_HERMES_GROUP_TOKEN", raising=False)
    token_file = tmp_path / "hermes-group.env"
    token_file.write_text(
        "HERMES_DASHBOARD_SESSION_TOKEN=file-secret\n",
        encoding="utf-8",
    )
    token_file.chmod(0o600)
    client = HermesGroupClient(
        studio(cognitive_hermes_group_token_file=str(token_file))
    )
    token, source = client._token()
    assert token == "file-secret"
    assert source == "file"
    status = client.status()
    assert status["token_available"] is True
    assert status["token_source"] == "file"
    assert "file-secret" not in repr(status)

    token_file.chmod(0o644)
    with pytest.raises(
        HermesGroupError,
        match="hermes_group_token_file_permissions",
    ):
        client._token()


@pytest.mark.asyncio
async def test_ensure_room_reuses_existing_room():
    client = HermesGroupClient(studio())
    calls = []

    async def fake_rpc(method, params=None, **kwargs):
        calls.append((method, params))
        if method == "groups.capabilities":
            return {"driver": True}
        if method == "groups.list":
            return {
                "rooms": [
                    {
                        "room_id": "hirda-cognitive-team",
                        "name": "HIRDA Cognitive Team",
                        "members": [
                            {"member_id": "coder", "profile": "coder", "handle": "coder"},
                            {"member_id": "tester", "profile": "tester", "handle": "tester"},
                            {"member_id": "reviewer", "profile": "reviewer", "handle": "reviewer"},
                        ],
                        "latest_seq": 1,
                        "created_at": 9999999999.0,
                    }
                ]
            }
        raise AssertionError(method)

    client.rpc = fake_rpc
    room = await client.ensure_room()
    assert room["room_id"] == "hirda-cognitive-team"
    assert [call[0] for call in calls] == [
        "groups.capabilities",
        "groups.list",
    ]


@pytest.mark.asyncio
async def test_ensure_room_creates_bot_group_when_missing():
    client = HermesGroupClient(studio())
    created = {}

    async def fake_rpc(method, params=None, **kwargs):
        if method == "groups.capabilities":
            return {"driver": True}
        if method == "groups.list":
            return {"rooms": []}
        if method == "groups.create":
            created.update(params or {})
            return {"room": dict(params or {})}
        raise AssertionError(method)

    client.rpc = fake_rpc
    room = await client.ensure_room()
    assert room["room_id"] == "hirda-cognitive-team"
    assert [item["profile"] for item in created["members"]] == [
        "coder",
        "tester",
        "reviewer",
    ]


def test_room_family_scopes_workspace_without_exposing_raw_scope():
    client = HermesGroupClient(studio())
    (
        room_a,
        _name_a,
        _members_a,
        family_a,
        digest_a,
    ) = client._room_spec("/home/alfred/project-a")
    (
        room_b,
        _name_b,
        _members_b,
        family_b,
        digest_b,
    ) = client._room_spec("/home/alfred/project-b")

    assert room_a == family_a
    assert room_b == family_b
    assert family_a != family_b
    assert digest_a != digest_b
    assert "/home/alfred" not in family_a
    assert "/home/alfred" not in family_b
    assert f"-s{digest_a}" in family_a


@pytest.mark.asyncio
async def test_ensure_room_rotates_when_event_budget_exceeded():
    cfg = studio(
        cognitive_hermes_group_rotation_enabled=True,
        cognitive_hermes_group_max_events=4,
        cognitive_hermes_group_max_age_seconds=0,
    )
    client = HermesGroupClient(cfg)
    members = [
        {"member_id": "coder", "profile": "coder", "handle": "coder"},
        {"member_id": "tester", "profile": "tester", "handle": "tester"},
        {"member_id": "reviewer", "profile": "reviewer", "handle": "reviewer"},
    ]
    calls: list[tuple[str, dict]] = []

    async def fake_rpc(method, params=None, **kwargs):
        params = dict(params or {})
        calls.append((method, params))
        if method == "groups.capabilities":
            return {"driver": True}
        if method == "groups.list":
            return {
                "rooms": [
                    {
                        "room_id": "hirda-cognitive-team",
                        "name": "HIRDA Cognitive Team",
                        "members": members,
                        "latest_seq": 4,
                        "created_at": 9999999999.0,
                    }
                ]
            }
        if method == "groups.state":
            return {
                "driver_status": {
                    "working": False,
                    "blocked": False,
                    "counts": {"queued": 0, "running": 0},
                }
            }
        if method == "groups.create":
            return {
                "room": {
                    "room_id": params["room_id"],
                    "name": params["name"],
                    "members": params["members"],
                    "latest_seq": 1,
                    "created_at": 9999999999.0,
                }
            }
        if method == "groups.disband":
            return {
                "tombstone": {
                    "room_id": params["room_id"],
                    "disbanded_at": 1.0,
                    "idempotent": False,
                }
            }
        raise AssertionError(method)

    client.rpc = fake_rpc
    room = await client.ensure_room()
    lifecycle = room["_hirda_lifecycle"]

    assert room["room_id"] == "hirda-cognitive-team-r0001"
    assert lifecycle["generation"] == 1
    assert lifecycle["rotated"] is True
    assert lifecycle["rotated_from"] == "hirda-cognitive-team"
    assert lifecycle["rotation_reasons"] == ["event_budget_exceeded"]
    assert lifecycle["fresh_session_boundary"] is True
    assert lifecycle["stale_rooms_cleaned"] == ["hirda-cognitive-team"]
    assert any(
        method == "groups.disband"
        and params["room_id"] == "hirda-cognitive-team"
        for method, params in calls
    )


@pytest.mark.asyncio
async def test_same_scope_missions_are_serialized():
    client = HermesGroupClient(studio())
    active = 0
    max_active = 0
    order: list[str] = []

    async def fake_locked(
        mission,
        *,
        request_id,
        timeout_seconds,
        scope_key,
    ):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        order.append(f"start:{request_id}")
        await asyncio.sleep(0.01)
        order.append(f"end:{request_id}")
        active -= 1
        return request_id

    client._run_mission_locked = fake_locked
    first, second = await asyncio.gather(
        client.run_mission(
            "A",
            request_id="REQ-A",
            timeout_seconds=1,
            scope_key="workspace-a",
        ),
        client.run_mission(
            "B",
            request_id="REQ-B",
            timeout_seconds=1,
            scope_key="workspace-a",
        ),
    )

    assert {first, second} == {"REQ-A", "REQ-B"}
    assert max_active == 1
    assert order in (
        ["start:REQ-A", "end:REQ-A", "start:REQ-B", "end:REQ-B"],
        ["start:REQ-B", "end:REQ-B", "start:REQ-A", "end:REQ-A"],
    )


@pytest.mark.asyncio
async def test_different_scope_missions_can_run_in_parallel():
    client = HermesGroupClient(studio())
    active = 0
    max_active = 0

    async def fake_locked(
        mission,
        *,
        request_id,
        timeout_seconds,
        scope_key,
    ):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.02)
        active -= 1
        return request_id

    client._run_mission_locked = fake_locked
    await asyncio.gather(
        client.run_mission(
            "A",
            request_id="REQ-A",
            timeout_seconds=1,
            scope_key="workspace-a",
        ),
        client.run_mission(
            "B",
            request_id="REQ-B",
            timeout_seconds=1,
            scope_key="workspace-b",
        ),
    )

    assert max_active == 2


@pytest.mark.asyncio
async def test_run_mission_requires_final_role_reports():
    client = HermesGroupClient(studio())

    async def ensure_room(scope_key=None):
        return {
            "room_id": "hirda-cognitive-team",
            "_hirda_lifecycle": {
                "scope_digest": "testscope",
                "generation": 0,
            },
        }

    client.ensure_room = ensure_room
    sends: list[dict] = []
    state_calls = 0

    async def fake_rpc(method, params=None, **kwargs):
        nonlocal state_calls
        params = params or {}
        if method == "groups.log" and params.get("limit") == 1:
            return {"latest_seq": 10}
        if method == "groups.send":
            sends.append(dict(params))
            return {"accepted": True}
        if method == "groups.log":
            if len(sends) == 1:
                return {
                    "latest_seq": 12,
                    "events": [
                        {
                            "kind": "turn.settled",
                            "payload": {"member_id": "coder", "passed": True},
                        },
                        {
                            "kind": "turn.settled",
                            "payload": {"member_id": "tester", "passed": True},
                        },
                        {
                            "kind": "turn.settled",
                            "payload": {"member_id": "reviewer", "passed": True},
                        },
                    ],
                }
            return {
                "latest_seq": 20,
                "events": [
                    {
                        "kind": "message.member",
                        "actor": {"id": "coder"},
                        "payload": {"text": "CODER_FINAL"},
                    },
                    {
                        "kind": "message.member",
                        "actor": {"id": "tester"},
                        "payload": {"text": "TESTER_FINAL"},
                    },
                    {
                        "kind": "message.member",
                        "actor": {"id": "reviewer"},
                        "payload": {"text": "REVIEWER_FINAL"},
                    },
                    {
                        "kind": "turn.settled",
                        "payload": {"member_id": "coder", "passed": False},
                    },
                    {
                        "kind": "turn.settled",
                        "payload": {"member_id": "tester", "passed": False},
                    },
                    {
                        "kind": "turn.settled",
                        "payload": {"member_id": "reviewer", "passed": False},
                    },
                ],
            }
        if method == "groups.state":
            state_calls += 1
            return {
                "driver_status": {
                    "working": False,
                    "counts": {"queued": 0, "running": 0},
                }
            }
        raise AssertionError(method)

    client.rpc = fake_rpc
    result = await client.run_mission(
        "MISSION",
        request_id="REQ-1",
        timeout_seconds=5,
    )

    assert len(sends) == 2
    assert sends[0]["payload"] == {
        "text": "MISSION",
        "thread_id": "hirda:REQ-1",
    }
    assert "HIRDA FINALIZATION ROUND" in sends[1]["payload"]["text"]
    assert result.member_outputs == {
        "coder": "CODER_FINAL",
        "tester": "TESTER_FINAL",
        "reviewer": "REVIEWER_FINAL",
    }
    assert result.output == (
        "[coder]\nCODER_FINAL\n\n"
        "[tester]\nTESTER_FINAL\n\n"
        "[reviewer]\nREVIEWER_FINAL"
    )
    assert result.recovery_rounds == 0
    assert result.latest_seq == 20
    assert state_calls == 2


@pytest.mark.asyncio
async def test_run_mission_retries_roles_that_pass_without_message():
    client = HermesGroupClient(studio())

    async def ensure_room(scope_key=None):
        return {
            "room_id": "hirda-cognitive-team",
            "_hirda_lifecycle": {
                "scope_digest": "testscope",
                "generation": 0,
            },
        }

    client.ensure_room = ensure_room
    sends: list[dict] = []

    async def fake_rpc(method, params=None, **kwargs):
        params = params or {}
        if method == "groups.log" and params.get("limit") == 1:
            return {"latest_seq": 30}
        if method == "groups.send":
            sends.append(dict(params))
            return {"accepted": True}
        if method == "groups.log":
            send_count = len(sends)
            if send_count == 1:
                return {
                    "latest_seq": 31,
                    "events": [
                        {
                            "kind": "turn.settled",
                            "payload": {"member_id": "coder", "passed": True},
                        },
                        {
                            "kind": "turn.settled",
                            "payload": {"member_id": "tester", "passed": True},
                        },
                        {
                            "kind": "turn.settled",
                            "payload": {"member_id": "reviewer", "passed": True},
                        },
                    ],
                }
            if send_count == 2:
                return {
                    "latest_seq": 35,
                    "events": [
                        {
                            "kind": "message.member",
                            "actor": {"id": "coder"},
                            "payload": {"text": "CODER_FINAL"},
                        },
                        {
                            "kind": "turn.settled",
                            "payload": {"member_id": "coder", "passed": False},
                        },
                        {
                            "kind": "turn.settled",
                            "payload": {"member_id": "tester", "passed": True},
                        },
                        {
                            "kind": "turn.settled",
                            "payload": {"member_id": "reviewer", "passed": True},
                        },
                    ],
                }
            if send_count == 3:
                return {
                    "latest_seq": 38,
                    "events": [
                        {
                            "kind": "message.member",
                            "actor": {"id": "tester"},
                            "payload": {"text": "TESTER_FINAL"},
                        },
                        {
                            "kind": "turn.settled",
                            "payload": {"member_id": "tester", "passed": False},
                        },
                    ],
                }
            if send_count == 4:
                return {
                    "latest_seq": 41,
                    "events": [
                        {
                            "kind": "message.member",
                            "actor": {"id": "reviewer"},
                            "payload": {"text": "REVIEWER_FINAL"},
                        },
                        {
                            "kind": "turn.settled",
                            "payload": {"member_id": "reviewer", "passed": False},
                        },
                    ],
                }
            raise AssertionError(send_count)
        if method == "groups.state":
            return {
                "driver_status": {
                    "working": False,
                    "counts": {"queued": 0, "running": 0},
                }
            }
        raise AssertionError(method)

    client.rpc = fake_rpc
    result = await client.run_mission(
        "MISSION",
        request_id="REQ-RETRY",
        timeout_seconds=5,
    )

    assert len(sends) == 4
    assert "@tester REQUIRED HIRDA ROLE COMPLETION" in sends[2]["payload"]["text"]
    assert "@reviewer REQUIRED HIRDA ROLE COMPLETION" in sends[3]["payload"]["text"]
    assert result.member_outputs == {
        "coder": "CODER_FINAL",
        "tester": "TESTER_FINAL",
        "reviewer": "REVIEWER_FINAL",
    }
    assert result.recovery_rounds == 2


@pytest.mark.asyncio
async def test_run_mission_timeout_stops_active_room():
    client = HermesGroupClient(studio())

    async def ensure_room(scope_key=None):
        return {
            "room_id": "hirda-cognitive-team",
            "_hirda_lifecycle": {
                "scope_digest": "testscope",
                "generation": 0,
            },
        }

    client.ensure_room = ensure_room
    calls = []

    async def fake_rpc(method, params=None, **kwargs):
        calls.append((method, dict(params or {})))
        if method == "groups.log" and (params or {}).get("limit") == 1:
            return {"latest_seq": 100}
        if method == "groups.send":
            return {
                "accepted": True,
                "event": {"event_id": "user:timeout-cert"},
            }
        if method == "groups.log":
            return {"latest_seq": 100, "events": []}
        if method == "groups.state":
            return {
                "driver_status": {
                    "working": True,
                    "counts": {"queued": 0, "running": 1},
                }
            }
        if method == "groups.stop":
            return {"cancelled": 1}
        raise AssertionError(method)

    client.rpc = fake_rpc
    with pytest.raises(
        HermesGroupError,
        match="hermes_group_mission_timeout",
    ):
        await client.run_mission(
            "MISSION",
            request_id="REQ-TIMEOUT",
            timeout_seconds=0.001,
        )

    stop_calls = [
        params
        for method, params in calls
        if method == "groups.stop"
    ]
    assert stop_calls == [
        {
            "room_id": "hirda-cognitive-team",
            "cancel_id": "hirda-timeout-REQ-TIMEOUT",
        }
    ]


@pytest.mark.asyncio
async def test_run_mission_stops_followup_chatter_after_required_roles_complete():
    client = HermesGroupClient(studio())

    async def ensure_room(scope_key=None):
        return {
            "room_id": "hirda-cognitive-team",
            "_hirda_lifecycle": {
                "scope_digest": "testscope",
                "generation": 0,
            },
        }

    client.ensure_room = ensure_room
    sends: list[dict] = []
    stop_calls: list[dict] = []
    stopped_for_round = False

    async def fake_rpc(method, params=None, **kwargs):
        nonlocal stopped_for_round
        params = dict(params or {})
        if method == "groups.log" and params.get("limit") == 1:
            return {"latest_seq": 200}
        if method == "groups.send":
            sends.append(params)
            stopped_for_round = False
            return {
                "accepted": True,
                "event": {"event_id": f"user:round-{len(sends)}"},
            }
        if method == "groups.log":
            final_round = len(sends) >= 2
            events = []
            if final_round:
                for role in ("coder", "tester", "reviewer"):
                    events.append(
                        {
                            "kind": "message.member",
                            "actor": {"id": role},
                            "payload": {"text": f"{role.upper()}_FINAL"},
                        }
                    )
            for role in ("coder", "tester", "reviewer"):
                events.append(
                    {
                        "kind": "turn.settled",
                        "payload": {
                            "member_id": role,
                            "passed": not final_round,
                            "discussion_event_id": f"user:round-{len(sends)}",
                        },
                    }
                )
            return {
                "latest_seq": 200 + len(sends),
                "events": events,
            }
        if method == "groups.state":
            return {
                "driver_status": {
                    "working": not stopped_for_round,
                    "counts": {
                        "queued": 0,
                        "running": 0 if stopped_for_round else 1,
                    },
                }
            }
        if method == "groups.stop":
            stop_calls.append(params)
            stopped_for_round = True
            return {"cancelled": 1}
        raise AssertionError(method)

    client.rpc = fake_rpc
    result = await client.run_mission(
        "MISSION",
        request_id="REQ-ROUND-STOP",
        timeout_seconds=5,
    )

    assert result.member_outputs == {
        "coder": "CODER_FINAL",
        "tester": "TESTER_FINAL",
        "reviewer": "REVIEWER_FINAL",
    }
    assert result.recovery_rounds == 0
    assert len(stop_calls) == 2
    assert all(call["room_id"] == "hirda-cognitive-team" for call in stop_calls)
    assert all(
        call["cancel_id"].startswith(
            "hirda-round-complete-REQ-ROUND-STOP-"
        )
        for call in stop_calls
    )


@pytest.mark.asyncio
async def test_ensure_room_fails_when_driver_is_unavailable():
    client = HermesGroupClient(studio())

    async def fake_rpc(method, params=None, **kwargs):
        assert method == "groups.capabilities"
        return {"driver": False}

    client.rpc = fake_rpc
    with pytest.raises(HermesGroupError, match="hermes_group_driver_unavailable"):
        await client.ensure_room()
