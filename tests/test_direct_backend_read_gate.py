from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from mcp_studio.gateway import GatewaySessionManager, _backend_requires_pstack_task
from mcp_studio.tool_permissions import decide_tool_call


@pytest.mark.parametrize(
    "tool_name",
    (
        "hirda__hirda_native_computer__list_directory",
        "hirda__hirda_native_computer__read_file",
    ),
)
def test_direct_native_read_does_not_require_pstack(tool_name):
    assert not _backend_requires_pstack_task(tool_name, "read", "read")


@pytest.mark.parametrize(
    ("tool_name", "declared", "effective"),
    (
        ("hirda__hirda_native_computer__read_file", "write", "write"),
        ("hirda__hirda_native_computer__read_file", None, "read"),
        ("hirda__hirda_native_computer__read_file", "read", "unknown"),
        ("hirda__hirda_native_computer__read_file", "read", "write"),
        ("hirda__hirda_native_computer__list_directory", "execute", "execute"),
        ("hirda__hirda_native_computer__write_file", "write", "write"),
        ("hirda__hirda_native_computer__start_process", "execute", "execute"),
        ("hirda__hirda_native_computer__get_file_info", "read", "read"),
        ("hirda__desktop_commander__read_file", "read", "read"),
        ("read_file", "read", "read"),
    ),
)
def test_all_other_backend_tools_still_require_pstack(tool_name, declared, effective):
    assert _backend_requires_pstack_task(tool_name, declared, effective)


def _studio(*, read=True):
    return SimpleNamespace(
        managed_session_default_read_allowed=read,
        managed_session_default_write_allowed=False,
        managed_session_default_execute_allowed=False,
        managed_session_default_destructive_allowed=False,
        managed_session_tool_scope_enforced=True,
        managed_session_tool_permissions_fail_closed=True,
        managed_session_allow_git_worktree_siblings=False,
    )


def _session(tmp_path):
    return {"project_path": str(tmp_path), "workspace_key": "direct-smoke", "metadata": {}}


def test_direct_reads_still_enforce_workspace_and_permissions(tmp_path):
    session = _session(tmp_path)
    for tool, good_path in (
        ("hirda__hirda_native_computer__list_directory", str(tmp_path)),
        ("hirda__hirda_native_computer__read_file", str(tmp_path / "README.md")),
    ):
        allowed = decide_tool_call(
            _studio(), session, tool, {"path": good_path}, declared_category="read"
        )
        assert allowed.allowed
        assert allowed.category == "read"
        assert not _backend_requires_pstack_task(tool, "read", allowed.category)
        denied_scope = decide_tool_call(
            _studio(), session, tool, {"path": str(tmp_path.parent / "outside.txt")},
            declared_category="read"
        )
        assert not denied_scope.allowed
        assert denied_scope.code == "TOOL_SCOPE_VIOLATION"
        denied_read = decide_tool_call(
            _studio(read=False), session, tool, {"path": good_path},
            declared_category="read"
        )
        assert not denied_read.allowed
        assert denied_read.code == "TOOL_PERMISSION_DENIED"


def test_no_task_exemption_is_only_inside_backend_gate():
    source = inspect.getsource(GatewaySessionManager.proxy_existing)
    start = source.index("            backend_pstack_task = None")
    end = source.index("\n        pstack_task = None", start)
    backend = source[start:end]
    assert "if not session.get(\"managed_session_id\")" in backend
    assert "decision = decide_tool_call(" in backend
    assert "_backend_requires_pstack_task(" in backend
    assert "if backend_pstack_task:" in backend
    assert backend.index("decision = decide_tool_call(") < backend.index("_backend_requires_pstack_task(")
    assert backend.index("pstack_workflow_gate(") < backend.index("self.machine_router.call_backend_tool(")
    assert '"PSTACK_TASK_REQUIRED"' in backend
    generic = source[end:]
    assert '"PSTACK_TASK_REQUIRED"' in generic
