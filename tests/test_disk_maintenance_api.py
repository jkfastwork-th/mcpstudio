from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from mcp_studio import main
from mcp_studio.disk_maintenance import DiskMaintenance

URL = "/api/maintenance/disk"
OWNER = {"x-hirda-owner-token": "fixture-owner-token"}


@pytest.fixture
def guarded_api(tmp_path, monkeypatch):
    home = tmp_path / "local-node-user"
    home.mkdir()
    releases = home / ".codex/packages/standalone/releases"
    for version in ("0.100.0", "0.101.0", "0.102.0", "0.103.0"):
        entry = releases / (version + "-x86_64-unknown-linux-musl")
        entry.mkdir(parents=True)
        (entry / "bin").mkdir()
        (entry / "bin/codex").write_bytes(b"test-only")
    engine = DiskMaintenance(home)
    monkeypatch.setattr(DiskMaintenance, "_process_commands", staticmethod(lambda: []))
    monkeypatch.setattr(main, "disk_maintenance", engine)
    monkeypatch.setattr(main.oauth, "verify_owner_token", lambda token: token == "fixture-owner-token")
    audit = AsyncMock()
    monkeypatch.setattr(main.db, "add_audit", audit)
    policy = SimpleNamespace(allowed=False)
    local = SimpleNamespace(local=True, enabled=True, machine_id="openclaw")
    registry = SimpleNamespace(
        local_machine_id="openclaw",
        get_node=lambda _machine: local,
        authorize_session=lambda _session, category: policy if category == "maintenance" else None,
    )
    monkeypatch.setattr(main, "machine_registry", registry)
    monkeypatch.setattr(main.settings.studio, "disk_maintenance_enabled", False)
    return releases, policy, audit


@pytest.mark.asyncio
async def test_unauthorized_requests_do_not_scan_or_approve_or_apply(guarded_api):
    releases, policy, audit = guarded_api
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        valid_payloads = {
            "status": None,
            "plan": {"targets": ["codex.old_releases"]},
            "approve": {"plan_id": "unprivileged", "fingerprint": "invalid"},
            "apply": {"plan_id": "unprivileged", "approval_token": "invalid"},
        }
        for path, payload in valid_payloads.items():
            method = "get" if payload is None else "post"
            for headers in ({}, {"cookie": "mcpstudio_owner_session=fixture-cookie"},
                            {"x-hirda-owner-token": "not-owner"}):
                response = await client.request(method, f"{URL}/{path}", json=payload, headers=headers)
                assert response.status_code == 403
    audit.assert_not_awaited()
    assert (releases / "0.100.0-x86_64-unknown-linux-musl").exists()


@pytest.mark.asyncio
async def test_authenticated_end_to_end_requires_two_independent_policy_gates(guarded_api):
    releases, policy, audit = guarded_api
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        status = await client.get(f"{URL}/status", headers=OWNER)
        assert status.status_code == 200
        assert status.json()["enabled"] is False
        assert status.json()["maintenance_permission"] is False

        p = await client.post(f"{URL}/plan", headers=OWNER, json={"targets": ["codex.old_releases"]})
        assert p.status_code == 200, p.text
        plan = p.json()
        assert plan["dry_run"] is True
        assert len(plan["candidates"]) == 1
        assert plan["candidates"][0]["path"].startswith(str(releases))

        approval = await client.post(f"{URL}/approve", headers=OWNER,
                                     json={"plan_id": plan["plan_id"], "fingerprint": plan["fingerprint"]})
        assert approval.status_code == 200
        token = approval.json()["approval_token"]

        payload = {"plan_id": plan["plan_id"], "approval_token": token}
        r = await client.post(f"{URL}/apply", headers=OWNER, json=payload)
        assert r.status_code == 403
        assert (releases / "0.100.0-x86_64-unknown-linux-musl").exists()

        main.settings.studio.disk_maintenance_enabled = True
        r = await client.post(f"{URL}/apply", headers=OWNER, json=payload)
        assert r.status_code == 403
        assert (releases / "0.100.0-x86_64-unknown-linux-musl").exists()

        policy.allowed = True
        r = await client.post(f"{URL}/apply", headers=OWNER, json=payload)
        assert r.status_code == 200, r.text
        assert len(r.json()["completed"]) == 1
        assert not (releases / "0.100.0-x86_64-unknown-linux-musl").exists()
        assert (releases / "0.103.0-x86_64-unknown-linux-musl").exists()

        r = await client.post(f"{URL}/apply", headers=OWNER, json=payload)
        assert r.status_code == 409

        kinds = [call.args[0] for call in audit.await_args_list]
        assert "maintenance.disk.plan" in kinds
        assert "maintenance.disk.approve" in kinds
        assert "maintenance.disk.denied" in kinds
        assert "maintenance.disk.apply_attempt" in kinds
        assert "maintenance.disk.applied" in kinds


@pytest.mark.asyncio
async def test_rejects_unknown_target_and_manifest_tampering(guarded_api):
    _, policy, audit = guarded_api
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        bad = await client.post(f"{URL}/plan", headers=OWNER, json={"targets": ["/etc"]})
        assert bad.status_code == 409
        p = await client.post(f"{URL}/plan", headers=OWNER, json={"targets": ["codex.old_releases"]})
        plan = p.json()
        bad = await client.post(f"{URL}/approve", headers=OWNER,
                                json={"plan_id": plan["plan_id"], "fingerprint": "not-a-real-fingerprint"})
        assert bad.status_code == 409
        assert (await client.get(f"{URL}/status", headers=OWNER)).status_code == 200
