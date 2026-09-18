from __future__ import annotations

import asyncio
import os
import re
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml


def _service_equivalent(actual: str, expected: str) -> bool:
    def norm(value: str) -> tuple[str, str, int, str]:
        p = urlparse(value)
        scheme = (p.scheme or "http").lower()
        host = (p.hostname or "").lower()
        port = p.port or (443 if scheme == "https" else 80)
        path = (p.path or "").rstrip("/")
        return scheme, host, port, path

    try:
        return norm(actual) == norm(expected)
    except Exception:
        return False


def _path_matches(pattern: str | None, path: str) -> bool:
    if pattern is None:
        return True
    try:
        return re.search(pattern, path) is not None
    except re.error:
        return False


def inspect_cloudflare_config(
    *,
    config_file: str,
    endpoint: str,
    origin: str,
    server_id: str,
) -> dict[str, Any]:
    """Statically validate that a cloudflared ingress config exposes only the MCP path.

    Cloudflare evaluates ingress rules top-to-bottom and forwards the full path to
    the origin, so a safe Studio config must have a hostname+path rule for the
    selected MCP route and terminate in a 404 catch-all. Any earlier broad rule
    that forwards the Studio origin is rejected.
    """

    path = Path(config_file).expanduser()
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    if not path.is_file():
        add("config_exists", False, str(path))
        return {"ok": False, "checks": checks, "config_file": str(path)}
    add("config_exists", True, str(path))

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        add("config_parse", False, str(exc))
        return {"ok": False, "checks": checks, "config_file": str(path)}
    add("config_parse", isinstance(raw, dict), "YAML object" if isinstance(raw, dict) else "not an object")
    if not isinstance(raw, dict):
        return {"ok": False, "checks": checks, "config_file": str(path)}

    tunnel_ref = raw.get("tunnel")
    add("named_tunnel", bool(tunnel_ref), f"tunnel={tunnel_ref!s}" if tunnel_ref else "missing tunnel")

    credentials = raw.get("credentials-file")
    if credentials:
        cred_path = Path(os.path.expandvars(os.path.expanduser(str(credentials))))
        add("credentials_file", cred_path.is_file(), str(cred_path))
    else:
        add("credentials_file", False, "missing credentials-file")

    ingress = raw.get("ingress")
    if not isinstance(ingress, list) or not ingress:
        add("ingress_present", False, "missing ingress list")
        return {"ok": False, "checks": checks, "config_file": str(path)}
    add("ingress_present", True, f"{len(ingress)} rules")

    last = ingress[-1] if isinstance(ingress[-1], dict) else {}
    catchall_ok = (
        not last.get("hostname")
        and not last.get("path")
        and str(last.get("service", "")).lower().startswith("http_status:404")
    )
    add("catch_all_404", catchall_ok, str(last))

    parsed_endpoint = urlparse(endpoint)
    expected_hostname = (parsed_endpoint.hostname or "").lower()
    expected_path = parsed_endpoint.path or f"/mcp/{server_id}"

    selected_rule: dict[str, Any] | None = None
    broad_origin_rules: list[int] = []
    dangerous_paths = ["/", "/api/status", "/static/app.js", "/api/workers"]

    for index, rule in enumerate(ingress[:-1]):
        if not isinstance(rule, dict):
            continue
        service = str(rule.get("service", ""))
        hostname = str(rule.get("hostname", "")).lower()
        path_pattern = rule.get("path")
        same_host = not hostname or hostname == expected_hostname
        routes_origin = _service_equivalent(service, origin)
        if same_host and routes_origin and _path_matches(path_pattern, expected_path):
            if selected_rule is None:
                selected_rule = rule
        if same_host and routes_origin and any(_path_matches(path_pattern, p) for p in dangerous_paths):
            broad_origin_rules.append(index)

    add(
        "mcp_path_rule",
        selected_rule is not None,
        f"hostname={expected_hostname} path={expected_path}" if selected_rule else "no matching hostname+path rule",
    )
    add(
        "studio_not_broadly_exposed",
        not broad_origin_rules,
        "no broad Studio origin rules" if not broad_origin_rules else f"dangerous ingress rule indexes: {broad_origin_rules}",
    )

    if selected_rule is not None:
        pattern = selected_rule.get("path")
        narrow = bool(pattern) and _path_matches(str(pattern), expected_path) and not any(
            _path_matches(str(pattern), p) for p in dangerous_paths
        )
        add("path_scope_narrow", narrow, f"path={pattern!s}")
        add(
            "origin_matches",
            _service_equivalent(str(selected_rule.get("service", "")), origin),
            f"service={selected_rule.get('service')} expected={origin}",
        )

    return {
        "ok": all(c["ok"] for c in checks),
        "checks": checks,
        "config_file": str(path),
        "endpoint": endpoint,
        "origin": origin,
        "server_id": server_id,
    }


async def run_cloudflared_ingress_checks(
    *, executable: str,
    config_file: str,
    endpoint: str,
    timeout: float = 10.0,
) -> dict[str, Any]:
    resolved = executable
    if os.path.sep not in executable:
        resolved = shutil.which(executable) or ""
    if not resolved or not Path(resolved).exists():
        return {
            "ok": False,
            "checks": [
                {"name": "cloudflared_executable", "ok": False, "detail": f"{executable} not found"}
            ],
        }

    checks: list[dict[str, Any]] = [
        {"name": "cloudflared_executable", "ok": True, "detail": resolved}
    ]

    commands = [
        ("ingress_validate", [resolved, "tunnel", "--config", config_file, "ingress", "validate"]),
        ("ingress_rule", [resolved, "tunnel", "--config", config_file, "ingress", "rule", endpoint]),
    ]
    for name, cmd in commands:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            raw, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            detail = raw.decode("utf-8", errors="replace").strip()[-4000:]
            checks.append({"name": name, "ok": proc.returncode == 0, "detail": detail})
        except Exception as exc:
            checks.append({"name": name, "ok": False, "detail": str(exc)})

    return {"ok": all(c["ok"] for c in checks), "checks": checks}
