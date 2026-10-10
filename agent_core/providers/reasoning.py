"""Translate effort settings into request parameters for a model route."""

from copy import deepcopy
from typing import Mapping


SEMANTIC_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max"})
_CUSTOM_PARAMETER_NAMES = frozenset(
    {"reasoning_effort", "thinking", "thinking_level", "reasoning", "extra_body"}
)


_MISTRAL_EFFORTS = {
    "none": "none",
    "minimal": "none",
    "low": "none",
    "medium": "high",
    "high": "high",
    "xhigh": "high",
    "max": "high",
}

def normalize_semantic_effort(value: str) -> str:
    """Normalize the small public effort vocabulary."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("reasoning_effort must be a non-empty string")
    normalized = value.strip().lower()
    if normalized not in SEMANTIC_EFFORTS:
        allowed = ", ".join(sorted(SEMANTIC_EFFORTS))
        raise ValueError(
            f"reasoning_effort must be one of {allowed}; got {value!r}"
        )
    return normalized


def validate_reasoning_parameters(
    value: Mapping[str, object] | None,
) -> dict[str, object]:
    """Validate and copy custom reasoning request parameters."""
    parameters = deepcopy(dict(value or {}))
    unknown = set(parameters) - _CUSTOM_PARAMETER_NAMES
    if unknown:
        names = ", ".join(sorted(unknown))
        raise ValueError(
            "reasoning_parameters contains unsupported field(s): " + names
        )
    for name in {"thinking", "reasoning", "extra_body"} & parameters.keys():
        if not isinstance(parameters[name], dict):
            raise ValueError(f"reasoning_parameters.{name} must be an object")
    for name in {"reasoning_effort", "thinking_level"} & parameters.keys():
        if not isinstance(parameters[name], str) or not parameters[name].strip():
            raise ValueError(f"reasoning_parameters.{name} must be a non-empty string")
    extra_body = parameters.get("extra_body", {})
    if "reasoning" in extra_body and not isinstance(extra_body["reasoning"], dict):
        raise ValueError("reasoning_parameters.extra_body.reasoning must be an object")
    return parameters


def reasoning_arguments(
    model: str,
    effort: str | None,
    custom_parameters: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Return request parameters; explicit values override mapped defaults.

    ``custom_parameters`` is intentionally an escape hatch for self-hosted
    OpenAI-compatible services.  An omitted effort leaves the request
    untouched, preserving the upstream model default.
    """
    arguments = validate_reasoning_parameters(custom_parameters)
    if effort is None:
        return arguments
    semantic_effort = normalize_semantic_effort(effort)
    route = model.strip().lower().partition("/")[0]
    mapped = (
        _MISTRAL_EFFORTS[semantic_effort] if route == "mistral"
        else "minimum" if route == "dashscope" and semantic_effort == "minimal"
        else semantic_effort
    )

    if route == "openrouter":
        reasoning = arguments.setdefault("extra_body", {}).setdefault("reasoning", {})
        reasoning.setdefault("effort", mapped)
    elif route == "anthropic":
        arguments.setdefault(
            "thinking",
            {"type": "disabled"}
            if semantic_effort == "none"
            else {
                "type": "enabled",
                "budget_tokens": _anthropic_budget(semantic_effort),
            },
        )
    elif route == "bedrock":
        arguments.setdefault(
            "extra_body",
            {
                "additionalModelRequestFields": {
                    "thinking": {"type": "adaptive", "effort": mapped},
                },
            },
        )
    elif route == "xai":
        arguments.setdefault(
            "extra_body",
            {"reasoning": {"summary": "concise", "effort": mapped}},
        )
    elif route in {"gemini", "vertex_ai"}:
        arguments.setdefault("thinking_level", mapped)
    elif route == "moonshot":
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
        "xhigh": 16384,
        "max": 16384,
    }[effort]


__all__ = [
    "SEMANTIC_EFFORTS",
    "normalize_semantic_effort",
    "reasoning_arguments",
    "validate_reasoning_parameters",
]
