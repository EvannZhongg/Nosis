from importlib import import_module

from .reasoning import (
    SEMANTIC_EFFORTS,
    normalize_semantic_effort,
    reasoning_arguments,
    validate_reasoning_parameters,
)

OPENAI_APIS = frozenset({"responses", "chat_completions"})

_PROVIDER_MODULES = {
    "LiteLLMImageGenerator": ".image_generation",
    "LiteLLMProvider": ".litellm_provider",
    "ModelProvider": ".base",
    "OpenAIProvider": ".openai_provider",
}


def __getattr__(name: str):
    if name not in _PROVIDER_MODULES:
        raise AttributeError(name)
    value = getattr(import_module(_PROVIDER_MODULES[name], __name__), name)
    globals()[name] = value
    return value

__all__ = [
    "LiteLLMImageGenerator",
    "LiteLLMProvider",
    "ModelProvider",
    "OpenAIProvider",
    "OPENAI_APIS",
    "SEMANTIC_EFFORTS",
    "normalize_semantic_effort",
    "reasoning_arguments",
    "validate_reasoning_parameters",
]
