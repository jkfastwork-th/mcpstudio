import subprocess
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
        "managed_session_allow_git_worktree_siblings": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def session(tmp_path, **metadata):
    return {"id": "ms-a", "project_path": str(tmp_path), "metadata": metadata}


def git_repo_with_worktree(tmp_path):
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


def test_classifies_serena_read_write_and_destructive_tools():
    assert classify_tool("read_file", {"relative_path": "x.py"}) == "read"
    assert classify_tool("replace_content", {}) == "write"
    assert classify_tool("replace_in_files", {"dry_run": True}) == "read"
    assert classify_tool("replace_in_files", {"dry_run": False}) == "write"
    assert classify_tool("safe_delete_symbol", {}) == "destructive"
    assert classify_tool("activate_project", {"project": "alpha"}) == "execute"
    assert classify_tool("mcpstudio_certify_language_runtime_recovery", {}) == "execute"
    assert classify_tool("herdr_prompt_agent", {"agent_id": "w1:pA"}) == "execute"
    assert classify_tool("herdr_wait_agent", {"agent_id": "w1:pA"}) == "read"


def test_hirda_skill_run_is_classified_as_execute():
    assert classify_tool("hirda_skill_run") == "execute"


def test_shell_classifier_keeps_read_write_execute_separate():
    assert classify_shell_command("git status && git diff --stat") == "read"
    assert classify_shell_command("git branch --show-current") == "read"
    assert classify_shell_command("git status --short && git branch --show-current") == "read"
    assert classify_shell_command("git branch feature/x") != "read"
    assert classify_shell_command("pytest -q tests") == "execute"
    assert classify_shell_command("cmd /c echo HIRDA_NATIVE_JKFASTDEV") == "read"
    assert classify_tool(
        "hirda__hirda_native_computer__start_process",
        {"command": "cmd /c echo HIRDA_NATIVE_JKFASTDEV"},
        declared_category="execute",
    ) == "execute"
    assert classify_tool(
        "hirda__hirda_native_computer__start_process",
        {"command": "cmd /c rmdir /s /q build"},
        declared_category="execute",
    ) == "destructive"
    assert classify_shell_command("uv run pytest -q tests") == "execute"
    assert classify_shell_command("uvx openbrowser-ai==0.1.54 --help") == "execute"
    assert classify_shell_command("corepack pnpm vitest run test/risk.test.ts") == "execute"
    assert classify_shell_command("corepack pnpm typecheck") == "execute"
    assert classify_shell_command("corepack pnpm install --frozen-lockfile") == "write"
    assert classify_shell_command("corepack enable") == "write"
    assert classify_shell_command("corepack mystery") == "unknown"
    assert classify_shell_command("python3 -m unittest -v tests.test_tool_permissions") == "execute"
    assert classify_shell_command("./.venv/bin/python -m py_compile mcp_studio/gateway.py") == "execute"
    assert classify_shell_command("./.venv/bin/python scripts/certify-live-session-handoff.py --help") == "execute"
    assert classify_shell_command("git add x.py && git commit -m test") == "write"
    assert classify_shell_command("git worktree list") == "read"
    assert classify_shell_command("git worktree add ../mcp-studio-permission-classifier HEAD") == "write"
    assert classify_shell_command("git worktree move ../old ../new") == "write"
    assert classify_shell_command("git worktree remove ../old") == "destructive"
    assert classify_shell_command("git worktree prune") == "destructive"
    assert classify_shell_command("git submodule") == "read"
    assert classify_shell_command("git submodule status") == "read"
    assert classify_shell_command("git submodule summary") == "read"
    assert classify_shell_command("git submodule update --init third_party/pstack-claude") == "write"
    assert classify_shell_command("git submodule sync") == "write"
    assert classify_shell_command("git submodule deinit third_party/pstack-claude") == "destructive"
    assert classify_shell_command("git submodule foreach 'echo $name'") == "unknown"
    assert classify_shell_command(
        "MCP_STUDIO_CONFIG=/home/alfred/mcp-studio/config.yaml "
        "/home/alfred/mcp-studio/.venv/bin/python -m pytest -q tests/test_tool_permissions.py"
    ) == "execute"
    assert classify_shell_command(
        "cd . && git submodule update --init third_party/pstack-claude && "
        "pytest -q tests/test_tool_permissions.py"
    ) == "write"
    assert classify_tool(
        "execute_shell_command",
        {"command": "git worktree add ../mcp-studio-permission-classifier HEAD"},
    ) == "write"
    assert classify_tool(
        "execute_shell_command",
        {"command": "git submodule update --init third_party/pstack-claude"},
    ) == "write"
    assert classify_shell_command("git push origin review/remove-hca") == "execute"
    assert classify_shell_command("git reset --hard HEAD") == "destructive"
    assert classify_shell_command("python -c 'open(\"x\",\"w\").write(\"x\")'") == "unknown"
    assert classify_shell_command("grep -E 'foo|bar' README.md") == "read"
    assert classify_shell_command("find . -type f | sort | sed -n '1,20p'") == "read"
    assert classify_shell_command("mcporter list") == "execute"
    assert classify_shell_command("herdr agent list") == "execute"
    assert classify_shell_command("curl -fsS http://127.0.0.1:8100/api/cognition/status") == "execute"
    assert classify_shell_command("systemctl status mcp-studio.service") == "read"
    assert classify_shell_command("systemctl --user status mcp-studio.service") == "read"
    assert classify_shell_command("systemctl --user restart mcp-studio.service") == "execute"
    assert classify_shell_command("systemctl enable mcp-studio.service") == "execute"
    assert classify_shell_command("sudo systemctl daemon-reload") == "execute"
    assert classify_shell_command("systemctl reenable mcp-studio.service") == "execute"
    assert classify_shell_command("systemctl unmask mcp-studio.service") == "execute"
    assert classify_shell_command("tailscale status") == "read"
    assert classify_shell_command("tailscale ip -4") == "read"
    assert classify_shell_command("tailscale ping 100.85.206.7") == "read"
    assert classify_shell_command("tailscale netcheck") == "read"
    assert classify_shell_command("tailscale up") == "unknown"
    assert classify_shell_command("systemctl --user stop mcp-studio.service") == "destructive"
    assert classify_shell_command("systemctl --host remote restart mcp-studio.service") == "unknown"
    assert classify_shell_command("sudo systemctl restart mcp-studio.service") == "execute"
    assert classify_shell_command("sudo systemctl stop mcp-studio.service") == "destructive"
    # Shell control-flow stays fail-closed: a loop can hide arbitrary commands and must not
    # inherit a safe class merely because its visible body appears read-only.
    assert classify_shell_command("for f in a b; do cat $f; done") == "unknown"
    assert classify_shell_command("systemctl is-active mcp-studio.service") == "read"
    assert classify_shell_command("systemctl restart mcp-studio.service") == "execute"
    assert classify_shell_command("systemctl stop mcp-studio.service") == "destructive"
    assert classify_shell_command("journalctl -u mcp-studio.service -n 20 --no-pager") == "read"
    assert classify_shell_command("journalctl --disk-usage") == "read"
    assert classify_shell_command("df -h .") == "read"
    assert classify_shell_command("du -sh .") == "read"
    assert classify_shell_command("ps -eo pid,ppid,stat,%cpu,%mem,etime,cmd --sort=-%cpu") == "read"
    assert classify_shell_command("uptime") == "read"
    assert classify_shell_command("df -h . && du -sh . && ps -ef && uptime && journalctl --disk-usage") == "read"
    assert classify_tool("hirda__hirda_native_computer__start_process", {"command": "df -h ."}, declared_category="execute") == "execute"
    assert classify_shell_command("ss -ltnp") == "read"


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


