from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ContextProfile:
    source_tokens: int
    retained_tokens: int

    def __post_init__(self) -> None:
        if self.source_tokens <= 0:
            raise ValueError("source_tokens must be greater than zero")
        if self.retained_tokens <= 0:
            raise ValueError("retained_tokens must be greater than zero")
        if self.retained_tokens > self.source_tokens:
            raise ValueError("retained_tokens cannot exceed source_tokens")

    @property
    def retained_percent(self) -> float:
        return round((self.retained_tokens / self.source_tokens) * 100.0, 1)

    @property
    def capsule_type(self) -> str:
        pct = self.retained_percent
        if self.retained_tokens == self.source_tokens:
            return "full"
        if pct >= 40.0:
            return "compact"
        return "minimal"

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_tokens": self.source_tokens,
            "retained_tokens": self.retained_tokens,
            "retained_percent": self.retained_percent,
            "capsule_type": self.capsule_type,
        }


@dataclass(frozen=True)
class ModelCapability:
    provider: str
    model_id: str
    context_window: int
    max_output_tokens: int
    system_prompt_tokens: int = 0
    tool_schema_tokens: int = 0
    safety_reserve_tokens: int = 4096
    last_verified_at: str | None = None

    def __post_init__(self) -> None:
        if not self.provider.strip():
            raise ValueError("provider is required")
        if not self.model_id.strip():
            raise ValueError("model_id is required")
        if self.context_window <= 0:
            raise ValueError("context_window must be greater than zero")
        for name in (
            "max_output_tokens",
            "system_prompt_tokens",
            "tool_schema_tokens",
            "safety_reserve_tokens",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} cannot be negative")

    @property
    def overhead_tokens(self) -> int:
        return (
            self.max_output_tokens
            + self.system_prompt_tokens
            + self.tool_schema_tokens
            + self.safety_reserve_tokens
        )

    @property
    def usable_context_tokens(self) -> int:
        return max(0, self.context_window - self.overhead_tokens)

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model_id": self.model_id,
            "context_window": self.context_window,
            "max_output_tokens": self.max_output_tokens,
            "system_prompt_tokens": self.system_prompt_tokens,
            "tool_schema_tokens": self.tool_schema_tokens,
            "safety_reserve_tokens": self.safety_reserve_tokens,
            "overhead_tokens": self.overhead_tokens,
            "usable_context_tokens": self.usable_context_tokens,
            "last_verified_at": self.last_verified_at,
        }


def evaluate_context_fit(
    profile: ContextProfile,
    capability: ModelCapability,
) -> dict[str, Any]:
    usable = capability.usable_context_tokens
    required = profile.retained_tokens
    fit = usable > 0 and required <= usable
    utilization = 100.0 if usable <= 0 else min(999.9, round((required / usable) * 100.0, 1))
    remaining = usable - required
    if not fit:
        fit_state = "blocked"
    elif utilization >= 90:
        fit_state = "tight"
    elif utilization >= 75:
        fit_state = "watch"
    else:
        fit_state = "safe"
    return {
        "fit": fit,
        "fit_state": fit_state,
        "required_tokens": required,
        "usable_context_tokens": usable,
        "remaining_tokens": remaining,
        "utilization_percent": utilization,
        "capsule": profile.as_dict(),
        "capability": capability.as_dict(),
    }
