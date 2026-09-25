from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest


@pytest.mark.skipif(
    os.environ.get("HIRDA_RUN_LIVE_HANDOFF_CERT") != "1",
    reason="manual live HIRDA handoff certification",
)

def test_live_session_handoff_certification():
    script = Path(__file__).resolve().parents[1] / "scripts" / "certify-live-session-handoff.py"
    spec = importlib.util.spec_from_file_location("hirda_live_handoff_cert", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.main() == 0
