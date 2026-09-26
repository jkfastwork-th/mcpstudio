import json
from pathlib import Path
from types import SimpleNamespace

from mcp_studio.device_runtime import (
    DeviceIdentityStore,
    DeviceRuntime,
    DeviceRuntimeConfig,
)
from mcp_studio.node_backends import NODE_BACKENDS_SCHEMA
from mcp_studio.node_models import (
    NODE_MODELS_SCHEMA,
    normalize_model_observation,
    probe_configured_models,
    refresh_local_node_models,
)
from mcp_studio.node_observations import ingest_node_observations
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
    )


def backend_observation() -> dict:
    return {
        "schema": NODE_BACKENDS_SCHEMA,
        "observed_at": "2026-09-26T03:00:00+00:00",
        "backends": [
            {
                "id": "runtime-x",
                "kind": "inference",
                "available": True,
                "binary": "/usr/bin/runtime-x",
                "version": "runtime-x 1",
                "capabilities": [],
            }
        ],
    }


def model_observation(*, backend_id: str = "runtime-x") -> dict:
    return {
        "schema": NODE_MODELS_SCHEMA,
        "observed_at": "2026-09-26T03:00:01+00:00",
        "inventories": [
            {
                "backend_id": backend_id,
                "status": "ready",
                "models": [
                    {"id": "model-a", "name": "Model A"},
                    {"id": "model-b", "name": None},
                ],
            }
        ],
    }


def test_lines_model_probe_is_backend_scoped(monkeypatch) -> None:
    import mcp_studio.node_models as models

    monkeypatch.setattr(models.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        models.subprocess,
        "run",
        lambda argv, **kwargs: SimpleNamespace(
            returncode=0,
            stdout="model-a\nmodel-b\nmodel-a\n",
            stderr="",
        ),
    )

    result = probe_configured_models(
        [
            {
                "id": "runtime-x",
                "kind": "inference",
                "command": ["runtime-x"],
                "model_probe": {
                    "command": ["runtime-x", "models"],
                    "format": "lines",
                },
            }
        ],
        observed_at="2026-09-26T03:00:01+00:00",
    )

    assert result["schema"] == NODE_MODELS_SCHEMA
    assert result["inventories"] == [
        {
            "backend_id": "runtime-x",
            "status": "ready",
            "models": [
                {"id": "model-a", "name": None},
                {"id": "model-b", "name": None},
            ],
        }
    ]


def test_json_model_probe_supports_generic_path_and_fields(monkeypatch) -> None:
    import mcp_studio.node_models as models

    monkeypatch.setattr(models.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        models.subprocess,
        "run",
        lambda argv, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "payload": {
                        "entries": [
                            {"slug": "alpha", "label": "Alpha"},
                            {"slug": "beta", "label": "Beta"},
                        ]
                    }
                }
            ),
            stderr="",
        ),
    )

    result = probe_configured_models(
        [
            {
                "id": "runtime-x",
                "kind": "inference",
                "command": ["runtime-x"],
                "model_probe": {
                    "command": ["runtime-x", "catalog", "--json"],
                    "format": "json",
                    "json_path": ["payload", "entries"],
                    "id_field": "slug",
                    "name_field": "label",
                },
            }
        ]
    )

    models_out = result["inventories"][0]["models"]
    assert models_out == [
        {"id": "alpha", "name": "Alpha"},
        {"id": "beta", "name": "Beta"},
    ]


def test_model_probe_does_not_run_when_backend_is_unavailable(monkeypatch) -> None:
    import mcp_studio.node_models as models

    monkeypatch.setattr(models.shutil, "which", lambda name: None)

    result = probe_configured_models(
        [
            {
                "id": "runtime-x",
                "command": ["runtime-x"],
                "model_probe": {"command": ["runtime-x", "models"]},
            }
        ]
    )

    assert result["inventories"] == [
        {
            "backend_id": "runtime-x",
            "status": "backend_unavailable",
            "models": [],
        }
    ]


