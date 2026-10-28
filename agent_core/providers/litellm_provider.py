from typing import Callable
from importlib.metadata import version

import httpx
import tiktoken
from litellm import completion, get_model_info, token_counter
from litellm.exceptions import MidStreamFallbackError
from openai import APIConnectionError, APIError

from agent_core.errors import ProviderRequestError, ProviderStreamError
from agent_core.llm import LLMRequest, LLMResponse
from agent_core.usage import TokenUsage

from .base import ModelProvider, _cancellable_chunks, _media_tokens, _request_messages, _request_tools, _text_from_content
from ._utils import get_field
from .reasoning import reasoning_arguments
from .tool_call_stream import ToolCallStreamAssembler


TOKEN_COUNTING_IDENTITY = ("litellm", version("litellm"), tiktoken.__version__)


class LiteLLMProvider(ModelProvider):
    @property
    def token_counting_identity(self) -> object:
        return [super().token_counting_identity, TOKEN_COUNTING_IDENTITY]

    @classmethod
    def model_info_for(cls, model: str, base_url: str | None = None) -> dict[str, object]:
        return get_model_info(model=model, api_base=base_url)

    def count_input_tokens(self, request: LLMRequest) -> int:
        text_tokens = token_counter(
            model=self._model,
            messages=_request_messages(request, self, encode_media=False),
            tools=_request_tools(request) or None,
        )
        return text_tokens + _media_tokens(request, self)

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
        finish_reason = None
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
                choice = choices[0]
                if get_field(choice, "finish_reason") is not None:
                    finish_reason = get_field(choice, "finish_reason")
                delta = get_field(choice, "delta")
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
            raise self._stream_error(
                "mid_stream_disconnect", message=str(error),
                partial_content_length=len(content),
                partial_reasoning_length=len(reasoning_text),
                argument_chunk_count=tool_calls.argument_fragment_count,
            ) from error
        except (APIConnectionError, httpx.TransportError) as error:
            raise self._stream_error("stream_transport_error", message=str(error)) from error
        except APIError as error:
            status_code = get_field(error, "status_code")
            if isinstance(status_code, int) and 400 <= status_code < 600:
                raise ProviderRequestError(
                    f"provider request failed (HTTP {status_code}): {error}",
                    details={
                        "phase": "provider_request", "model": self._model,
                        "reason": "http_error", "status_code": status_code,
                        "request_id": get_field(error, "request_id"),
                        "message": str(error), "error": error.body,
                    },
                ) from error
            raise self._stream_error("api_error", message=str(error), error=error.body) from error
        finally:
            chunks.close()
        if finish_reason not in {"stop", "tool_calls"}:
            reason = "missing_finish_reason" if finish_reason is None else "incomplete_response"
            raise self._stream_error(reason, finish_reason=finish_reason)
        return LLMResponse(
            content=content or None,
            reasoning=reasoning_text or None,
            tool_calls=tool_calls.finish(),
            usage=_token_usage(usage),
        )

    def _stream_error(self, reason: str, **details: object) -> ProviderStreamError:
        message = f"provider stream did not complete: {reason}"
        if details.get("message"):
            message += f": {details['message']}"
        return ProviderStreamError(
            message,
            details={"phase": "response_stream", "model": self._model,
                     "reason": reason, **details},
        )


def _token_usage(usage: object | None) -> TokenUsage | None:
    if usage is None:
        return None
    details = get_field(usage, "prompt_tokens_details")
    return TokenUsage(
        input_tokens=getattr(usage, "prompt_tokens"),
        output_tokens=getattr(usage, "completion_tokens"),
        total_tokens=getattr(usage, "total_tokens"),
        cache_read_tokens=get_field(details, "cached_tokens"),
        cache_write_tokens=get_field(details, "cache_write_tokens"),
    )
