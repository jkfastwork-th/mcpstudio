from pathlib import Path

from mcp_studio.device_runtime import DeviceIdentityStore, DeviceRuntime, DeviceRuntimeConfig
from mcp_studio.machine_registry import MachineRegistryError
from mcp_studio.node_backends import NODE_BACKENDS_SCHEMA
from mcp_studio.node_capabilities import (
    NODE_CAPABILITIES_SCHEMA,
    build_capability_advertisement,
    normalize_capability_advertisement,
    refresh_local_node_capabilities,
)
from mcp_studio.node_models import NODE_MODELS_SCHEMA
from mcp_studio.node_observations import ingest_node_observations
from mcp_studio.node_registry import NodeRegistry
from mcp_studio.settings import StudioConfig


class FakeBackend:
    async def call(self, **kwargs):
        return {"content": [{"type": "text", "text": "OK"}]}

    async def close(self):
        return None


def studio(tmp_path: Path, *, execute: bool = False) -> StudioConfig:
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
                    "execute": execute,
                    "destructive": False,
                    "fail_closed_unknown": True,
                },
            }
        ],
    )


def backend_observation() -> dict:
    return {
        "schema": NODE_BACKENDS_SCHEMA,
        "observed_at": "2026-09-26T04:00:00+00:00",
        "backends": [
            {
                "id": "runtime-x",
                "kind": "inference",
                "available": True,
                "binary": "/usr/bin/runtime-x",
                "version": "runtime-x 1",
                "capabilities": ["text-generation", "embeddings"],
            }
        ],
    }


def model_observation() -> dict:
    return {
        "schema": NODE_MODELS_SCHEMA,
        "observed_at": "2026-09-26T04:00:01+00:00",
        "node_id": "ignored-from-sender",
        "inventories": [
            {
                "backend_id": "runtime-x",
                "status": "ready",
                "models": [
                    {"id": "model-a", "name": "Model A"},
                    {"id": "model-b", "name": "Model B"},
                ],
            }
        ],
    }


def capability_observation() -> dict:
    return {
        "schema": NODE_CAPABILITIES_SCHEMA,
        "observed_at": "2026-09-26T04:00:02+00:00",
        "node": {
            "capabilities": ["filesystem", "process", "inference"],
        },
        "backends": [
            {
                "backend_id": "runtime-x",
                "capabilities": ["text-generation", "embeddings"],
            },
            {
                "backend_id": "forged-backend",
                "capabilities": ["dangerous"],
            },
        ],
        "models": [
            {
                "backend_id": "runtime-x",
                "model_id": "model-a",
                "capabilities": ["chat"],
            },
            {
                "backend_id": "runtime-x",
                "model_id": "forged-model",
                "capabilities": ["dangerous"],
            },
            {
                "backend_id": "forged-backend",
                "model_id": "model-a",
                "capabilities": ["dangerous"],
            },
        ],
    }


def test_builder_preserves_node_backend_model_lineage() -> None:
    result = build_capability_advertisement(
        ["filesystem", "process"],
        backend_observation(),
        model_observation(),
        backend_specs=[
            {
                "id": "runtime-x",
                "model_capabilities": ["chat", "tool-use"],
            }
        ],
        observed_at="2026-09-26T04:00:02+00:00",
    )

    assert result == {
        "schema": NODE_CAPABILITIES_SCHEMA,
        "observed_at": "2026-09-26T04:00:02+00:00",
        "node": {
            "capabilities": ["filesystem", "process"],
        },
        "backends": [
            {
                "backend_id": "runtime-x",
                "capabilities": ["text-generation", "embeddings"],
            }
        ],
        "models": [
            {
                "backend_id": "runtime-x",
                "model_id": "model-a",
                "capabilities": ["chat", "tool-use"],
            },
            {
                "backend_id": "runtime-x",
                "model_id": "model-b",
                "capabilities": ["chat", "tool-use"],
            },
        ],
    }


def test_normalizer_sanitizes_and_deduplicates_claims() -> None:
    raw = capability_observation()
    raw["node"]["capabilities"].extend(["filesystem", "../bad"])
    raw["backends"].append(
        {
            "backend_id": "runtime-x",
            "capabilities": ["duplicate"],
        }
    )
    raw["models"].append(
        {
            "backend_id": "runtime-x",
            "model_id": "model-a",
            "capabilities": ["duplicate"],
        }
    )

    result = normalize_capability_advertisement(raw)

    assert result is not None
    assert result["node"]["capabilities"] == ["filesystem", "process", "inference"]
    assert [item["backend_id"] for item in result["backends"]] == [
        "runtime-x",
        "forged-backend",
    ]
    assert len(result["models"]) == 3


