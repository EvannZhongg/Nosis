"""Native OpenAI wire protocols, shared by official and relay endpoints."""

import json
from typing import Callable

import httpx
from openai import APIError, OpenAI

from agent_core.errors import ProviderProtocolError, ProviderStreamError
from agent_core.llm import LLMRequest, LLMResponse, TokenUsage
from agent_core.tools import ToolCall

from .base import ModelProvider, _cancellable_chunks, _request_messages, _request_tools
from .reasoning import reasoning_arguments
from .tool_call_stream import ToolCallStreamAssembler


OPENAI_APIS = frozenset({"responses", "chat_completions"})


class OpenAIProvider(ModelProvider):
    def __init__(self, *args, api: str = "responses", wire_model: str | None = None, **kwargs) -> None:
        if api not in OPENAI_APIS:
            raise ValueError("api must be responses or chat_completions")
        super().__init__(*args, **kwargs)
        self._api = api
        self._wire_model = wire_model if wire_model is not None else self._model

    def _stream(
        self,
        request: LLMRequest,
        on_text_delta: Callable[[str], None],
        on_reasoning_delta: Callable[[str], None] | None,
        *,
        check_cancelled: Callable[[], None],
    ) -> LLMResponse:
        # Client and stream belong to this call, so cancellation releases both.
        with OpenAI(
            api_key=self._api_key,
            base_url=self._base_url,
            timeout=self._request_timeout_seconds,
            max_retries=self._max_retries,
        ) as client:
            if self._api == "responses":
                return self._responses(
                    client, request, on_text_delta, on_reasoning_delta, check_cancelled,
                )
            return self._chat(
                client, request, on_text_delta, on_reasoning_delta, check_cancelled,
            )

    def _responses(self, client, request, on_text_delta, on_reasoning_delta, check_cancelled):
        arguments = {
            "model": self._wire_model,
            "instructions": request.system_prompt,
            "input": _responses_input(request, self),
            "stream": True,
            "store": False,
            "include": ["reasoning.encrypted_content"],
        }
        reasoning = reasoning_arguments(
            "openai/" + self._wire_model,
            self._reasoning_effort,
            self._reasoning_parameters,
        )
        effort = reasoning.pop("reasoning_effort", None)
        if effort is not None:
            reasoning.setdefault("reasoning", {}).setdefault("effort", effort)
        unsupported = set(reasoning) - {"reasoning", "extra_body"}
        if unsupported:
            raise ValueError(f"Responses API does not accept: {', '.join(sorted(unsupported))}")
        arguments.update(reasoning)
        tools = _request_tools(request)
        if tools:
            arguments["tools"] = [
                {"type": "function", **tool["function"], "strict": False}
                for tool in tools
            ]
        if request.max_generation_tokens is not None:
            arguments["max_output_tokens"] = request.max_generation_tokens

        reasoning_text = ""
        completed = None
        chunks = _cancellable_chunks(
            lambda: client.responses.create(**arguments),
            check_cancelled,
            self._CANCEL_POLL_SECONDS,
        )
        try:
            for event in chunks:
                if event.type in {"response.output_text.delta", "response.refusal.delta"}:
                    on_text_delta(event.delta)
                elif event.type in {
                    "response.reasoning_summary_text.delta", "response.reasoning_text.delta",
                }:
                    reasoning_text += event.delta
                    if on_reasoning_delta is not None:
                        on_reasoning_delta(event.delta)
                elif event.type == "response.completed":
                    completed = event.response
                    break
                elif event.type in {"response.failed", "response.incomplete"}:
                    raise self._stream_error(
                        event.type, status=event.response.status,
                        error=(event.response.error.model_dump() if event.response.error else None),
                        incomplete_details=(
                            event.response.incomplete_details.model_dump()
                            if event.response.incomplete_details else None
                        ),
                    )
                elif event.type == "error":
                    raise self._stream_error("api_error", code=event.code, message=event.message)
        except (APIError, httpx.TransportError) as error:
            raise self._stream_error("stream_transport_error", message=str(error)) from error
        finally:
            chunks.close()
        if completed is None:
            raise self._stream_error("missing_completed_event")
        if completed.status != "completed":
            raise self._stream_error("incomplete_response", status=completed.status)

        calls = []
        output = []
        content = []
        for item in completed.output:
            output.append(item.model_dump(mode="json", exclude_none=True))
            if item.type == "function_call":
                if not item.call_id or not item.name:
                    raise ProviderProtocolError(
                        "tool call id and name must be non-empty",
                        details={"phase": "tool_call_assembly", "model": self._model,
                                 "reason": "missing_identity"},
                    )
                try:
                    parameters = json.loads(item.arguments)
                except (TypeError, json.JSONDecodeError) as error:
                    raise ProviderProtocolError(
                        f"invalid arguments for tool '{item.name}'",
                        details={"phase": "tool_call_assembly", "model": self._model,
                                 "reason": "invalid_json", "tool_name": item.name,
                                 "tool_call_id": item.call_id},
                    ) from error
                if not isinstance(parameters, dict):
                    raise ProviderProtocolError(
                        f"arguments for tool '{item.name}' must be a JSON object",
                        details={"phase": "tool_call_assembly", "model": self._model,
                                 "reason": "arguments_not_object", "tool_name": item.name},
                    )
                calls.append(ToolCall(item.call_id, item.name, parameters))
            elif item.type == "message":
                for part in item.content:
                    if part.type == "output_text":
                        content.append(part.text)
                    elif part.type == "refusal":
                        content.append(part.refusal)
        usage = completed.usage
        provider_data = {"openai_responses": output}
        if usage is not None and usage.output_tokens_details is not None:
            provider_data["openai_reasoning_tokens"] = usage.output_tokens_details.reasoning_tokens or 0
        return LLMResponse(
            content="".join(content) or None,
            reasoning=reasoning_text or None,
            tool_calls=tuple(calls),
            usage=TokenUsage(usage.input_tokens, usage.output_tokens, usage.total_tokens)
            if usage is not None else None,
            provider_data=provider_data,
        )

    def count_input_tokens(self, request: LLMRequest) -> int:
        tokens = super().count_input_tokens(request)
        if self._api == "responses":
            # Encrypted reasoning cannot be counted by a text tokenizer.
            tokens += sum(message.provider_data.get("openai_reasoning_tokens", 0)
                          for message in request.messages)
        return tokens

    def _chat(self, client, request, on_text_delta, on_reasoning_delta, check_cancelled):
        reasoning = reasoning_arguments(
            "openai/" + self._wire_model, self._reasoning_effort, self._reasoning_parameters,
        )
        # Nonstandard fields use the SDK's extension body for any relay.
        extra_body = reasoning.pop("extra_body", {})
        for name in tuple(reasoning):
            if name != "reasoning_effort":
                extra_body.setdefault(name, reasoning.pop(name))
        if extra_body:
            reasoning["extra_body"] = extra_body
        arguments = {
            "model": self._wire_model,
            "messages": _request_messages(request, self),
            "stream": True,
            "stream_options": {"include_usage": True},
            **reasoning,
        }
        tools = _request_tools(request)
        if tools:
            arguments["tools"] = tools
        if request.max_generation_tokens is not None:
            arguments["max_completion_tokens"] = request.max_generation_tokens
        content = ""
        reasoning_text = ""
        calls = ToolCallStreamAssembler(self._model)
        usage = None
        finished = False
        chunks = _cancellable_chunks(
            lambda: client.chat.completions.create(**arguments),
            check_cancelled,
            self._CANCEL_POLL_SECONDS,
        )
        try:
            for chunk in chunks:
                if chunk.usage is not None:
                    usage = chunk.usage
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                if choice.finish_reason is not None:
                    if choice.finish_reason not in {"stop", "tool_calls"}:
                        raise self._stream_error("incomplete_response", finish_reason=choice.finish_reason)
                    finished = True
                if choice.delta.content:
                    content += choice.delta.content
                    on_text_delta(choice.delta.content)
                text = getattr(choice.delta, "reasoning_content", None)
                if text:
                    reasoning_text += text
                    if on_reasoning_delta is not None:
                        on_reasoning_delta(text)
                calls.add_batch(choice.delta.tool_calls or [])
        except (APIError, httpx.TransportError) as error:
            raise self._stream_error("stream_transport_error", message=str(error)) from error
        finally:
            chunks.close()
        if not finished:
            raise self._stream_error("missing_finish_reason")
        return LLMResponse(
            content=content or None, reasoning=reasoning_text or None,
            tool_calls=calls.finish(),
            usage=TokenUsage(usage.prompt_tokens, usage.completion_tokens, usage.total_tokens)
            if usage is not None else None,
        )

    def _stream_error(self, reason: str, **details: object) -> ProviderStreamError:
        return ProviderStreamError(
            f"{self._api} stream did not complete: {reason}",
            details={"phase": "response_stream", "model": self._model,
                     "api": self._api, "reason": reason, **details},
        )


def _responses_input(request: LLMRequest, provider: OpenAIProvider) -> list[dict[str, object]]:
    inputs = []
    messages = _request_messages(request, provider)[1:]
    for message, rendered in zip(request.messages, messages):
        native_output = message.provider_data.get("openai_responses")
        if message.role == "assistant" and native_output:
            inputs.extend(native_output)
            continue
        if message.role == "tool":
            inputs.append({"type": "function_call_output", "call_id": message.tool_call_id,
                           "output": rendered["content"] or ""})
            continue
        content = rendered["content"]
        if isinstance(content, list):
            content = [
                {"type": "input_text", "text": part["text"]}
                if part["type"] == "text" else
                {"type": "input_image", "image_url": part["image_url"]["url"]}
                for part in content
            ]
        if content:
            inputs.append({"role": message.role, "content": content})
        for call in message.tool_calls:
            inputs.append({"type": "function_call", "call_id": call.id, "name": call.name,
                           "arguments": json.dumps(call.arguments, ensure_ascii=False)})
    return inputs
