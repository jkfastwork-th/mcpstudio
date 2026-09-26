from pathlib import Path
from types import SimpleNamespace

from mcp_studio.device_runtime import (
    DeviceIdentityStore,
    DeviceRuntime,
    DeviceRuntimeConfig,
)
from mcp_studio.machine_enrollment import MachineEnrollmentManager
from mcp_studio.node_backends import (
    NODE_BACKENDS_SCHEMA,
    NodeBackendDiscoveryError,
    normalize_backend_observation,
    normalize_backend_probe_specs,
    probe_configured_backends,
    refresh_local_node_backends,
)
from mcp_studio.node_observations import (
    ingest_node_observations,
    normalize_node_observations,
)
from mcp_studio.node_registry import NodeRegistry
from mcp_studio.settings import StudioConfig


class FakeBackend:
    async def call(self, **kwargs):
        return {"content": [{"type": "text", "text": "OK"}]}

    async def close(self):
        return None


def studio(tmp_path: Path) -> StudioConfig:
    return StudioConfig(
        machine_local_id="openclaw",
        machine_registry=[
            {
                "id": "openclaw",
                "name": "openclaw",
                "enabled": True,
                "local": True,
                "os": "linux",
                "address": "127.0.0.1",
                "capabilities": ["filesystem", "process"],
                "providers": {"desktop_commander": {"mode": "local"}},
                "policy": {
                    "read": True,
                    "write": True,
                    "execute": True,
                    "destructive": False,
                    "fail_closed_unknown": True,
                },
            }
        ],
        machine_enrollment_enabled=True,
        machine_enrollment_state_path=str(tmp_path / "enrollment.json"),
        machine_enrollment_agent_port=8765,
        machine_enrollment_allowed_cidrs=["100.64.0.0/10"],
    )


def backend_observation(*, available: bool = True) -> dict:
    return {
        "schema": NODE_BACKENDS_SCHEMA,
        "observed_at": "2026-09-26T02:00:00+00:00",
        "backends": [
            {
                "id": "runtime-x",
                "kind": "inference",
                "available": available,
                "binary": "/opt/runtime-x/bin/runtime",
                "version": "runtime-x 1.2.3",
                "capabilities": ["text-generation"],
            }
        ],
    }


def remote_identity() -> dict:
    return {
        "schema": "hirda-machine-agent-v1",
        "machine_id": "REMOTE",
        "hostname": "remote",
        "platform": "linux",
        "architecture": "test-arch",
        "agent_version": "1",
        "capabilities": ["filesystem", "process"],
        "providers": {
            "desktop_commander": {
                "available": True,
                "tool_count": 13,
            }
        },
        "observations": {
            "runtime.backends": backend_observation(),
        },
        "address": "100.100.10.20",
        "endpoint": "http://100.100.10.20:8765",
        "fingerprint": "f" * 64,
    }


def test_probe_specs_are_generic_and_strict() -> None:
    specs = normalize_backend_probe_specs(
        [
            {
                "id": "runtime-x",
                "kind": "inference",
                "command": ["runtime-x"],
                "version_args": ["--version"],
                "capabilities": ["text-generation"],
            }
        ]
    )
    assert specs[0]["id"] == "runtime-x"
    assert specs[0]["kind"] == "inference"
    assert specs[0]["command"] == ["runtime-x"]

    try:
        normalize_backend_probe_specs([{"id": "../bad", "command": ["x"]}])
    except NodeBackendDiscoveryError:
        pass
    else:
        raise AssertionError("invalid backend id must fail closed")


def test_configured_backend_probe_reports_availability_and_version(monkeypatch) -> None:
    import mcp_studio.node_backends as backends

    monkeypatch.setattr(backends.shutil, "which", lambda name: "/usr/local/bin/runtime-x")
    monkeypatch.setattr(
        backends.subprocess,
        "run",
        lambda argv, **kwargs: SimpleNamespace(
            returncode=0,
            stdout="runtime-x 1.2.3\n",
            stderr="",
        ),
    )

    result = probe_configured_backends(
        [
            {
                "id": "runtime-x",
                "kind": "inference",
                "command": ["runtime-x"],
                "version_args": ["--version"],
                "capabilities": ["text-generation"],
            }
        ],
        observed_at="2026-09-26T02:01:00+00:00",
    )

    assert result["schema"] == NODE_BACKENDS_SCHEMA
    assert result["backends"] == [
        {
            "id": "runtime-x",
            "kind": "inference",
            "available": True,
            "binary": "/usr/local/bin/runtime-x",
            "version": "runtime-x 1.2.3",
            "capabilities": ["text-generation"],
        }
    ]


def test_backend_observation_normalizer_sanitizes_and_deduplicates() -> None:
    raw = backend_observation()
    raw["backends"].append(
        {
            "id": "runtime-x",
            "kind": "inference",
            "available": True,
            "binary": "/tmp/duplicate",
        }
    )
    raw["backends"].append(
        {
            "id": "../invalid",
            "kind": "inference",
            "available": True,
        }
    )

    result = normalize_backend_observation(raw)

    assert result is not None
    assert [item["id"] for item in result["backends"]] == ["runtime-x"]
    assert result["backends"][0]["version"] == "runtime-x 1.2.3"


