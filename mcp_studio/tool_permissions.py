from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .git_worktrees import is_registered_git_worktree_path


ToolClass = Literal["read", "write", "execute", "destructive", "unknown"]

_READ_TOOLS = {
    "initial_instructions", "get_current_config", "check_onboarding_performed",
    "list_dir", "find_file", "search_for_pattern", "get_symbols_overview",
    "find_symbol", "find_referencing_symbols", "find_implementations",
    "find_declaration", "get_diagnostics_for_file", "read_file", "list_memories", "read_memory",
    "think_about_task_adherence", "think_about_collected_information",
    "think_about_whether_you_are_done",
    "herdr_list_agents", "herdr_list_panes", "herdr_get_agent", "herdr_read_agent", "herdr_wait_agent",
    "family_context_read", "fern_bridge_status", "fern_inbox_list_or_peek",
    "mcpstudio_current_session", "mcpstudio_get_session", "mcpstudio_list_sessions",
    "mcpstudio_list_workspaces", "mcpstudio_session_history", "mcpstudio_context_status",
    "mcpstudio_lane_status", "mcpstudio_list_capsules", "mcpstudio_get_capsule",
}
_WRITE_TOOLS = {
    "replace_content", "replace_symbol_body", "insert_after_symbol",
    "insert_before_symbol", "rename_symbol", "write_memory", "edit_memory",
    "rename_memory", "create_text_file", "onboarding",
    "mcpstudio_register_workspace", "mcpstudio_rename_session",
    "mcpstudio_set_capsule_contract",
}
_DESTRUCTIVE_TOOLS = {"safe_delete_symbol", "delete_memory"}
_EXECUTE_TOOLS = {
    "activate_project", "herdr_prompt_agent",
    "mcpstudio_use_workspace", "mcpstudio_create_session", "mcpstudio_use_session",
    "mcpstudio_handoff_session", "mcpstudio_accept_handoff", "mcpstudio_report_context_usage",
    "mcpstudio_world_authoring_preview", "mcpstudio_world_authoring_audit",
    "mcpstudio_close_session", "mcpstudio_detach_session",
    "mcpstudio_set_lane_state", "mcpstudio_approve_capsule_handoff",
    "fern_inbox_claim", "fern_reply_submit", "fern_interjection_decide",
}
_SHELL_TOOLS = {"execute_shell_command", "shell", "run_command"}

_DESTRUCTIVE_SHELL = re.compile(
    r"(?:^|[;&|]\s*)(?:sudo\s+)?(?:rm|unlink|rmdir)\b"
    r"|\bgit\s+(?:reset\s+--hard|clean\b|checkout\s+--|restore\b)"
    r"|(?:^|[;&|]\s*)(?:sudo\s+)?(?:kill|pkill|killall|shutdown|reboot)\b"
    r"|\bsystemctl\s+(?:stop|disable|mask)\b"
    r"|\b(?:docker|podman)\s+(?:rm|rmi|prune)\b",
    re.IGNORECASE,
)
_WRITE_SHELL = re.compile(
    r"\bsed\s+-i\b|\btee\b|(?:^|[;&|]\s*)(?:touch|mkdir|cp|mv|chmod|chown)\b"
    r"|\bgit\s+(?:add|commit|merge|rebase|cherry-pick|tag|switch|checkout)\b"
    r"|\b(?:pip|pip3)\s+install\b|\b(?:npm|pnpm|yarn)\s+(?:install|add|remove)\b"
    r"|(?<![0-9])>{1,2}\s*(?!&)",
    re.IGNORECASE,
)

_READ_COMMANDS = {
    "pwd", "ls", "cat", "grep", "rg", "find", "head", "tail", "wc", "stat",
    "printf", "echo", "which", "type", "realpath", "readlink", "env", "printenv",
    "sort", "sed", "journalctl", "ss",
}
_EXECUTE_COMMANDS = {
    "pytest", "uv", "make", "cmake", "ctest", "ninja", "node", "npm", "pnpm", "yarn",
    "cargo", "go", "ruff", "mypy", "eslint", "tsc", "mcporter", "herdr", "curl",
}
_PATH_ARGUMENT_KEYS = {
    "relative_path", "path", "file_path", "directory", "cwd", "workdir",
    "source_path", "destination_path", "target_path",
}
_ALLOWED_EXTERNAL_PATHS = {Path("/dev/null")}


@dataclass(frozen=True, slots=True)
class PermissionDecision:
    allowed: bool
    category: ToolClass
    code: str | None
    message: str
    policy: dict[str, Any]


def _base_tool_name(name: str) -> str:
    value = str(name or "").strip()
    return value.rsplit("__", 1)[-1]


def _git_command_class(tokens: list[str]) -> ToolClass:
    if len(tokens) < 2:
        return "read"
    sub = tokens[1]
    if sub in {"status", "diff", "log", "show", "rev-parse", "ls-files", "ls-tree", "grep"}:
        return "read"
    if sub in {"add", "commit", "merge", "rebase", "cherry-pick", "tag", "switch", "checkout"}:
        return "write"
    if sub in {"push"}:
        return "execute"
    if sub in {"reset", "clean", "restore"}:
        return "destructive"
    return "unknown"