def test_model_normalizer_deduplicates_models_and_backends() -> None:
    raw = model_observation()
    raw["inventories"][0]["models"].append({"id": "model-a", "name": "duplicate"})
    raw["inventories"].append(
        {
            "backend_id": "runtime-x",
            "status": "ready",
            "models": [{"id": "other"}],
        }
    )

    result = normalize_model_observation(raw)

    assert result is not None
    assert len(result["inventories"]) == 1
    assert [item["id"] for item in result["inventories"][0]["models"]] == [
        "model-a",
        "model-b",
    ]


def test_ingestion_stamps_node_id_and_rejects_unknown_backend(tmp_path: Path) -> None:
    registry = NodeRegistry(studio(tmp_path), base_dir=tmp_path)

    accepted = ingest_node_observations(
        registry,
        "openclaw",
        {
            "runtime.backends": backend_observation(),
            "runtime.models": {
                "schema": NODE_MODELS_SCHEMA,
                "observed_at": "2026-09-26T03:00:01+00:00",
                "inventories": [
                    model_observation()["inventories"][0],
                    {
                        "backend_id": "forged-backend",
                        "status": "ready",
                        "models": [{"id": "forged-model"}],
                    },
                ],
            },
        },
        source="authenticated-device",
    )

    models = accepted["runtime.models"]
    assert models["node_id"] == "openclaw"
    assert [item["backend_id"] for item in models["inventories"]] == ["runtime-x"]
    stored = registry.lifecycle("openclaw")["enrichment"]["facets"]["runtime.models"]["data"]
    assert stored["node_id"] == "openclaw"
    assert stored["inventories"][0]["models"][0]["id"] == "model-a"


def test_local_model_refresh_requires_discovered_backend(monkeypatch, tmp_path: Path) -> None:
    import mcp_studio.node_models as models

    registry = NodeRegistry(studio(tmp_path), base_dir=tmp_path)
    registry.enrich_node(
        "openclaw",
        facet="runtime.backends",
        data=backend_observation(),
        source="local_probe",
        observed_at="2026-09-26T03:00:00+00:00",
    )
    monkeypatch.setattr(
        models,
        "probe_configured_models",
        lambda raw_specs: {
            "schema": NODE_MODELS_SCHEMA,
            "observed_at": "2026-09-26T03:00:01+00:00",
            "inventories": [
                model_observation()["inventories"][0],
                {
                    "backend_id": "not-discovered",
                    "status": "ready",
                    "models": [{"id": "bad"}],
                },
            ],
        },
    )

    result = refresh_local_node_models(registry, [])

    assert result is not None
    assert result["node_id"] == "openclaw"
    assert [item["backend_id"] for item in result["inventories"]] == ["runtime-x"]


def test_shared_runtime_emits_backend_and_model_observations(monkeypatch, tmp_path: Path) -> None:
    import mcp_studio.device_runtime as runtime_module

    config = DeviceRuntimeConfig.from_dict(
        {
            "control_plane_url": "https://hirda.example",
            "name": "device",
            "identity_path": ".hirda/key.pem",
            "desktop_commander_command": ["desktop-commander"],
            "workspaces": {},
            "capabilities": ["filesystem"],
            "backend_probes": [
                {
                    "id": "runtime-x",
                    "kind": "inference",
                    "command": ["runtime-x"],
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
        lambda storage_path=None: {"schema": "hirda-node-hardware-v1", "observed_at": "x"},
    )
    monkeypatch.setattr(runtime_module, "probe_configured_backends", lambda specs: backend_observation())
    monkeypatch.setattr(runtime_module, "probe_configured_models", lambda specs: model_observation())

    runtime = DeviceRuntime(config, identity=identity, backend=FakeBackend())
    observations = runtime.node_observations()

    assert config.backend_probes[0]["model_probe"]["format"] == "lines"
    assert observations["runtime.backends"]["schema"] == NODE_BACKENDS_SCHEMA
    assert observations["runtime.models"]["schema"] == NODE_MODELS_SCHEMA


def test_n4_core_contains_no_product_or_model_vendor_hardcoding() -> None:
    root = Path(__file__).resolve().parents[1]
    source = (
        (root / "mcp_studio" / "node_models.py").read_text(encoding="utf-8")
        + (root / "mcp_studio" / "node_observations.py").read_text(encoding="utf-8")
    ).casefold()
    for forbidden in ("nova", "ollama", "openvino", "mlx"):
        assert forbidden not in source