def test_local_backend_refresh_preserves_node_authority(monkeypatch, tmp_path: Path) -> None:
    import mcp_studio.node_backends as backends

    registry = NodeRegistry(studio(tmp_path), base_dir=tmp_path)
    node = registry.get_node("openclaw")
    policy_before = dict(node.policy)
    providers_before = dict(node.providers)
    capabilities_before = tuple(node.capabilities)

    monkeypatch.setattr(
        backends,
        "probe_configured_backends",
        lambda raw_specs: backend_observation(),
    )

    result = refresh_local_node_backends(
        registry,
        [{"id": "runtime-x", "command": ["runtime-x"]}],
    )

    assert result is not None
    assert registry.get_node("openclaw") is node
    assert node.policy == policy_before
    assert node.providers == providers_before
    assert node.capabilities == capabilities_before
    facet = registry.lifecycle("openclaw")["enrichment"]["facets"]["runtime.backends"]
    assert facet["source"] == "local_probe"
    assert facet["data"]["backends"][0]["id"] == "runtime-x"


def test_observation_ingestor_accepts_known_facets_and_ignores_unknown(tmp_path: Path) -> None:
    registry = NodeRegistry(studio(tmp_path), base_dir=tmp_path)

    accepted = ingest_node_observations(
        registry,
        "openclaw",
        {
            "runtime.backends": backend_observation(),
            "untrusted.unknown": {
                "observed_at": "2026-09-26T02:00:00+00:00",
                "payload": "ignored",
            },
        },
        source="test-runtime",
    )

    assert sorted(accepted) == ["runtime.backends"]
    facets = registry.lifecycle("openclaw")["enrichment"]["facets"]
    assert "runtime.backends" in facets
    assert "untrusted.unknown" not in facets
    assert normalize_node_observations({"untrusted.unknown": {}}) == {}


def test_enrollment_enriches_runtime_backend_after_node_creation(tmp_path: Path) -> None:
    cfg = studio(tmp_path)
    registry = NodeRegistry(cfg, base_dir=tmp_path)
    manager = MachineEnrollmentManager(cfg, registry, base_dir=tmp_path)
    manager._state["pending"]["REMOTE"] = remote_identity()

    result = manager.approve("REMOTE", actor="n3-test")

    assert result["status"] == "approved"
    lifecycle = registry.lifecycle("REMOTE")
    assert lifecycle["state"] == "enrolled"
    facet = lifecycle["enrichment"]["facets"]["runtime.backends"]
    assert facet["source"] == "machine_agent"
    assert facet["data"]["backends"][0]["id"] == "runtime-x"

    approved_record = manager._state["approved"]["REMOTE"]["machine"]
    assert "observations" not in approved_record


def test_shared_device_runtime_builds_same_observation_contract(monkeypatch, tmp_path: Path) -> None:
    import mcp_studio.device_runtime as runtime_module

    config = DeviceRuntimeConfig.from_dict(
        {
            "control_plane_url": "https://hirda.example",
            "name": "device",
            "identity_path": ".hirda/key.pem",
            "desktop_commander_command": ["desktop-commander"],
            "workspaces": {},
            "capabilities": ["filesystem", "process"],
            "backend_probes": [
                {
                    "id": "runtime-x",
                    "kind": "inference",
                    "command": ["runtime-x"],
                    "version_args": ["--version"],
                }
            ],
        },
        base_dir=tmp_path,
    )
    identity = DeviceIdentityStore(config.identity_path).load_or_create()
    monkeypatch.setattr(
        runtime_module,
        "probe_hardware_resources",
        lambda storage_path=None: {
            "schema": "hirda-node-hardware-v1",
            "observed_at": "2026-09-26T02:00:00+00:00",
            "architecture": "test",
            "processor": None,
            "cpu": {"logical_count": 4},
            "memory": {"total_bytes": 1000},
            "storage": {"total_bytes": 10, "used_bytes": 4, "free_bytes": 6},
        },
    )
    monkeypatch.setattr(
        runtime_module,
        "probe_configured_backends",
        lambda raw_specs: backend_observation(),
    )

    runtime = DeviceRuntime(config, identity=identity, backend=FakeBackend())
    observations = runtime.node_observations()

    assert config.backend_probes[0]["id"] == "runtime-x"
    assert observations["runtime.backends"]["schema"] == NODE_BACKENDS_SCHEMA
    assert observations["runtime.backends"]["backends"][0]["id"] == "runtime-x"
    assert observations["hardware.resources"]["schema"] == "hirda-node-hardware-v1"


def test_machine_agent_and_broker_expose_generic_observation_path() -> None:
    root = Path(__file__).resolve().parents[1]
    agent_source = (root / "scripts" / "hirda_machine_agent.py").read_text()
    main_source = (root / "mcp_studio" / "main.py").read_text()

    assert '"runtime.backends": probe_configured_backends(backend_probes)' in agent_source
    assert 'config.get("backend_probes")' in agent_source
    assert 'if kind == "node_observations":' in main_source
    assert 'source="device_runtime"' in main_source


def test_n3_core_contains_no_product_or_runtime_vendor_hardcoding() -> None:
    root = Path(__file__).resolve().parents[1]
    source = (
        (root / "mcp_studio" / "node_backends.py").read_text(encoding="utf-8")
        + (root / "mcp_studio" / "node_observations.py").read_text(encoding="utf-8")
    ).casefold()
    for forbidden in ("nova", "ollama", "openvino", "mlx"):
        assert forbidden not in source