def _segment_class(segment: str) -> ToolClass:
    segment = segment.strip()
    if not segment:
        return "read"
    if re.fullmatch(r"set\s+-[A-Za-z]+", segment):
        return "read"
    try:
        tokens = shlex.split(segment)
    except ValueError:
        return "unknown"
    if not tokens:
        return "read"
    while tokens and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0]):
        tokens.pop(0)
    if tokens and Path(tokens[0]).name == "sudo":
        tokens.pop(0)
    if not tokens:
        return "read"
    command = Path(tokens[0]).name
    if command == "cd":
        return "read"
    if command == "git":
        return _git_command_class(tokens)
    if command == "systemctl":
        args = tokens[1:]
        # systemctl commonly places connection/scope flags before the verb
        # (notably `systemctl --user restart ...`). Treat only known
        # argument-free local flags as transparent; remote/unknown options stay
        # fail-closed so classification cannot silently broaden authority.
        local_flags = {
            "--user", "--system", "--no-pager", "--quiet", "--no-ask-password",
            "--runtime", "--global",
        }
        while args and args[0] in local_flags:
            args.pop(0)
        if not args:
            return "read"
        if args[0].startswith("-"):
            return "unknown"
        sub = args[0]
        if sub in {"status", "is-active", "is-enabled", "show", "list-units", "list-unit-files"}:
            return "read"
        if sub in {"restart", "start", "reload", "try-restart"}:
            return "execute"
        if sub in {"stop", "disable", "mask"}:
            return "destructive"
        return "unknown"
    if command in _READ_COMMANDS:
        return "read"
    if command in _EXECUTE_COMMANDS:
        return "execute"
    if command in {"python", "python3"}:
        if len(tokens) >= 3 and tokens[1:3] in (["-m", "pytest"], ["-m", "unittest"], ["-m", "compileall"]):
            return "execute"
        return "unknown"
    return "unknown"


