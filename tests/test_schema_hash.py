from mcp_studio.mcp_client import canonical_tools_hash


def test_tool_order_does_not_change_hash():
    a = [
        {"name": "b", "description": "B", "inputSchema": {"type": "object"}},
        {"name": "a", "description": "A", "inputSchema": {"type": "object"}},
    ]
    b = list(reversed(a))
    assert canonical_tools_hash(a) == canonical_tools_hash(b)


def test_schema_change_changes_hash():
    a = [{"name": "a", "inputSchema": {"type": "object", "properties": {}}}]
    b = [{"name": "a", "inputSchema": {"type": "object", "properties": {"x": {"type": "string"}}}}]
    assert canonical_tools_hash(a) != canonical_tools_hash(b)
