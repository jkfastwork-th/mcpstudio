from __future__ import annotations

import json

from mcp_studio.herdr import _read_agent_session_result


def test_read_agent_session_result_extracts_matching_assistant_marker(tmp_path):
    session_id = "d8822ff0-92aa-4374-acba-3dde3b9decf2"
    request_id = "COG-TRANSCRIPT-OK"
    transcript_dir = tmp_path / ".claude" / "projects" / "-home-alfred-mcp-studio"
    transcript_dir.mkdir(parents=True)
    transcript = transcript_dir / f"{session_id}.jsonl"
    rows = [
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "older unrelated answer"}],
            },
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"<<<HIRDA_COGNITIVE_RESULT:{request_id}>>>\n"
                            "TRANSCRIPT_OK\n"
                            "<<<END_HIRDA_COGNITIVE_RESULT>>>"
                        ),
                    }
                ],
            },
        },
    ]
    transcript.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    pane = {
        "cwd": "/home/alfred/mcp-studio",
        "agent_session": {
            "source": "herdr:claude",
            "value": session_id,
        },
    }

    result = _read_agent_session_result(
        pane,
        request_id,
        home=tmp_path,
    )

    assert result is not None
    assert "TRANSCRIPT_OK" in result


def test_read_agent_session_result_ignores_unrecognized_session_source(tmp_path):
    result = _read_agent_session_result(
        {
            "cwd": "/home/alfred/mcp-studio",
            "agent_session": {
                "source": "herdr:other",
                "value": "session-1",
            },
        },
        "COG-NOPE",
        home=tmp_path,
    )

    assert result is None


def test_read_agent_session_result_extracts_codex_assistant_marker_without_session_id(
    tmp_path,
):
    request_id = "COG-CODEX-TRANSCRIPT-OK"
    transcript_dir = (
        tmp_path
        / ".codex"
        / "sessions"
        / "2026"
        / "09"
        / "27"
    )
    transcript_dir.mkdir(parents=True)
    transcript = transcript_dir / "rollout-test.jsonl"
    rows = [
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": (
                            f"<<<HIRDA_COGNITIVE_RESULT:{request_id}>>>\n"
                            "USER_ECHO\n"
                            "<<<END_HIRDA_COGNITIVE_RESULT>>>"
                        ),
                    }
                ],
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "item": {
                    "type": "AgentMessage",
                    "content": [
                        {
                            "type": "Text",
                            "text": (
                                f"<<<HIRDA_COGNITIVE_RESULT:{request_id}>>>\n"
                                "CODEX_TRANSCRIPT_OK\n"
                                "<<<END_HIRDA_COGNITIVE_RESULT>>>"
                            ),
                        }
                    ],
                },
            },
        },
    ]
    transcript.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )

    result = _read_agent_session_result(
        {"agent": "codex", "cwd": "/home/alfred/mcp-studio"},
        request_id,
        home=tmp_path,
    )

    assert result is not None
    assert "CODEX_TRANSCRIPT_OK" in result
    assert "USER_ECHO" not in result


def test_read_agent_session_result_extracts_codex_response_item(tmp_path):
    request_id = "COG-CODEX-RESPONSE-ITEM"
    transcript_dir = (
        tmp_path
        / ".codex"
        / "sessions"
        / "2026"
        / "09"
        / "27"
    )
    transcript_dir.mkdir(parents=True)
    transcript = transcript_dir / "rollout-response.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": (
                                f"<<<HIRDA_COGNITIVE_RESULT:{request_id}>>>\n"
                                "CODEX_RESPONSE_OK\n"
                                "<<<END_HIRDA_COGNITIVE_RESULT>>>"
                            ),
                        }
                    ],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = _read_agent_session_result(
        {"agent": "codex"},
        request_id,
        home=tmp_path,
    )

    assert result is not None
    assert "CODEX_RESPONSE_OK" in result
