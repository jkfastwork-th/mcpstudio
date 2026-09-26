from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "certifications" / "hirda-lsp-cert-live.out"
OUT.parent.mkdir(parents=True, exist_ok=True)

with OUT.open("wb") as handle:
    subprocess.Popen(
        [
            "/bin/sh",
            "-c",
            "sleep 2; exec ./.venv/bin/python scripts/certify_language_runtime_recovery_live.py",
        ],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        close_fds=True,
    )

print(OUT)