def _split_shell_segments(text: str) -> list[str]:
    """Split shell chains without treating quoted separators as syntax."""
    segments: list[str] = []
    start = 0
    quote: str | None = None
    escaped = False
    index = 0
    while index < len(text):
        char = text[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if char == "\\" and quote != "'":
            escaped = True
            index += 1
            continue
        if quote:
            if char == quote:
                quote = None
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            index += 1
            continue
        width = 0
        if text.startswith("&&", index) or text.startswith("||", index):
            width = 2
        elif char in {";", "|"}:
            width = 1
        if width:
            part = text[start:index].strip()
            if part:
                segments.append(part)
            index += width
            start = index
            continue
        index += 1
    if quote or escaped:
        raise ValueError("shell command contains an unterminated quote or escape")
    tail = text[start:].strip()
    if tail:
        segments.append(tail)
    return segments


def classify_shell_command(command: str) -> ToolClass:
    text = str(command or "").strip()
    if not text:
        return "unknown"
    if _DESTRUCTIVE_SHELL.search(text):
        return "destructive"
    if _WRITE_SHELL.search(text):
        return "write"
    try:
        segments = _split_shell_segments(text)
    except ValueError:
        return "unknown"
    classes = {_segment_class(part) for part in segments}
    if "unknown" in classes:
        return "unknown"
    if "write" in classes:
        return "write"
    if "execute" in classes:
        return "execute"
    if "destructive" in classes:
        return "destructive"
    return "read" if classes else "unknown"


def classify_tool(tool_name: str, arguments: dict[str, Any] | None = None) -> ToolClass:
    name = _base_tool_name(tool_name)
    args = arguments or {}
    if name == "replace_in_files":
        return "read" if bool(args.get("dry_run")) else "write"
    if name in _DESTRUCTIVE_TOOLS:
        return "destructive"
    if name in _WRITE_TOOLS:
        return "write"
    if name in _EXECUTE_TOOLS:
        return "execute"
    if name in _READ_TOOLS or name.startswith("think_about_"):
        return "read"
    if name in _SHELL_TOOLS:
        return classify_shell_command(str(args.get("command") or ""))
    return "unknown"


def effective_policy(studio: Any, session: dict[str, Any]) -> dict[str, Any]:
    policy: dict[str, Any] = {
        "read": bool(getattr(studio, "managed_session_default_read_allowed", True)),
        "write": bool(getattr(studio, "managed_session_default_write_allowed", True)),
        "execute": bool(getattr(studio, "managed_session_default_execute_allowed", True)),
        "destructive": bool(getattr(studio, "managed_session_default_destructive_allowed", False)),
        "scope": "workspace" if bool(getattr(studio, "managed_session_tool_scope_enforced", True)) else "unrestricted",
        "fail_closed_unknown": bool(getattr(studio, "managed_session_tool_permissions_fail_closed", True)),
    }
    metadata = session.get("metadata") if isinstance(session, dict) else None
    override = metadata.get("tool_permissions") if isinstance(metadata, dict) else None
    if isinstance(override, dict):
        for key in ("read", "write", "execute", "destructive"):
            if isinstance(override.get(key), bool):
                policy[key] = override[key]
        if override.get("scope") in {"workspace", "unrestricted"}:
            policy["scope"] = override["scope"]
        if isinstance(override.get("fail_closed_unknown"), bool):
            policy["fail_closed_unknown"] = override["fail_closed_unknown"]
    return policy


def _within(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def _within_scope(
    root: Path,
    candidate: Path,
    *,
    allow_git_worktree_siblings: bool = False,
) -> bool:
    if _within(root, candidate):
        return True
    return bool(
        allow_git_worktree_siblings
        and is_registered_git_worktree_path(root, candidate)
    )


def _argument_scope_violation(
    arguments: dict[str, Any],
    workspace_root: Path,
    *,
    allow_git_worktree_siblings: bool = False,
) -> str | None:
    for key in _PATH_ARGUMENT_KEYS:
        value = arguments.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        raw = Path(value).expanduser()
        candidate = raw if raw.is_absolute() else workspace_root / raw
        if not _within_scope(
            workspace_root,
            candidate,
            allow_git_worktree_siblings=allow_git_worktree_siblings,
        ):
            return f"argument {key} escapes workspace: {value}"
    return None


def _shell_scope_violation(
    command: str,
    workspace_root: Path,
    *,
    allow_git_worktree_siblings: bool = False,
) -> str | None:
    try:
        segments = _split_shell_segments(command)
        for segment in segments:
            tokens = shlex.split(segment)
            if not tokens:
                continue
            for token in tokens[1:]:
                cleaned = token.strip("'\"(),")
                if "=" in cleaned and not cleaned.startswith(("/", "../", "./")):
                    _, cleaned = cleaned.split("=", 1)
                if cleaned.startswith("~"):
                    candidate = Path(cleaned).expanduser()
                elif cleaned.startswith("/"):
                    candidate = Path(cleaned)
                elif cleaned == ".." or cleaned.startswith("../"):
                    candidate = workspace_root / cleaned
                else:
                    continue
                if candidate in _ALLOWED_EXTERNAL_PATHS:
                    continue
                if not _within_scope(
                    workspace_root,
                    candidate,
                    allow_git_worktree_siblings=allow_git_worktree_siblings,
                ):
                    return f"shell path escapes workspace: {cleaned}"
    except ValueError:
        return "shell command could not be parsed safely"
    return None


def decide_tool_call(studio: Any, session: dict[str, Any], tool_name: str, arguments: dict[str, Any] | None = None) -> PermissionDecision:
    args = arguments or {}
    policy = effective_policy(studio, session)
    category = classify_tool(tool_name, args)
    if category == "unknown":
        if policy["fail_closed_unknown"]:
            return PermissionDecision(False, category, "TOOL_PERMISSION_UNCLASSIFIED", f"Tool {tool_name} is not classified; permission policy is fail-closed.", policy)
        return PermissionDecision(True, category, None, "Unclassified tool allowed by policy.", policy)
    if not bool(policy.get(category, False)):
        return PermissionDecision(False, category, "TOOL_PERMISSION_DENIED", f"{category.upper()} permission is disabled for this managed session.", policy)
    if policy.get("scope") == "workspace":
        project_path = str(session.get("project_path") or "").strip()
        if not project_path:
            return PermissionDecision(False, category, "TOOL_SCOPE_UNAVAILABLE", "Managed session has no project_path for workspace scope enforcement.", policy)
        root = Path(project_path).expanduser().resolve(strict=False)
        allow_git_worktree_siblings = bool(
            getattr(studio, "managed_session_allow_git_worktree_siblings", False)
        )
        violation = _argument_scope_violation(
            args,
            root,
            allow_git_worktree_siblings=allow_git_worktree_siblings,
        )
        if not violation and _base_tool_name(tool_name) in _SHELL_TOOLS:
            cwd = args.get("cwd")
            if isinstance(cwd, str) and cwd.strip():
                cwd_path = Path(cwd).expanduser()
                if not cwd_path.is_absolute():
                    cwd_path = root / cwd_path
                if not _within_scope(
                    root,
                    cwd_path,
                    allow_git_worktree_siblings=allow_git_worktree_siblings,
                ):
                    violation = f"shell cwd escapes workspace: {cwd}"
            if not violation:
                violation = _shell_scope_violation(
                    str(args.get("command") or ""),
                    root,
                    allow_git_worktree_siblings=allow_git_worktree_siblings,
                )
        if violation:
            return PermissionDecision(False, category, "TOOL_SCOPE_VIOLATION", violation, policy)
    return PermissionDecision(True, category, None, f"{category.upper()} permission granted.", policy)
