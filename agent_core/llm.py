from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from .session import Message
from .tools import ToolCall, ToolDefinition


@dataclass(frozen=True)
class LLMRequest:
    system_prompt: str
    messages: tuple[Message, ...]
    tools: tuple[ToolDefinition, ...] = ()
    max_generation_tokens: int | None = None
    # Runtime-only root used to resolve relative media paths. It is not
    # persisted in session logs or sent to the provider API.
    media_root: Path | None = None


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None


@dataclass(frozen=True)
class LLMResponse:
    content: str | None
    reasoning: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    usage: TokenUsage | None = None


@dataclass(frozen=True)
class ProviderCapabilities:
    input_modalities: frozenset[str] = frozenset({"text"})


class LLMProvider(ABC):
    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities()
    @property
    @abstractmethod
    def max_context_tokens(self) -> int:
        raise NotImplementedError

    @property
    def max_output_tokens(self) -> int | None:
        """Return the model's per-call output limit, if it is known."""
        return None

    @abstractmethod
    def count_input_tokens(self, request: LLMRequest) -> int:
        raise NotImplementedError

    @abstractmethod
    def stream(
        self,
        request: LLMRequest,
        on_text_delta: Callable[[str], None],
        on_reasoning_delta: Callable[[str], None] | None = None,
    ) -> LLMResponse:
        """Produce a response, reporting assistant text as it arrives.

        ``on_text_delta`` receives incremental fragments, never cumulative
        text. Implementations stay synchronous so the agent loop remains
        blocking and single-threaded.
        """
        raise NotImplementedError

    def stream_cancellable(
        self,
        request: LLMRequest,
        on_text_delta: Callable[[str], None],
        on_reasoning_delta: Callable[[str], None] | None,
        check_cancelled: Callable[[], None],
    ) -> LLMResponse:
        """Stream with cancellation checkpoints when the provider supports them."""
        check_cancelled()
        response = self.stream(request, on_text_delta, on_reasoning_delta)
        check_cancelled()
        return response


def with_generation_limit(
    request: LLMRequest,
    provider: LLMProvider,
    policy_limit: int | None = None,
) -> LLMRequest:
    if policy_limit is None:
        return request

    limit = policy_limit
    provider_limit = provider.max_output_tokens
    if provider_limit is not None:
        limit = min(limit, provider_limit)
    return replace(
        request,
        max_generation_tokens=limit,
    )
