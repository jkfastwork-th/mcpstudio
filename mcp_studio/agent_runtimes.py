from __future__ import annotations

import json
import os
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

    AUTH_ENV = {
        "claude": ("ANTHROPIC_API_KEY",),
        "codex": ("OPENAI_API_KEY",),
        "hermes": ("NOUS_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"),
    }
    AUTH_FILES = {
        "claude": (".claude/.credentials.json",),
        "codex": (".codex/auth.json",),
        "hermes": (),
    }
    AUTH_STATUS_KEYS = (
        "auth_status",
        "authentication_status",
        "login_status",
        "credential_status",
    )
    AUTH_BOOL_KEYS = ("authenticated", "is_authenticated", "logged_in")
    LIMIT_STATUS_KEYS = (
        "rate_limit_status",
        "quota_status",
        "limit_status",
        "rate_limit_state",
        "quota_state",
    )
    LIMIT_REMAINING_KEYS = (
        "rate_limit_remaining",
        "quota_remaining",
        "remaining_quota",
        "remaining_requests",
    )
    LIMIT_RESET_KEYS = (
        "rate_limit_reset_at",
        "quota_reset_at",
        "reset_at",
        "rate_limit_reset",
    )
    ERROR_KEYS = (
        "last_error",
        "error",
        "error_message",
        "status_message",
        "message",
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
    def _run_probe(binary: str | None, args: list[str], timeout: float = 4.0) -> dict[str, Any] | None:
        if not binary:
            return None
        try:
            proc = subprocess.run(
                [binary, *args],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except Exception:
            return None
        return {
            "returncode": int(proc.returncode),
            "stdout": (proc.stdout or "").strip()[:8000],
            "stderr": (proc.stderr or "").strip()[:8000],
        }

    @classmethod
    def _version(cls, binary: str | None) -> str | None:
        probe = cls._run_probe(binary, ["--version"], timeout=2.0)
        if not probe:
            return None
        text = (probe["stdout"] or probe["stderr"]).strip().splitlines()
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


    @classmethod
    def _pane_bool(cls, panes: list[dict[str, Any]], *keys: str) -> bool | None:
        for pane in panes:
            for key in keys:
                if key not in pane:
                    continue
                value = pane.get(key)
                if isinstance(value, bool):
                    return value
                text = str(value or "").strip().lower()
                if text in {"1", "true", "yes", "authenticated", "logged_in"}:
                    return True
                if text in {"0", "false", "no", "unauthenticated", "logged_out"}:
                    return False
        return None

    @classmethod
    def _pane_error_text(cls, panes: list[dict[str, Any]]) -> str:
        parts: list[str] = []
        for pane in panes:
            for key in cls.ERROR_KEYS:
                value = pane.get(key)
                if value not in (None, ""):
                    parts.append(str(value))
        return " | ".join(parts).lower()

    @classmethod
    def _credential_marker(cls, key: str) -> dict[str, Any] | None:
        for env_name in cls.AUTH_ENV.get(key, ()):
            if os.environ.get(env_name):
                return {
                    "source": "environment",
                    "detail": "Credential environment is configured.",
                }
        home = Path.home()
        for relative in cls.AUTH_FILES.get(key, ()):
            if (home / relative).is_file():
                return {
                    "source": "credential_file",
                    "detail": "Credential file is present.",
                }
        return None

    @classmethod
    def _cli_auth_health(
        cls,
        key: str,
        *,
        binary: str | None,
        provider: str | None,
    ) -> dict[str, Any] | None:
        if not binary:
            return None

        if key == "claude":
            probe = cls._run_probe(binary, ["auth", "status", "--json"])
            if not probe:
                return None
            raw = probe["stdout"] or probe["stderr"]
            try:
                payload = json.loads(raw) if raw else {}
            except Exception:
                payload = {}
            logged_in = payload.get("loggedIn") if isinstance(payload, dict) else None
            if logged_in is True:
                method = str(payload.get("authMethod") or "").strip()
                subscription = str(payload.get("subscriptionType") or "").strip()
                suffix = " · ".join(x for x in (method, subscription) if x)
                return {
                    "status": "logged_in",
                    "authenticated": True,
                    "source": "claude_auth_status",
                    "detail": "Claude Code reports logged in." + (f" {suffix}" if suffix else ""),
                }
            if logged_in is False:
                return {
                    "status": "error",
                    "authenticated": False,
                    "source": "claude_auth_status",
                    "detail": "Claude Code reports not logged in.",
                }
            lower = raw.lower()
            if "logged in" in lower and "not logged in" not in lower:
                return {
                    "status": "logged_in",
                    "authenticated": True,
                    "source": "claude_auth_status",
                    "detail": "Claude Code reports logged in.",
                }
            if "not logged in" in lower or probe["returncode"] != 0:
                return {
                    "status": "error",
                    "authenticated": False,
                    "source": "claude_auth_status",
                    "detail": "Claude Code authentication check failed.",
                }
            return None

        if key == "codex":
            probe = cls._run_probe(binary, ["login", "status"])
            if not probe:
                return None
            raw = f'{probe["stdout"]}\n{probe["stderr"]}'.strip()
            lower = raw.lower()
            if "logged in using" in lower:
                mode = raw.split("Logged in using", 1)[-1].strip().splitlines()[0][:120]
                return {
                    "status": "logged_in",
                    "authenticated": True,
                    "source": "codex_login_status",
                    "detail": f"Codex reports logged in using {mode}.",
                }
            if "not logged in" in lower or probe["returncode"] != 0:
                return {
                    "status": "error",
                    "authenticated": False,
                    "source": "codex_login_status",
                    "detail": "Codex reports not logged in.",
                }
            return None

        if key == "hermes" and provider:
            probe = cls._run_probe(binary, ["auth", "status", str(provider)])
            if not probe:
                return None
            raw = f'{probe["stdout"]}\n{probe["stderr"]}'.strip()
            lower = raw.lower()
            if "logged in" in lower and "logged out" not in lower:
                return {
                    "status": "logged_in",
                    "authenticated": True,
                    "source": "hermes_auth_status",
                    "detail": f"Hermes reports {provider} logged in.",
                }
            if "logged out" in lower:
                return {
                    "status": "error",
                    "authenticated": False,
                    "source": "hermes_auth_status",
                    "detail": f"Hermes reports {provider} logged out.",
                }
            return None

        return None

    @classmethod
    def _auth_health(
        cls,
        key: str,
        *,
        binary: str | None,
        provider: str | None,
        installed: bool,
        runtime_status: str,
        panes: list[dict[str, Any]],
    ) -> dict[str, Any]:
        cli_health = cls._cli_auth_health(key, binary=binary, provider=provider)
        if cli_health is not None:
            return cli_health

        explicit = cls._pane_value(panes, *cls.AUTH_STATUS_KEYS)
        authenticated = cls._pane_bool(panes, *cls.AUTH_BOOL_KEYS)
        error_text = cls._pane_error_text(panes)

        if explicit:
            normalized = explicit.strip().lower()
            if normalized in {"ok", "healthy", "authenticated", "logged_in", "ready", "valid"}:
                return {
                    "status": "logged_in",
                    "authenticated": True,
                    "source": "pane_metadata",
                    "detail": "Runtime metadata reports authenticated.",
                }
            if normalized in {
                "error", "failed", "invalid", "unauthenticated", "logged_out",
                "expired", "missing", "login_required",
            }:
                return {
                    "status": "error",
                    "authenticated": False,
                    "source": "pane_metadata",
                    "detail": f"Runtime reports authentication state: {explicit}.",
                }

        if authenticated is True:
            return {
                "status": "healthy",
                "authenticated": True,
                "source": "pane_metadata",
                "detail": "Runtime reports authenticated.",
            }
        if authenticated is False:
            return {
                "status": "error",
                "authenticated": False,
                "source": "pane_metadata",
                "detail": "Runtime reports unauthenticated.",
            }

        auth_error_signals = (
            "401",
            "authentication failed",
            "authentication error",
            "unauthorized",
            "invalid api key",
            "invalid_api_key",
            "login required",
            "not logged in",
            "credential expired",
            "token expired",
        )
        if any(signal in error_text for signal in auth_error_signals):
            return {
                "status": "error",
                "authenticated": False,
                "source": "runtime_error",
                "detail": "Authentication failure observed in runtime status.",
            }

        marker = cls._credential_marker(key)
        if marker:
            return {
                "status": "configured",
                "authenticated": None,
                **marker,
            }

        if not installed:
            return {
                "status": "unavailable",
                "authenticated": None,
                "source": "runtime",
                "detail": "Runtime is not installed or observed.",
            }

        return {
            "status": "unknown",
            "authenticated": None,
            "source": "none",
            "detail": "No authentication health signal is available.",
        }

    @classmethod
    def _limit_health(
        cls,
        key: str,
        *,
        installed: bool,
        runtime_status: str,
        panes: list[dict[str, Any]],
    ) -> dict[str, Any]:
        explicit = cls._pane_value(panes, *cls.LIMIT_STATUS_KEYS)
        remaining = cls._pane_value(panes, *cls.LIMIT_REMAINING_KEYS)
        reset_at = cls._pane_value(panes, *cls.LIMIT_RESET_KEYS)
        rate_limited = cls._pane_bool(panes, "rate_limited")
        quota_exhausted = cls._pane_bool(panes, "quota_exhausted")
        error_text = cls._pane_error_text(panes)

        status = None
        detail = None
        source = "none"

        if explicit:
            normalized = explicit.strip().lower()
            source = "pane_metadata"
            if normalized in {"ok", "healthy", "normal", "available", "clear"}:
                status = "healthy"
                detail = "Runtime reports no active quota or rate-limit issue."
            elif normalized in {"exhausted", "quota_exhausted", "insufficient_quota"}:
                status = "exhausted"
                detail = f"Runtime reports limit state: {explicit}."
            elif normalized in {"limited", "rate_limited", "throttled", "blocked", "429"}:
                status = "limited"
                detail = f"Runtime reports limit state: {explicit}."
            else:
                status = "reported"
                detail = f"Runtime reports limit state: {explicit}."

        if quota_exhausted is True:
            status = "exhausted"
            source = "pane_metadata"
            detail = "Runtime reports exhausted quota."
        elif rate_limited is True:
            status = "limited"
            source = "pane_metadata"
            detail = "Runtime reports an active rate limit."

        exhausted_signals = (
            "insufficient_quota",
            "quota exceeded",
            "quota exhausted",
            "usage limit reached",
            "credit balance",
        )
        limited_signals = (
            "rate limit",
            "rate_limit",
            "too many requests",
            "429",
            "throttl",
        )
        if any(signal in error_text for signal in exhausted_signals):
            status = "exhausted"
            source = "runtime_error"
            detail = "Quota exhaustion signal observed in runtime status."
        elif any(signal in error_text for signal in limited_signals):
            status = "limited"
            source = "runtime_error"
            detail = "Rate-limit signal observed in runtime status."

        if status is None and remaining is not None:
            status = "reported"
            source = "pane_metadata"
            detail = "Runtime reports remaining quota or request capacity."

        if status is None and not installed:
            status = "unavailable"
            source = "runtime"
            detail = "Runtime is not installed or observed."

        if status is None:
            status = "interactive_only"
            source = "cli_capability"
            if key == "claude":
                detail = "Claude Code exposes plan usage interactively; no stable headless quota probe is available."
            elif key == "codex":
                detail = "Codex login status does not expose quota; limits are available in the interactive client."
            else:
                detail = "No stable headless quota probe is available for the active Hermes provider."

        return {
            "status": status,
            "remaining": remaining,
            "reset_at": reset_at,
            "source": source,
            "detail": detail,
        }

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
            runtime_status = self._status(installed, panes)
            effective_model = model or last_used_model
            free = bool(effective_model and ":free" in effective_model.lower())
            auth_health = self._auth_health(
                key,
                binary=binary,
                provider=provider or default_provider,
                installed=installed,
                runtime_status=runtime_status,
                panes=panes,
            )
            limit_health = self._limit_health(
                key,
                installed=installed,
                runtime_status=runtime_status,
                panes=panes,
            )
            last_error = self._pane_value(panes, *self.ERROR_KEYS)
            item: dict[str, Any] = {
                "id": key,
                "name": label,
                "brand": default_provider,
                "installed": installed,
                "launchable": bool(binary),
                "observed": observed,
                "binary": binary,
                "version": self._version(binary),
                "status": runtime_status,
                "auth_health": auth_health,
                "limit_health": limit_health,
                "last_error": last_error,
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
                "auth_healthy": sum(
                    1 for r in runtimes
                    if (r.get("auth_health") or {}).get("status") in {"logged_in", "healthy"}
                ),
                "auth_verified": sum(
                    1 for r in runtimes
                    if (r.get("auth_health") or {}).get("status") == "logged_in"
                ),
                "auth_configured": sum(
                    1 for r in runtimes
                    if (r.get("auth_health") or {}).get("status") in {"logged_in", "healthy", "configured"}
                ),
                "limit_constrained": sum(
                    1 for r in runtimes
                    if (r.get("limit_health") or {}).get("status") in {"limited", "exhausted"}
                ),
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