def test_workspace_scope_blocks_windows_style_relative_escape(tmp_path):
    decision = decide_tool_call(
        studio(),
        session(tmp_path),
        "read_file",
        {"relative_path": "..\\\\..\\\\outside.txt"},
    )
    assert decision.allowed is False
    assert decision.code == "TOOL_SCOPE_VIOLATION"


def test_workspace_scope_blocks_windows_drive_path(tmp_path):
    decision = decide_tool_call(
        studio(),
        session(tmp_path),
        "read_file",
        {"relative_path": "C:\\\\outside\\\\secret.txt"},
    )
    assert decision.allowed is False
    assert decision.code == "TOOL_SCOPE_VIOLATION"


def test_shell_scope_blocks_absolute_operand_outside_workspace(tmp_path):
    decision = decide_tool_call(studio(), session(tmp_path), "execute_shell_command", {"command": "cat /etc/passwd"})
    assert decision.allowed is False
    assert decision.code == "TOOL_SCOPE_VIOLATION"


def test_python_script_outside_workspace_is_still_scope_blocked(tmp_path):
    decision = decide_tool_call(
        studio(),
        session(tmp_path),
        "execute_shell_command",
        {"command": "python3 ../outside.py"},
    )
    assert decision.allowed is False
    assert decision.category == "execute"
    assert decision.code == "TOOL_SCOPE_VIOLATION"


