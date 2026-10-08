from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "mcp_studio" / "machine_registry.py"
spec = importlib.util.spec_from_file_location("maintenance_machine_registry", ROOT)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def test_machine_maintenance_permission_is_separate_and_off_by_default():
    default = module._normalize_machine_policy(None)
    assert default["maintenance"] is False
    assert default["destructive"] is False
    explicit = module._normalize_machine_policy({
        "maintenance": True,
        "destructive": False,
    })
    assert explicit["maintenance"] is True
    assert explicit["destructive"] is False


def test_machine_maintenance_permission_rejects_non_boolean_config():
    import pytest

    with pytest.raises(module.MachineRegistryError):
        module._normalize_machine_policy({"maintenance": "yes"})
