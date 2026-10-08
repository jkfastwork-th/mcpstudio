from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1] / "mcp_studio" / "disk_maintenance.py"
spec = importlib.util.spec_from_file_location("disk_maintenance_isolated", MODULE)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
DiskMaintenance = module.DiskMaintenance
DiskMaintenanceError = module.DiskMaintenanceError


def setup_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    releases = home / ".codex" / "packages" / "standalone" / "releases"
    for version in ("0.100.0", "0.101.0", "0.102.0", "0.103.0"):
        path = releases / (version + "-x86_64-unknown-linux-musl")
        path.mkdir(parents=True)
        (path / "codex").write_bytes(b"x" * 4096)
    monkeypatch.setattr(DiskMaintenance, "_process_commands", staticmethod(lambda: []))
    return home, releases


@pytest.mark.asyncio
async def test_retain_three_codex_versions_and_delete_only_old_with_explicit_policy(tmp_path, monkeypatch):
    home, releases = setup_home(tmp_path, monkeypatch)
    service = DiskMaintenance(home)
    plan = await service.plan(["codex.old_releases"])
    assert len(plan["candidates"]) == 1
    assert "0.100.0" in plan["candidates"][0]["id"]
    assert plan["dry_run"]
    assert plan["estimated_bytes"] == 4096
    auth = await service.approve(plan["plan_id"], plan["fingerprint"])
    with pytest.raises(DiskMaintenanceError, match="maintenance not enabled"):
        await service.apply(plan["plan_id"], auth["approval_token"],
                            enabled=False, machine_maintenance_permission=True)
    assert (releases / "0.100.0-x86_64-unknown-linux-musl").exists()
    result = await service.apply(plan["plan_id"], auth["approval_token"],
                                 enabled=True, machine_maintenance_permission=True)
    assert result["completed"] == [plan["candidates"][0]["id"]]
    assert not (releases / "0.100.0-x86_64-unknown-linux-musl").exists()
    assert (releases / "0.103.0-x86_64-unknown-linux-musl").exists()
    with pytest.raises(DiskMaintenanceError):
        await service.apply(plan["plan_id"], auth["approval_token"],
                            enabled=True, machine_maintenance_permission=True)


@pytest.mark.asyncio
async def test_machine_policy_denies_cleanup_by_default(tmp_path, monkeypatch):
    home, releases = setup_home(tmp_path, monkeypatch)
    service = DiskMaintenance(home)
    plan = await service.plan(["codex.old_releases"])
    auth = await service.approve(plan["plan_id"], plan["fingerprint"])
    with pytest.raises(DiskMaintenanceError, match="machine policy"):
        await service.apply(plan["plan_id"], auth["approval_token"],
                            enabled=True, machine_maintenance_permission=False)
    assert (releases / "0.100.0-x86_64-unknown-linux-musl").exists()


@pytest.mark.asyncio
async def test_mutation_after_plan_blocks_apply(tmp_path, monkeypatch):
    home, releases = setup_home(tmp_path, monkeypatch)
    service = DiskMaintenance(home)
    plan = await service.plan(["codex.old_releases"])
    auth = await service.approve(plan["plan_id"], plan["fingerprint"])
    target = releases / "0.100.0-x86_64-unknown-linux-musl"
    (target / "changed.txt").write_text("updated")
    with pytest.raises(DiskMaintenanceError, match="filesystem or process changed"):
        await service.apply(plan["plan_id"], auth["approval_token"],
                            enabled=True, machine_maintenance_permission=True)
    assert target.exists()


@pytest.mark.asyncio
async def test_unknown_target_and_duplicate_approval_rejected(tmp_path, monkeypatch):
    home, _ = setup_home(tmp_path, monkeypatch)
    service = DiskMaintenance(home)
    with pytest.raises(DiskMaintenanceError, match="unknown"):
        await service.plan(["/home/alfred/mcp-studio/backups"])
    plan = await service.plan(["codex.old_releases"])
    with pytest.raises(DiskMaintenanceError, match="fingerprint"):
        await service.approve(plan["plan_id"], "bad")
    await service.approve(plan["plan_id"], plan["fingerprint"])
    with pytest.raises(DiskMaintenanceError, match="already approved"):
        await service.approve(plan["plan_id"], plan["fingerprint"])