def test_registered_git_worktree_sibling_is_allowed_when_opted_in(tmp_path):
    repo, sibling = git_repo_with_worktree(tmp_path)
    enabled = studio(managed_session_allow_git_worktree_siblings=True)

    cwd_decision = decide_tool_call(
        enabled,
        session(repo),
        "execute_shell_command",
        {"command": "git status", "cwd": str(sibling)},
    )
    path_decision = decide_tool_call(
        enabled,
        session(repo),
        "read_file",
        {"relative_path": str(sibling / "README.md")},
    )

    assert cwd_decision.allowed is True
    assert path_decision.allowed is True


def test_registered_git_worktree_sibling_stays_blocked_without_opt_in(tmp_path):
    repo, sibling = git_repo_with_worktree(tmp_path)
    decision = decide_tool_call(
        studio(),
        session(repo),
        "execute_shell_command",
        {"command": "git status", "cwd": str(sibling)},
    )
    assert decision.allowed is False
    assert decision.code == "TOOL_SCOPE_VIOLATION"


def test_unregistered_sibling_stays_blocked_with_worktree_opt_in(tmp_path):
    repo, _ = git_repo_with_worktree(tmp_path)
    fake = tmp_path / "repo-copy"
    fake.mkdir()
    decision = decide_tool_call(
        studio(managed_session_allow_git_worktree_siblings=True),
        session(repo),
        "execute_shell_command",
        {"command": "git status", "cwd": str(fake)},
    )
    assert decision.allowed is False
    assert decision.code == "TOOL_SCOPE_VIOLATION"


def test_unknown_tool_fails_closed(tmp_path):
    decision = decide_tool_call(studio(), session(tmp_path), "future_mutator", {})
    assert decision.allowed is False
    assert decision.code == "TOOL_PERMISSION_UNCLASSIFIED"


def test_backend_declared_permission_classifies_unknown_tool(tmp_path):
    decision = decide_tool_call(
        studio(),
        session(tmp_path),
        "hirda__desktop_commander__list_directory",
        {"path": "."},
        declared_category="read",
    )
    assert decision.allowed is True
    assert decision.category == "read"


def test_native_windows_cmd_switch_is_not_treated_as_workspace_path(tmp_path):
    decision = decide_tool_call(
        studio(),
        session(tmp_path),
        "hirda__hirda_native_computer__start_process",
        {"command": "cmd /c echo HIRDA_NATIVE_JKFASTDEV"},
        declared_category="execute",
    )
    assert decision.allowed is True
    assert decision.category == "execute"


def test_backend_start_process_uses_command_risk_not_declared_execute(tmp_path):
    decision = decide_tool_call(
        studio(),
        session(tmp_path),
        "hirda__desktop_commander__start_process",
        {"command": "rm -rf ./build"},
        declared_category="execute",
    )
    assert decision.allowed is False
    assert decision.category == "destructive"
    assert decision.code == "TOOL_PERMISSION_DENIED"


def test_backend_paths_list_and_urls_are_workspace_scoped(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    paths = decide_tool_call(
        studio(),
        session(root),
        "hirda__desktop_commander__read_multiple_files",
        {"paths": ["inside.txt", str(outside)]},
        declared_category="read",
    )
    assert paths.allowed is False
    assert paths.code == "TOOL_SCOPE_VIOLATION"

    url = decide_tool_call(
        studio(),
        session(root),
        "hirda__desktop_commander__read_file",
        {"path": "https://example.test/file"},
        declared_category="read",
    )
    assert url.allowed is False
    assert url.code == "TOOL_SCOPE_VIOLATION"
