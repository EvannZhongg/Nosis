"""Provider-specific reasoning parameter normalization.

The public setting is a semantic effort level.  This module translates that
intent into the wire parameters understood by the selected model route.  It is
kept independent from LiteLLM so native provider adapters can consume the same
result when they are introduced.
"""

from dataclasses import dataclass
from typing import Mapping


SEMANTIC_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "max"})
_CUSTOM_PARAMETER_NAMES = frozenset(
    {"reasoning_effort", "thinking", "thinking_level", "reasoning", "extra_body"}
)


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    prefixes: tuple[str, ...] = ()
    reasoning_effort_remap: Mapping[str, str] | None = None
    thinking_style: str | None = None
    gateway_reasoning_style: bool = False


_MISTRAL_EFFORTS = {
    "none": "none",
    "minimal": "none",
    "low": "none",
    "medium": "high",
    "high": "high",
    "max": "high",
}

_DASHSCOPE_EFFORTS = {"minimal": "minimum"}

PROVIDER_SPECS: tuple[ProviderSpec, ...] = (
    ProviderSpec("openrouter", ("openrouter/",), gateway_reasoning_style=True),
    ProviderSpec("anthropic", ("anthropic/",), thinking_style="anthropic"),
    ProviderSpec("bedrock", ("bedrock/",), thinking_style="bedrock_adaptive"),
    ProviderSpec("mistral", ("mistral/",), reasoning_effort_remap=_MISTRAL_EFFORTS),
    ProviderSpec("dashscope", ("dashscope/",), reasoning_effort_remap=_DASHSCOPE_EFFORTS),
    ProviderSpec("xai", ("xai/",), thinking_style="xai"),
    ProviderSpec("moonshot", ("moonshot/",), thinking_style="moonshot"),
    ProviderSpec("deepseek", ("deepseek/",), thinking_style="deepseek"),
    ProviderSpec("openai", ("openai/",), thinking_style="openai"),
    ProviderSpec("azure", ("azure/",), thinking_style="openai"),
    ProviderSpec("gemini", ("gemini/",), thinking_style="gemini"),
    ProviderSpec("vertex_ai", ("vertex_ai/",), thinking_style="gemini"),
)


def normalize_semantic_effort(value: str) -> str:
    """Normalize the small public effort vocabulary."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("reasoning_effort must be a non-empty string")
    normalized = value.strip().lower()
    if normalized == "minimum":
        normalized = "minimal"
    if normalized not in SEMANTIC_EFFORTS:
        allowed = ", ".join(sorted(SEMANTIC_EFFORTS))
        raise ValueError(
            f"reasoning_effort must be one of {allowed}; got {value!r}"
        )
    return normalized


def provider_spec(model: str) -> ProviderSpec | None:
    lowered = model.strip().lower()
    for spec in PROVIDER_SPECS:
        if lowered.startswith(spec.prefixes):
            return spec
    return None


def reasoning_arguments(
    model: str,
    effort: str | None,
    custom_parameters: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Return LiteLLM/native-adapter-neutral request parameters.

    ``custom_parameters`` is intentionally an escape hatch for self-hosted
    OpenAI-compatible services.  An omitted effort leaves the request
    untouched, preserving the upstream model default.
    """
    arguments = dict(custom_parameters or {})
    unknown = set(arguments) - _CUSTOM_PARAMETER_NAMES
    if unknown:
        names = ", ".join(sorted(unknown))
        raise ValueError(
            "reasoning_parameters contains unsupported field(s): " + names
        )
    if effort is None:
        return arguments
    semantic_effort = normalize_semantic_effort(effort)
    spec = provider_spec(model)
    mapped = (
        spec.reasoning_effort_remap.get(semantic_effort, semantic_effort)
        if spec is not None and spec.reasoning_effort_remap is not None
        else semantic_effort
    )

    if spec is None or spec.thinking_style in {"openai", "deepseek"}:
        arguments.setdefault("reasoning_effort", mapped)
    elif spec.gateway_reasoning_style:
        arguments.setdefault("extra_body", {})
        extra_body = arguments["extra_body"]
        if not isinstance(extra_body, dict):
            raise ValueError("reasoning_parameters.extra_body must be an object")
        extra_body.setdefault("reasoning", {})
        reasoning = extra_body["reasoning"]
        if not isinstance(reasoning, dict):
            raise ValueError("reasoning_parameters.extra_body.reasoning must be an object")
        reasoning.setdefault("effort", mapped)
    elif spec.thinking_style == "anthropic":
        arguments.setdefault(
            "thinking",
            {"type": "disabled"}
            if semantic_effort == "none"
            else {
                "type": "enabled",
                "budget_tokens": _anthropic_budget(semantic_effort),
            },
        )
    elif spec.thinking_style == "bedrock_adaptive":
        arguments.setdefault(
            "extra_body",
            {
                "additionalModelRequestFields": {
                    "thinking": {"type": "adaptive", "effort": mapped},
                },
            },
        )
    elif spec.thinking_style == "xai":
        arguments.setdefault(
            "extra_body",
            {"reasoning": {"summary": "concise", "effort": mapped}},
        )
    elif spec.thinking_style == "gemini":
        arguments.setdefault("thinking_level", mapped)
    elif spec.thinking_style == "moonshot":
        model_name = model.lower().rsplit("/", 1)[-1]
        if "k3" in model_name:
            arguments.setdefault("reasoning_effort", "max")
        else:
            arguments.pop("reasoning_effort", None)
    else:
        arguments.setdefault("reasoning_effort", mapped)

    return arguments


def _anthropic_budget(effort: str) -> int:
    return {
        "none": 1024,
        "minimal": 1024,
        "low": 2048,
        "medium": 4096,
        "high": 8192,
        "max": 16384,
    }[effort]


__all__ = [
    "PROVIDER_SPECS",
    "SEMANTIC_EFFORTS",
    "ProviderSpec",
    "normalize_semantic_effort",
    "provider_spec",
    "reasoning_arguments",
]
