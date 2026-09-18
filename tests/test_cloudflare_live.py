from __future__ import annotations

import os
from pathlib import Path

import pytest

from mcp_studio.cloudflare_live import inspect_cloudflare_config, run_cloudflared_ingress_checks
from mcp_studio.settings import load_settings


def _write_config(tmp_path: Path, *, broad: bool = False) -> tuple[Path, Path]:
    cred = tmp_path / "tunnel.json"
    cred.write_text("{}", encoding="utf-8")
    cfg = tmp_path / "config.yml"
    if broad:
        ingress = f"""
ingress:
  - hostname: serena.example.com
    service: http://127.0.0.1:8100
  - service: http_status:404
"""
    else:
        ingress = f"""
ingress:
  - hostname: serena.example.com
    path: ^/mcp/serena-8001/?$
    service: http://127.0.0.1:8100
  - service: http_status:404
"""
    cfg.write_text(
        f"tunnel: 00000000-0000-0000-0000-000000000001\ncredentials-file: {cred}\n{ingress}",
        encoding="utf-8",
    )
    return cfg, cred


def test_path_scoped_cloudflare_config_passes(tmp_path: Path):
    cfg, _ = _write_config(tmp_path)
    result = inspect_cloudflare_config(
        config_file=str(cfg),
        endpoint="https://serena.example.com/mcp/serena-8001",
        origin="http://127.0.0.1:8100",
        server_id="serena-8001",
    )
    assert result["ok"] is True
    assert all(item["ok"] for item in result["checks"])


def test_broad_cloudflare_config_is_rejected(tmp_path: Path):
    cfg, _ = _write_config(tmp_path, broad=True)
    result = inspect_cloudflare_config(
        config_file=str(cfg),
        endpoint="https://serena.example.com/mcp/serena-8001",
        origin="http://127.0.0.1:8100",
        server_id="serena-8001",
    )
    assert result["ok"] is False
    failures = {item["name"] for item in result["checks"] if not item["ok"]}
    assert "studio_not_broadly_exposed" in failures
    assert "path_scope_narrow" in failures


@pytest.mark.asyncio
async def test_cloudflared_ingress_cli_checks(tmp_path: Path):
    fake = tmp_path / "cloudflared"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        "set -e\n"
        "if [[ \"$*\" == *\"ingress validate\"* ]]; then echo valid; exit 0; fi\n"
        "if [[ \"$*\" == *\"ingress rule\"* ]]; then echo 'Matched rule #0'; exit 0; fi\n"
        "exit 2\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    cfg, _ = _write_config(tmp_path)
    result = await run_cloudflared_ingress_checks(
        executable=str(fake),
        config_file=str(cfg),
        endpoint="https://serena.example.com/mcp/serena-8001",
        timeout=2,
    )
    assert result["ok"] is True
    assert {x["name"] for x in result["checks"]} >= {
        "cloudflared_executable",
        "ingress_validate",
        "ingress_rule",
    }


def test_public_gateway_requires_bearer_when_enabled(tmp_path: Path):
    cfg = tmp_path / "config.yml"
    cfg.write_text(
        """
studio:
  gateway_enabled: true
  gateway_auth_mode: none
servers:
  - id: serena-8001
    name: Serena
    url: http://127.0.0.1:8001/mcp
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="requires studio.gateway_auth_mode=bearer"):
        load_settings(cfg)