def test_ingestion_stamps_node_id_and_filters_unknown_lineage(tmp_path: Path) -> None:
    registry = NodeRegistry(studio(tmp_path), base_dir=tmp_path)

    accepted = ingest_node_observations(
        registry,
        "openclaw",
        {
            "runtime.backends": backend_observation(),
            "runtime.models": model_observation(),
            "capabilities.advertised": capability_observation(),
        },
        source="authenticated-device",
    )

    capabilities = accepted["capabilities.advertised"]
    assert capabilities["node_id"] == "openclaw"
    assert [item["backend_id"] for item in capabilities["backends"]] == [
        "runtime-x"
    ]
    assert capabilities["models"] == [
        {
            "backend_id": "runtime-x",
            "model_id": "model-a",
            "capabilities": ["chat"],
        }
    ]

    stored = registry.lifecycle("openclaw")["enrichment"]["facets"][
        "capabilities.advertised"
    ]["data"]
    assert stored["node_id"] == "openclaw"


def test_advertisement_does_not_mutate_machine_authority(tmp_path: Path) -> None:
    registry = NodeRegistry(studio(tmp_path, execute=False), base_dir=tmp_path)
    node = registry.get_node("openclaw")
    capabilities_before = tuple(node.capabilities)
    providers_before = dict(node.providers)
    policy_before = dict(node.policy)

    ingest_node_observations(
        registry,
        "openclaw",
        {
            "runtime.backends": backend_observation(),
            "runtime.models": model_observation(),
            "capabilities.advertised": {
                **capability_observation(),
                "node": {
                    "capabilities": [
                        "filesystem",
                        "process",
                        "inference",
                        "execute-anything",
                    ]
                },
            },
        },
        source="authenticated-device",
    )

    assert node.capabilities == capabilities_before
    assert node.providers == providers_before
    assert node.policy == policy_before

    denied = registry.authorize_session(
        {"metadata": {"machine_id": "openclaw"}},
        "execute",
    )
    assert denied.allowed is False
    assert denied.code == "MACHINE_PERMISSION_DENIED"


def test_local_refresh_uses_existing_inventory_without_rerouting_authority(
    tmp_path: Path,
) -> None:
    registry = NodeRegistry(studio(tmp_path), base_dir=tmp_path)
    registry.enrich_node(
        "openclaw",
        facet="runtime.backends",
        data=backend_observation(),
        source="local_probe",
        observed_at="2026-09-26T04:00:00+00:00",
    )
    registry.enrich_node(
        "openclaw",
        facet="runtime.models",
        data={
            **model_observation(),
            "node_id": "openclaw",
        },
        source="local_probe",
        observed_at="2026-09-26T04:00:01+00:00",
    )

    result = refresh_local_node_capabilities(
        registry,
        [
            {
                "id": "runtime-x",
                "model_capabilities": ["chat"],
            }
        ],
    )

    assert result is not None
    assert result["node_id"] == "openclaw"
    assert result["node"]["capabilities"] == ["filesystem", "process"]
    assert result["backends"][0]["capabilities"] == [
        "text-generation",
        "embeddings",
    ]
    assert result["models"][0]["capabilities"] == ["chat"]


def test_shared_runtime_emits_capability_advertisement(monkeypatch, tmp_path: Path) -> None:
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
                    "capabilities": ["text-generation"],
                    "model_capabilities": ["chat"],
                    "model_probe": {
                        "command": ["runtime-x", "models"],
                        "format": "lines",
                    },
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
            "observed_at": "2026-09-26T04:00:00+00:00",
        },
    )
    monkeypatch.setattr(
        runtime_module,
        "probe_configured_backends",
        lambda specs: backend_observation(),
    )
    monkeypatch.setattr(
        runtime_module,
        "probe_configured_models",
        lambda specs: model_observation(),
    )

    runtime = DeviceRuntime(config, identity=identity, backend=FakeBackend())
    observations = runtime.node_observations()

    advertised = observations["capabilities.advertised"]
    assert advertised["schema"] == NODE_CAPABILITIES_SCHEMA
    assert advertised["node"]["capabilities"] == ["filesystem", "process"]
    assert advertised["backends"][0]["backend_id"] == "runtime-x"
    assert advertised["models"][0] == {
        "backend_id": "runtime-x",
        "model_id": "model-a",
        "capabilities": ["chat"],
    }


def test_machine_agent_advertises_capabilities_with_existing_observations() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "scripts" / "hirda_machine_agent.py"
    ).read_text()

    assert "build_capability_advertisement" in source
    assert '"capabilities.advertised": capability_observation' in source


def test_n5_core_contains_no_product_or_provider_hardcoding() -> None:
    root = Path(__file__).resolve().parents[1]
    source = (
        (root / "mcp_studio" / "node_capabilities.py").read_text(encoding="utf-8")
        + (root / "mcp_studio" / "node_observations.py").read_text(encoding="utf-8")
    ).casefold()
    for forbidden in ("nova", "ollama", "openvino", "mlx"):
        assert forbidden not in source