@pytest.mark.asyncio
async def test_active_codex_release_is_preserved(tmp_path, monkeypatch):
    home, releases = setup_home(tmp_path, monkeypatch)
    active = str(releases / "0.100.0-x86_64-unknown-linux-musl")
    monkeypatch.setattr(DiskMaintenance, "_process_commands",
                        staticmethod(lambda: [f"{active}/bin/codex app-server"]))
    service = DiskMaintenance(home)
    plan = await service.plan(["codex.old_releases"])
    assert plan["candidates"] == []
    assert any(x["reason"] == "in_use" for x in plan["skipped"])


@pytest.mark.asyncio
async def test_cache_paths_are_fixed_and_active_package_managers_blocked(tmp_path, monkeypatch):
    home, _ = setup_home(tmp_path, monkeypatch)
    npm = home / ".npm" / "_cacache"
    bun = home / ".bun" / "install" / "cache"
    for cache in (npm, bun):
        cache.mkdir(parents=True)
        (cache / "file").write_text("cached")
    monkeypatch.setattr(DiskMaintenance, "_process_commands",
                        staticmethod(lambda: ["npm install --save foo", "bunx --bun foo"]))
    service = DiskMaintenance(home)
    plan = await service.plan(["npm.cacache", "bun.download_cache"])
    assert not plan["candidates"]
    assert {x["reason"] for x in plan["skipped"]} == {"package_manager_active"}


@pytest.mark.asyncio
async def test_symlinked_cache_root_is_rejected(tmp_path, monkeypatch):
    home, _ = setup_home(tmp_path, monkeypatch)
    external = tmp_path / "important"
    external.mkdir()
    (home / ".npm").mkdir()
    (home / ".npm" / "_cacache").symlink_to(external, target_is_directory=True)
    service = DiskMaintenance(home)
    with pytest.raises(DiskMaintenanceError, match="unsafe"):
        await service.plan(["npm.cacache"])
    assert external.exists()

@pytest.mark.asyncio
async def test_fd_guard_blocks_parent_symlink_replacement(tmp_path, monkeypatch):
    home, releases = setup_home(tmp_path, monkeypatch)
    service = DiskMaintenance(home)
    target = releases / "0.100.0-x86_64-unknown-linux-musl"
    external = tmp_path / "important"
    external.mkdir()
    (external / target.name).mkdir()
    # Replace the directory parent with a symlink to protected outside data.
    release_parent = releases.parent
    releases.rename(release_parent / "releases-original")
    releases.symlink_to(external, target_is_directory=True)
    with pytest.raises(DiskMaintenanceError, match="unsafe"):
        service._remove_with_parent_fd(target)
    assert (external / target.name).exists()


@pytest.mark.asyncio
async def test_partial_apply_failure_is_reported_without_replay(tmp_path, monkeypatch):
    home, releases = setup_home(tmp_path, monkeypatch)
    # One old version in each Codex package family.
    other = home / ".codex/packages/app-server-daemon/releases"
    for version in ("0.100.0", "0.101.0", "0.102.0", "0.103.0"):
        entry = other / (version + "-x86_64-unknown-linux-musl")
        entry.mkdir(parents=True)
        (entry / "binary").write_bytes(b"abc")
    service = DiskMaintenance(home)
    plan = await service.plan(["codex.old_releases"])
    assert len(plan["candidates"]) == 2
    token = (await service.approve(plan["plan_id"], plan["fingerprint"]))["approval_token"]
    real_remove = service._remove_with_parent_fd
    removed = []

    def partial(path):
        if removed:
            raise OSError("injected cleanup failure")
        real_remove(path)
        removed.append(path)

    monkeypatch.setattr(service, "_remove_with_parent_fd", partial)
    with pytest.raises(module.DiskMaintenanceApplyError) as caught:
        await service.apply(plan["plan_id"], token, enabled=True,
                            machine_maintenance_permission=True)
    assert len(caught.value.completed) == 1
    with pytest.raises(DiskMaintenanceError, match="not approved"):
        await service.apply(plan["plan_id"], token, enabled=True,
                            machine_maintenance_permission=True)
