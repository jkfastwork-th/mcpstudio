from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import yaml


class AgentRuntimeInventory:
    """Lightweight host/runtime discovery for the three first-class agent lanes."""

    SPECS = (
        ("claude", "Claude", "Anthropic"),
        ("codex", "Codex", "OpenAI"),
        ("hermes", "Hermes", "Nous / custom"),
    )

    def __init__(self, herdr: Any, ttl_seconds: float = 30.0):
        self.herdr = herdr
        self.ttl_seconds = ttl_seconds
        self._cached_at = 0.0
        self._cached: dict[str, Any] | None = None

    @staticmethod
    def _find_binary(name: str) -> str | None:
        found = shutil.which(name)
        if found:
            return found
        home = Path.home()
        for candidate in (
            home / ".local" / "bin" / name,
            home / ".npm-global" / "bin" / name,
            home / ".bun" / "bin" / name,
            home / "bin" / name,
        ):
            if candidate.is_file():
                return str(candidate)
        return None

    @staticmethod
    def _version(binary: str | None) -> str | None:
        if not binary:
            return None
        try:
            proc = subprocess.run(
                [binary, "--version"],
                capture_output=True,
                text=True,
                timeout=2.0,
                check=False,
            )
        except Exception:
            return None
        text = (proc.stdout or proc.stderr or "").strip().splitlines()
        return text[0][:240] if text else None

    @staticmethod
    def _pane_value(panes: list[dict[str, Any]], *keys: str) -> str | None:
        for pane in panes:
            for key in keys:
                value = pane.get(key)
                if value not in (None, ""):
                    return str(value)
        return None

    @staticmethod
    def _hermes_config() -> dict[str, Any]:
        path = Path.home() / ".hermes" / "config.yaml"
        if not path.is_file():
            return {}
        try:
            data = yaml.safe_load(path.read_text()) or {}
        except Exception:
            return {}
        if not isinstance(data, dict):
            return {}
        model_cfg = data.get("model") or {}
        if not isinstance(model_cfg, dict):
            return {}
        return {
            "provider": model_cfg.get("provider"),
            "model": (
                model_cfg.get("model")
                or model_cfg.get("name")
                or model_cfg.get("default_model")
            ),
        }

    @staticmethod
    def _hermes_last_used_model() -> str | None:
        root = Path.home() / ".hermes" / "profiles"
        if not root.is_dir():
            return None
        dumps = sorted(
            root.glob("*/sessions/request_dump_*.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for path in dumps[:20]:
            try:
                import json
                payload = json.loads(path.read_text())
            except Exception:
                continue
            request = payload.get("request") if isinstance(payload, dict) else None
            body = request.get("body") if isinstance(request, dict) else None
            model = body.get("model") if isinstance(body, dict) else None
            if isinstance(model, str) and model.strip():
                return model.strip()
        return None

    @staticmethod
    def _status(installed: bool, panes: list[dict[str, Any]]) -> str:
        if not installed:
            return "unavailable"
        states = [str(p.get("agent_status") or "").lower() for p in panes]
        if any(s in {"working", "busy", "running"} for s in states):
            return "running"
        if any(s in {"blocked", "error", "failed"} for s in states):
            return "blocked"
        if panes:
            return "ready"
        return "available"

    def _build(self) -> dict[str, Any]:
        snapshot = self.herdr.snapshot if isinstance(self.herdr.snapshot, dict) else {}
        pane_list = self.herdr.pane_list(snapshot)
        runtimes: list[dict[str, Any]] = []
        hermes_cfg = self._hermes_config()

        for key, label, default_provider in self.SPECS:
            binary = self._find_binary(key)
            panes = [
                p for p in pane_list
                if str(p.get("agent") or "").lower() == key
            ]
            provider = self._pane_value(panes, "provider", "model_provider")
            model = self._pane_value(panes, "model", "model_name")
            if key == "hermes":
                provider = provider or hermes_cfg.get("provider")
                model = model or hermes_cfg.get("model")

            last_used_model = self._hermes_last_used_model() if key == "hermes" else None
            observed = bool(panes)
            installed = bool(binary or observed)
            effective_model = model or last_used_model
            free = bool(effective_model and ":free" in effective_model.lower())
            item: dict[str, Any] = {
                "id": key,
                "name": label,
                "brand": default_provider,
                "installed": installed,
                "launchable": bool(binary),
                "observed": observed,
                "binary": binary,
                "version": self._version(binary),
                "status": self._status(installed, panes),
                "pane_count": len(panes),
                "active_panes": [
                    {
                        "pane_id": p.get("pane_id"),
                        "workspace_id": p.get("workspace_id"),
                        "cwd": p.get("cwd"),
                        "agent_status": p.get("agent_status"),
                        "model": p.get("model") or p.get("model_name"),
                        "provider": p.get("provider") or p.get("model_provider"),
                    }
                    for p in panes[:10]
                ],
                "provider": provider or default_provider,
                "model": model,
                "last_used_model": last_used_model,
                "free": free,
            }
            if key == "hermes":
                item["nous_free"] = {
                    "portal": "Nous Portal",
                    "preferred_model": "upstage/solar-pro4:free",
                    "active": bool(
                        effective_model
                        and effective_model.lower() == "upstage/solar-pro4:free"
                    ),
                    "last_used_model": last_used_model,
                }
            runtimes.append(item)

        return {
            "runtimes": runtimes,
            "summary": {
                "installed": sum(1 for r in runtimes if r["installed"]),
                "launchable": sum(1 for r in runtimes if r["launchable"]),
                "observed": sum(1 for r in runtimes if r["observed"]),
                "running": sum(1 for r in runtimes if r["status"] == "running"),
                "ready": sum(1 for r in runtimes if r["status"] in {"ready", "available", "running"}),
                "free_active": sum(1 for r in runtimes if r.get("free")),
            },
            "herdr_status": snapshot.get("status"),
            "refreshed_at_monotonic": time.monotonic(),
        }

    def snapshot(self, *, force: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        if (
            not force
            and self._cached is not None
            and now - self._cached_at < self.ttl_seconds
        ):
            return self._cached
        self._cached = self._build()
        self._cached_at = now
        return self._cached
