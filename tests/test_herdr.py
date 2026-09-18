import json

from mcp_studio.herdr import _count_items, _decode_text_content


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
