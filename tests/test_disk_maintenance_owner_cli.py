from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "disk_maintenance_owner.py"
spec = importlib.util.spec_from_file_location("disk_maintenance_owner_tool", SCRIPT)
cli = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = cli
spec.loader.exec_module(cli)


@pytest.fixture
def fake_api(monkeypatch):
    calls = []
    fingerprint = "a" * 64
    response = {
        "dry_run": True,
        "fingerprint": fingerprint,
        "plan_id": "fixture-plan",
        "estimated_bytes": 100,
        "candidates": [{"id": "codex:standalone:0.100.0", "bytes": 100}],
        "skipped": [],
    }

    def handler(method, path, token, payload=None):
        assert token == "fixture-secret"
        calls.append((path, payload))
        if path.endswith("status"):
            return {"machine_id": "openclaw", "machine_local": True,
                    "enabled": True, "maintenance_permission": True}
        if path.endswith("plan"):
            assert payload == {"targets": ["codex.old_releases"]}
            return response
        if path.endswith("approve"):
            return {"approval_token": "only-once"}
        if path.endswith("apply"):
            return {"completed": ["codex:standalone:0.100.0"],
                    "estimated_bytes_removed": 100}
        raise AssertionError(path)

    monkeypatch.setattr(cli, "api", handler)
    monkeypatch.setattr(cli, "free_gib", lambda: 24.0)
    monkeypatch.setenv("MCP_STUDIO_OAUTH_OWNER_TOKEN", "fixture-secret")
    return calls, response


def test_defaults_to_dry_run_without_approval(fake_api, monkeypatch, capsys):
    calls, _ = fake_api
    monkeypatch.setattr(cli.sys, "argv", ["disk_maintenance_owner.py"])
    assert cli.main() == 0
    assert [name.split("/")[-1] for name, _ in calls] == ["status", "plan"]
    assert "DRY RUN ONLY" in capsys.readouterr().out


def test_apply_without_typed_fingerprint_is_refused(fake_api, monkeypatch):
    calls, _ = fake_api
    monkeypatch.setattr(cli.sys, "argv", ["disk_maintenance_owner.py", "--apply"])
    monkeypatch.setattr("builtins.input", lambda _text: "yes")
    assert cli.main() == 2
    assert [name.split("/")[-1] for name, _ in calls] == ["status", "plan"]


def test_apply_uses_exact_single_use_plan_token(fake_api, monkeypatch):
    calls, response = fake_api
    monkeypatch.setattr(cli.sys, "argv", ["disk_maintenance_owner.py", "--apply"])
    monkeypatch.setattr("builtins.input", lambda _text: "APPROVE " + response["fingerprint"][:12])
    assert cli.main() == 0
    assert [name.split("/")[-1] for name, _ in calls] == ["status", "plan", "approve", "apply"]
    assert calls[-1][1] == {"plan_id": "fixture-plan", "approval_token": "only-once"}


def test_refuses_server_scope_escape(fake_api, monkeypatch):
    calls, response = fake_api
    response["candidates"][0]["id"] = "npm.cacache"
    monkeypatch.setattr(cli.sys, "argv", ["disk_maintenance_owner.py", "--apply"])
    monkeypatch.setattr("builtins.input", lambda _text: pytest.fail("should never prompt to apply"))
    assert cli.main() == 1
    assert [name.split("/")[-1] for name, _ in calls] == ["status", "plan"]
