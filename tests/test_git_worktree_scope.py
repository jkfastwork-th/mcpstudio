from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from mcp_studio.git_worktrees import is_registered_git_worktree_path
from mcp_studio.tool_permissions import classify_tool, decide_tool_call


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _repo_with_sibling_worktree(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    (repo / "README.md").write_text("test\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    subprocess.run(
        [
            "git", "-C", str(repo),
            "-c", "user.name=HIRDA Test",
            "-c", "user.email=hirda-test@example.invalid",
            "commit", "-m", "init",
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    sibling = tmp_path / "repo-ux-ui"
    _git(repo, "worktree", "add", "-b", "ux-ui-test", str(sibling))
    return repo.resolve(), sibling.resolve()


def _studio(*, allow_siblings: bool) -> SimpleNamespace:
    return SimpleNamespace(
        managed_session_default_read_allowed=True,
        managed_session_default_write_allowed=True,
        managed_session_default_execute_allowed=True,
        managed_session_default_destructive_allowed=False,
        managed_session_tool_scope_enforced=True,
        managed_session_tool_permissions_fail_closed=True,
        managed_session_allow_git_worktree_siblings=allow_siblings,
    )


def _session(repo: Path) -> dict:
    return {"id": "ms-a", "project_path": str(repo), "metadata": {}}


def test_registered_sibling_git_worktree_is_in_family_scope(tmp_path: Path):
    repo, sibling = _repo_with_sibling_worktree(tmp_path)
    assert is_registered_git_worktree_path(repo, sibling)
    assert is_registered_git_worktree_path(repo, sibling / "static" / "styles.css")


def test_unregistered_sibling_is_not_in_family_scope(tmp_path: Path):
    repo, _ = _repo_with_sibling_worktree(tmp_path)
    fake = tmp_path / "repo-copy"
    fake.mkdir()
    assert not is_registered_git_worktree_path(repo, fake)


def test_symlink_escape_from_registered_worktree_is_rejected(tmp_path: Path):
    repo, sibling = _repo_with_sibling_worktree(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    escape = sibling / "escape"
    escape.symlink_to(outside, target_is_directory=True)
    assert not is_registered_git_worktree_path(repo, escape / "secret.txt")


def test_tool_scope_allows_registered_sibling_only_when_opted_in(tmp_path: Path):
    repo, sibling = _repo_with_sibling_worktree(tmp_path)
    args = {"relative_path": str(sibling / "static" / "styles.css")}

    denied = decide_tool_call(_studio(allow_siblings=False), _session(repo), "replace_content", args)
    allowed = decide_tool_call(_studio(allow_siblings=True), _session(repo), "replace_content", args)

    assert denied.allowed is False
    assert denied.code == "TOOL_SCOPE_VIOLATION"
    assert allowed.allowed is True


def test_shell_cwd_allows_registered_sibling_but_not_random_sibling(tmp_path: Path):
    repo, sibling = _repo_with_sibling_worktree(tmp_path)
    fake = tmp_path / "repo-copy"
    fake.mkdir()
    studio = _studio(allow_siblings=True)

    allowed = decide_tool_call(
        studio,
        _session(repo),
        "execute_shell_command",
        {"cwd": str(sibling), "command": "git status"},
    )
    denied = decide_tool_call(
        studio,
        _session(repo),
        "execute_shell_command",
        {"cwd": str(fake), "command": "git status"},
    )

    assert allowed.allowed is True
    assert denied.allowed is False
    assert denied.code == "TOOL_SCOPE_VIOLATION"


def test_current_exposed_control_tools_have_explicit_classification():
    expected = {
        "fern_bridge_status": "read",
        "fern_inbox_list_or_peek": "read",
        "fern_inbox_claim": "execute",
        "fern_reply_submit": "execute",
        "fern_interjection_decide": "execute",
        "herdr_list_agents": "read",
        "herdr_list_panes": "read",
        "herdr_get_agent": "read",
        "herdr_read_agent": "read",
        "herdr_wait_agent": "read",
        "herdr_prompt_agent": "execute",
        "mcpstudio_current_session": "read",
        "mcpstudio_get_session": "read",
        "mcpstudio_list_sessions": "read",
        "mcpstudio_list_workspaces": "read",
        "mcpstudio_session_history": "read",
        "mcpstudio_lane_status": "read",
        "mcpstudio_list_capsules": "read",
        "mcpstudio_get_capsule": "read",
        "mcpstudio_set_capsule_contract": "write",
        "mcpstudio_set_lane_state": "execute",
        "mcpstudio_approve_capsule_handoff": "execute",
        "mcpstudio_register_workspace": "write",
        "mcpstudio_rename_session": "write",
        "mcpstudio_use_workspace": "execute",
        "mcpstudio_create_session": "execute",
        "mcpstudio_use_session": "execute",
        "mcpstudio_close_session": "execute",
        "mcpstudio_detach_session": "execute",
        "get_diagnostics_for_file": "read",
    }
    assert {name: classify_tool(name, {}) for name in expected} == expected
