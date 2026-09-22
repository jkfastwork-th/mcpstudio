from __future__ import annotations

from dataclasses import dataclass
from importlib import metadata
import inspect
from typing import Any, Awaitable, Protocol, runtime_checkable

from .action_adapter import ActionEnvelope, ActionEnvelopeError, normalize_action_envelope


ACTION_PROVIDER_REGISTRY_SCHEMA = "hirda-action-provider-registry-v1"
DEFAULT_ENTRYPOINT_GROUP = "hirda.action_providers"


class ActionProviderRegistryError(ValueError):
    pass


@runtime_checkable
class ActionProvider(Protocol):
    provider_id: str
    domain: str
    title: str
    description: str
    executor_attached: bool

    def build_envelope(
        self,
    ) -> ActionEnvelope | dict[str, Any] | Awaitable[ActionEnvelope | dict[str, Any]]:
        ...


@dataclass(frozen=True, slots=True)
class RegisteredActionProvider:
    provider_id: str
    domain: str
    title: str
    description: str
    source: str
    executor_attached: bool
    provider: ActionProvider

    def summary(self, *, action_count: int | None = None) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "domain": self.domain,
            "title": self.title,
            "description": self.description,
            "source": self.source,
            "executor_attached": self.executor_attached,
            "action_count": action_count,
        }


