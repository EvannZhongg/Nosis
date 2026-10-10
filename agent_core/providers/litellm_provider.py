from typing import Callable

from litellm import completion
from litellm.exceptions import MidStreamFallbackError

from agent_core.errors import ProviderStreamError
from agent_core.llm import LLMRequest, LLMResponse, TokenUsage

from .base import ModelProvider, _cancellable_chunks, _request_messages, _request_tools, _text_from_content
from ._utils import get_field
from .reasoning import reasoning_arguments
from .tool_call_stream import ToolCallStreamAssembler


class LiteLLMProvider(ModelProvider):
    def _stream(
        self,
        request: LLMRequest,
        on_text_delta: Callable[[str], None],
        on_reasoning_delta: Callable[[str], None] | None,
        *,
        check_cancelled: Callable[[], None],
    ) -> LLMResponse:
        base_arguments = dict(
            model=self._model,
            base_url=self._base_url,
            api_key=self._api_key,
            messages=_request_messages(request, self),
            stream=True,
            timeout=self._request_timeout_seconds,
            max_retries=self._max_retries,
            # Streamed responses omit usage unless it is requested
            # explicitly; it arrives in a final usage-only chunk.
            stream_options={"include_usage": True},
        )
        reasoning = reasoning_arguments(
            self._model,
            self._reasoning_effort,
            self._reasoning_parameters,
        )
        base_arguments.update(reasoning)
        if "reasoning_effort" in reasoning:
            base_arguments["allowed_openai_params"] = ["reasoning_effort"]
        tools = _request_tools(request)
        if tools:
            base_arguments["tools"] = tools
        if request.max_generation_tokens is not None:
            base_arguments["max_completion_tokens"] = (
                request.max_generation_tokens
            )

        content = ""
        reasoning_text = ""
        tool_calls = ToolCallStreamAssembler(self._model)
        usage = None
        chunks = _cancellable_chunks(
            lambda: completion(**base_arguments),
            check_cancelled,
            self._CANCEL_POLL_SECONDS,
        )
        try:
            for chunk in chunks:
                chunk_usage = getattr(chunk, "usage", None)
                if chunk_usage is not None:
                    usage = chunk_usage
                choices = getattr(chunk, "choices", None) or []
                if not choices:
                    continue
                delta = get_field(choices[0], "delta")
                if delta is None:
                    continue
                text = _text_from_content(get_field(delta, "content"))
                if text:
                    content += text
                    on_text_delta(text)
                for field in ("reasoning_content", "reasoning", "thinking"):
                    value = get_field(delta, field)
                    if isinstance(value, str) and value:
                        reasoning_text += value
                        if on_reasoning_delta is not None:
                            on_reasoning_delta(value)
                tool_calls.add_batch(get_field(delta, "tool_calls") or [])
        except MidStreamFallbackError as error:
            raise ProviderStreamError(
                "provider stream ended before the response completed: " + str(error),
                details={
                    "phase": "response_stream",
                    "model": self._model,
                    "reason": "mid_stream_disconnect",
                    "partial_content_length": len(content),
                    "partial_reasoning_length": len(reasoning_text),
                    "argument_chunk_count": tool_calls.argument_fragment_count,
                },
            ) from error
        finally:
            chunks.close()
        return LLMResponse(
            content=content or None,
            reasoning=reasoning_text or None,
            tool_calls=tool_calls.finish(),
            usage=_token_usage(usage),
        )


def _token_usage(usage: object | None) -> TokenUsage | None:
    if usage is None:
        return None
    return TokenUsage(
        input_tokens=getattr(usage, "prompt_tokens"),
        output_tokens=getattr(usage, "completion_tokens"),
        total_tokens=getattr(usage, "total_tokens"),
    )
