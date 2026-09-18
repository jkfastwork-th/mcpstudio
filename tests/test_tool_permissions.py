from types import SimpleNamespace

from mcp_studio.tool_permissions import classify_shell_command, classify_tool, decide_tool_call, effective_policy


def studio(**overrides):
    values = {
        "managed_session_default_read_allowed": True,
        "managed_session_default_write_allowed": True,
        "managed_session_default_execute_allowed": True,
        "managed_session_default_destructive_allowed": False,
        "managed_session_tool_scope_enforced": True,
        "managed_session_tool_permissions_fail_closed": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def session(tmp_path, **metadata):
    return {"id": "ms-a", "project_path": str(tmp_path), "metadata": metadata}


def test_classifies_serena_read_write_and_destructive_tools():
    assert classify_tool("read_file", {"relative_path": "x.py"}) == "read"
    assert classify_tool("replace_content", {}) == "write"
    assert classify_tool("replace_in_files", {"dry_run": True}) == "read"
    assert classify_tool("replace_in_files", {"dry_run": False}) == "write"
    assert classify_tool("safe_delete_symbol", {}) == "destructive"
    assert classify_tool("activate_project", {"project": "alpha"}) == "execute"


def test_shell_classifier_keeps_read_write_execute_separate():
    assert classify_shell_command("git status && git diff --stat") == "read"
    assert classify_shell_command("pytest -q tests") == "execute"
    assert classify_shell_command("git add x.py && git commit -m test") == "write"
    assert classify_shell_command("git reset --hard HEAD") == "destructive"
    assert classify_shell_command("python -c 'open(\"x\",\"w\").write(\"x\")'") == "unknown"
    assert classify_shell_command("grep -E 'foo|bar' README.md") == "read"


def test_default_policy_blocks_destructive(tmp_path):
    decision = decide_tool_call(studio(), session(tmp_path), "safe_delete_symbol", {"relative_path": "x.py"})
    assert decision.allowed is False
    assert decision.code == "TOOL_PERMISSION_DENIED"
    assert decision.category == "destructive"


def test_session_metadata_can_make_session_read_only(tmp_path):
    s = session(tmp_path, tool_permissions={"read": True, "write": False, "execute": False, "destructive": False})
    assert effective_policy(studio(), s)["write"] is False
    read = decide_tool_call(studio(), s, "read_file", {"relative_path": "x.py"})
    write = decide_tool_call(studio(), s, "replace_content", {"relative_path": "x.py"})
    assert read.allowed is True
    assert write.allowed is False
    assert write.code == "TOOL_PERMISSION_DENIED"


def test_workspace_scope_blocks_relative_escape(tmp_path):
    decision = decide_tool_call(studio(), session(tmp_path), "replace_content", {"relative_path": "../outside.txt"})
    assert decision.allowed is False
    assert decision.code == "TOOL_SCOPE_VIOLATION"


def test_shell_scope_blocks_absolute_operand_outside_workspace(tmp_path):
    decision = decide_tool_call(studio(), session(tmp_path), "execute_shell_command", {"command": "cat /etc/passwd"})
    assert decision.allowed is False
    assert decision.code == "TOOL_SCOPE_VIOLATION"


def test_unknown_tool_fails_closed(tmp_path):
    decision = decide_tool_call(studio(), session(tmp_path), "future_mutator", {})
    assert decision.allowed is False
    assert decision.code == "TOOL_PERMISSION_UNCLASSIFIED"