class ActionProviderRegistry:
    """Registry for bounded ActionEnvelope producers.

    JAA-2 deliberately registers envelope producers only. Executor attachment is
    rejected by default and provider output is normalized on every build.
    """

    def __init__(self, *, allow_executors: bool = False) -> None:
        self.allow_executors = bool(allow_executors)
        self._providers: dict[str, RegisteredActionProvider] = {}
        self._discovery_errors: list[dict[str, str]] = []

    @staticmethod
    def _clean(value: object, *, field: str) -> str:
        text = " ".join(str(value or "").split())
        if not text:
            raise ActionProviderRegistryError(f"{field} is required")
        return text

    def _validate_provider(
        self,
        provider: ActionProvider,
        *,
        source: str,
    ) -> RegisteredActionProvider:
        if not isinstance(provider, ActionProvider):
            raise ActionProviderRegistryError(
                "provider must implement ActionProvider contract"
            )

        provider_id = self._clean(provider.provider_id, field="provider_id")
        domain = self._clean(provider.domain, field="domain")
        title = self._clean(provider.title, field="title")
        description = " ".join(str(provider.description or "").split())
        executor_attached = bool(provider.executor_attached)

        if executor_attached and not self.allow_executors:
            raise ActionProviderRegistryError(
                f"provider {provider_id!r} declares executor_attached=true; "
                "JAA-2 registry is envelope-only"
            )

        return RegisteredActionProvider(
            provider_id=provider_id,
            domain=domain,
            title=title,
            description=description,
            source=self._clean(source, field="source"),
            executor_attached=executor_attached,
            provider=provider,
        )

    def register(
        self,
        provider: ActionProvider,
        *,
        source: str = "runtime",
        replace: bool = False,
    ) -> RegisteredActionProvider:
        registration = self._validate_provider(provider, source=source)
        existing = self._providers.get(registration.provider_id)
        if existing is not None and not replace:
            raise ActionProviderRegistryError(
                f"duplicate action provider: {registration.provider_id}"
            )
        self._providers[registration.provider_id] = registration
        return registration

    def unregister(self, provider_id: str) -> bool:
        return self._providers.pop(str(provider_id or "").strip(), None) is not None

    def get(self, provider_id: str) -> RegisteredActionProvider:
        normalized = str(provider_id or "").strip()
        registration = self._providers.get(normalized)
        if registration is None:
            raise KeyError(normalized)
        return registration

    async def build_envelope(self, provider_id: str) -> ActionEnvelope:
        registration = self.get(provider_id)
        try:
            raw = registration.provider.build_envelope()
            if inspect.isawaitable(raw):
                raw = await raw
            envelope = normalize_action_envelope(raw)
        except (ActionEnvelopeError, TypeError, ValueError) as exc:
            raise ActionProviderRegistryError(
                f"provider {registration.provider_id!r} returned invalid ActionEnvelope: {exc}"
            ) from exc
        if envelope.provider != registration.provider_id:
            raise ActionProviderRegistryError(
                f"provider {registration.provider_id!r} changed envelope.provider"
            )
        if envelope.domain != registration.domain:
            raise ActionProviderRegistryError(
                f"provider {registration.provider_id!r} changed envelope.domain"
            )
        if bool(envelope.metadata.get("executor_attached", False)) != registration.executor_attached:
            raise ActionProviderRegistryError(
                f"provider {registration.provider_id!r} changed executor_attached contract"
            )
        return envelope

    async def provider_detail(self, provider_id: str) -> dict[str, Any]:
        registration = self.get(provider_id)
        envelope = await self.build_envelope(provider_id)
        return {
            **registration.summary(action_count=len(envelope.actions)),
            "envelope": envelope.as_dict(),
        }

    async def snapshot(self) -> dict[str, Any]:
        providers: list[dict[str, Any]] = []
        for registration in self._providers.values():
            try:
                envelope = await self.build_envelope(registration.provider_id)
                action_count: int | None = len(envelope.actions)
                status = "ready"
                error = None
            except Exception as exc:
                action_count = None
                status = "invalid"
                error = type(exc).__name__
            row = registration.summary(action_count=action_count)
            row["status"] = status
            row["error"] = error
            providers.append(row)
        return {
            "schema": ACTION_PROVIDER_REGISTRY_SCHEMA,
            "providers": providers,
            "provider_count": len(providers),
            "entrypoint_group": DEFAULT_ENTRYPOINT_GROUP,
            "discovery_errors": list(self._discovery_errors),
            "allow_executors": self.allow_executors,
            "executor_attached": False,
        }

    def discover_entry_points(
        self,
        *,
        group: str = DEFAULT_ENTRYPOINT_GROUP,
        strict: bool = False,
    ) -> dict[str, Any]:
        """Load opt-in external providers published as Python entry points.

        Entry-point loading executes installed Python package code, so callers
        must explicitly enable discovery in HIRDA configuration.
        """

        loaded_ids: list[str] = []
        errors: list[dict[str, str]] = []
        try:
            discovered = metadata.entry_points()
            candidates = (
                discovered.select(group=group)
                if hasattr(discovered, "select")
                else discovered.get(group, [])
            )
        except Exception as exc:
            if strict:
                raise ActionProviderRegistryError(
                    f"failed to enumerate action provider entry points: {exc}"
                ) from exc
            errors.append(
                {
                    "entry_point": group,
                    "error": type(exc).__name__,
                }
            )
            self._discovery_errors.extend(errors)
            return {"loaded": loaded_ids, "errors": errors}

        for entry_point in candidates:
            name = str(getattr(entry_point, "name", "unknown"))
            try:
                loaded = entry_point.load()
                provider = loaded() if isinstance(loaded, type) else loaded
                if callable(provider) and not isinstance(provider, ActionProvider):
                    provider = provider()
                registration = self.register(
                    provider,
                    source=f"entrypoint:{name}",
                )
                loaded_ids.append(registration.provider_id)
            except Exception as exc:
                if strict:
                    raise ActionProviderRegistryError(
                        f"failed to load action provider entry point {name!r}: {exc}"
                    ) from exc
                errors.append(
                    {
                        "entry_point": name,
                        "error": type(exc).__name__,
                    }
                )

        self._discovery_errors.extend(errors)
        return {"loaded": loaded_ids, "errors": errors}


def build_action_provider_registry(studio: Any) -> ActionProviderRegistry:
    from .action_test_providers import all_test_providers

    registry = ActionProviderRegistry(allow_executors=False)
    for provider in all_test_providers():
        registry.register(provider, source="builtin-test")

    if bool(getattr(studio, "action_provider_entrypoints_enabled", False)):
        group = str(
            getattr(
                studio,
                "action_provider_entrypoint_group",
                DEFAULT_ENTRYPOINT_GROUP,
            )
            or DEFAULT_ENTRYPOINT_GROUP
        ).strip()
        registry.discover_entry_points(group=group, strict=False)
    return registry
