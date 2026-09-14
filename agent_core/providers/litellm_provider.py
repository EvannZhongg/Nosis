import base64
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from litellm import completion, get_model_info, token_counter

from agent_core.llm import (
    LLMProvider,
    LLMRequest,
    LLMResponse,
    ProviderCapabilities,
    TokenUsage,
)
from agent_core.content import ImagePart, TextPart
from agent_core.session import Message
from agent_core.tools import AnalyzeImageTool, ToolCall, ToolDefinition


class LiteLLMProvider(LLMProvider):
    def __init__(
        self,
        model: str,
        base_url: str | None = None,
        api_key: str | None = None,
        max_context_tokens: int | None = None,
        media_root: Path | None = None,
    ) -> None:
        self._model = model
        self._base_url = base_url
        self._api_key = api_key
        self._media_root = media_root.expanduser().resolve() if media_root is not None else None
        if max_context_tokens is not None:
            if (
                isinstance(max_context_tokens, bool)
                or not isinstance(max_context_tokens, int)
                or max_context_tokens < 1
            ):
                raise ValueError(
                    "max_context_tokens must be a positive integer"
                )
            self._max_context_tokens = max_context_tokens
        else:
            self._max_context_tokens = _get_model_max_context_tokens(
                model,
                base_url,
            )
        self._capabilities: ProviderCapabilities | None = None

    @property
    def capabilities(self) -> ProviderCapabilities:
        if self._capabilities is None:
            self._capabilities = self.capabilities_for_model(
                self._model,
                self._base_url,
            )
        return self._capabilities

    @classmethod
    def capabilities_for_model(
        cls,
        model: str,
        base_url: str | None = None,
    ) -> ProviderCapabilities:
        modalities = {"text"}
        try:
            info = get_model_info(model=model, api_base=base_url)
            if info.get("supports_vision") is True:
                modalities.add("image")
        except Exception:
            # Capability discovery must not prevent text-only providers
            # from being used when LiteLLM has no model metadata.
            pass
        return ProviderCapabilities(frozenset(modalities))

    @property
    def max_context_tokens(self) -> int:
        return self._max_context_tokens

    def count_input_tokens(self, request: LLMRequest) -> int:
        messages = _request_messages(request, self)
        tools = _request_tools(request)
        return token_counter(
            model=self._model,
            messages=messages,
            tools=tools or None,
        )

    def stream(
        self,
        request: LLMRequest,
        on_text_delta: Callable[[str], None],
        on_reasoning_delta: Callable[[str], None] | None = None,
    ) -> LLMResponse:
        arguments = dict(
            model=self._model,
            base_url=self._base_url,
            api_key=self._api_key,
            messages=_request_messages(request, self),
            stream=True,
            # Streamed responses omit usage unless it is requested
            # explicitly; it arrives in a final usage-only chunk.
            stream_options={"include_usage": True},
        )
        tools = _request_tools(request)
        if tools:
            arguments["tools"] = tools
        if request.max_output_tokens is not None:
            arguments["max_tokens"] = request.max_output_tokens

        content = ""
        reasoning = ""
        tool_call_fragments: dict[int, _ToolCallFragment] = {}
        usage = None

        for chunk in completion(**arguments):
            chunk_usage = getattr(chunk, "usage", None)
            if chunk_usage is not None:
                usage = chunk_usage

            choices = getattr(chunk, "choices", None) or []
            if not choices:
                continue

            delta = _get_field(choices[0], "delta")
            if delta is None:
                continue

            text = _get_field(delta, "content")
            if isinstance(text, str) and text:
                content += text
                on_text_delta(text)

            for field in ("reasoning_content", "reasoning", "thinking"):
                value = _get_field(delta, field)
                if isinstance(value, str) and value:
                    reasoning += value
                    if on_reasoning_delta is not None:
                        on_reasoning_delta(value)

            for tool_call in _get_field(delta, "tool_calls") or []:
                _accumulate_tool_call(tool_call_fragments, tool_call)

        return LLMResponse(
            content=content or None,
            reasoning=reasoning or None,
            tool_calls=tuple(
                fragment.to_tool_call()
                for _, fragment in sorted(tool_call_fragments.items())
            ),
            usage=TokenUsage(
                input_tokens=usage.prompt_tokens,
                output_tokens=usage.completion_tokens,
                total_tokens=usage.total_tokens,
            )
            if usage is not None
            else None,
        )


def _request_messages(
    request: LLMRequest,
    provider: LiteLLMProvider | None = None,
) -> list[dict[str, object]]:
    return [
        {"role": "system", "content": request.system_prompt},
        *[
            _message_to_dict(
                message,
                include_images=(
                    provider is None
                    or "image" in provider.capabilities.input_modalities
                ),
                # Only point the model at the tool when it actually has it.
                can_analyze_images=any(
                    tool.name == AnalyzeImageTool.name for tool in request.tools
                ),
                media_root=(
                    request.media_root
                    if request.media_root is not None
                    else (
                        getattr(provider, "_media_root", None)
                        if provider is not None
                        else None
                    )
                ),
            )
            for message in request.messages
        ],
    ]


def _request_tools(request: LLMRequest) -> list[dict[str, object]]:
    return [_tool_definition_to_dict(tool) for tool in request.tools]


def _get_model_max_context_tokens(
    model: str,
    base_url: str | None,
) -> int:
    try:
        model_info = get_model_info(model=model, api_base=base_url)
    except Exception as error:
        raise ValueError(
            f"LiteLLM has no context limit metadata for model '{model}'; "
            "configure 'max_context_tokens' for this provider"
        ) from error

    max_context_tokens = model_info.get("max_input_tokens")
    if max_context_tokens is None:
        max_context_tokens = model_info.get("max_tokens")
    if (
        isinstance(max_context_tokens, bool)
        or not isinstance(max_context_tokens, int)
        or max_context_tokens < 1
    ):
        raise ValueError(
            f"LiteLLM has no context limit metadata for model '{model}'; "
            "configure 'max_context_tokens' for this provider"
        )
    return max_context_tokens


def _message_to_dict(
    message: Message,
    *,
    include_images: bool = True,
    can_analyze_images: bool = False,
    media_root: Path | None = None,
) -> dict[str, object]:
    data: dict[str, object] = {
        "role": message.role,
        "content": _content_to_provider_format(
            message,
            include_images=include_images,
            can_analyze_images=can_analyze_images,
            media_root=media_root,
        ),
    }
    if message.reasoning is not None:
        data["reasoning_content"] = message.reasoning
    if message.tool_calls:
        data["tool_calls"] = [
            {
                "id": tool_call.id,
                "type": "function",
                "function": {
                    "name": tool_call.name,
                    "arguments": json.dumps(
                        tool_call.arguments,
                        ensure_ascii=False,
                    ),
                },
            }
            for tool_call in message.tool_calls
        ]
    if message.tool_call_id is not None:
        data["tool_call_id"] = message.tool_call_id
    return data


def _content_to_provider_format(
    message: Message,
    *,
    include_images: bool = True,
    can_analyze_images: bool = False,
    media_root: Path | None = None,
) -> object:
    parts = message.parts
    if not parts:
        return None
    if all(isinstance(part, TextPart) for part in parts):
        return "".join(part.text for part in parts)
    if not include_images:
        # This model cannot see an image, so the attachment is named rather
        # than sent. Naming the tool it does not have would only invite a
        # call that fails, so the hint depends on the registered tool set.
        text = "".join(part.text for part in parts if isinstance(part, TextPart))
        paths = [part.path for part in parts if isinstance(part, ImagePart)]
        heading = (
            f"Attached images (use {AnalyzeImageTool.name} if needed):"
            if can_analyze_images
            else "Attached images (this model cannot read them):"
        )
        listed = "\n".join(f"- {path}" for path in paths)
        return f"{text}\n\n{heading}\n{listed}"
    rendered: list[dict[str, object]] = []
    for part in parts:
        if isinstance(part, TextPart):
            rendered.append({"type": "text", "text": part.text})
        elif isinstance(part, ImagePart):
            try:
                path = _resolve_media_path(part.path, media_root)
            except FileNotFoundError:
                rendered.append(
                    {
                        "type": "text",
                        "text": (
                            "[Image attachment unavailable in this workspace: "
                            f"{part.path}]"
                        ),
                    }
                )
                continue
            with path.open("rb") as file:
                encoded = base64.b64encode(file.read()).decode("ascii")
            rendered.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{part.mime_type};base64,{encoded}"
                    },
                }
            )
    return rendered


def _resolve_media_path(path: str, media_root: Path | None) -> Path:
    candidate = Path(path)
    was_relative = not candidate.is_absolute()
    if was_relative:
        if media_root is None:
            raise ValueError(
                "relative image paths require a provider media_root"
            )
        candidate = media_root / candidate
    resolved = candidate.expanduser().resolve()
    if was_relative and media_root is not None:
        try:
            resolved.relative_to(media_root)
        except ValueError as error:
            raise ValueError("image path must stay within media_root") from error
    if not resolved.is_file():
        raise FileNotFoundError(f"image attachment does not exist: {path}")
    return resolved


def _tool_definition_to_dict(tool: ToolDefinition) -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        },
    }


@dataclass
class _ToolCallFragment:
    """Streamed tool call assembled across chunks.

    Providers send ``id`` and ``name`` once, then split
    ``function.arguments`` into string fragments.
    """

    id: str | None = None
    name: str | None = None
    arguments: str = ""

    def to_tool_call(self) -> ToolCall:
        if not isinstance(self.id, str) or not self.id:
            raise ValueError("tool call id must be a non-empty string")
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("tool call name must be a non-empty string")

        arguments = json.loads(self.arguments) if self.arguments else {}
        if not isinstance(arguments, dict):
            raise ValueError("tool call arguments must be a JSON object")

        return ToolCall(id=self.id, name=self.name, arguments=arguments)


def _accumulate_tool_call(
    fragments: dict[int, _ToolCallFragment],
    tool_call: object,
) -> None:
    index = _get_field(tool_call, "index")
    if not isinstance(index, int) or isinstance(index, bool):
        index = 0

    fragment = fragments.setdefault(index, _ToolCallFragment())
    tool_call_id = _get_field(tool_call, "id")
    if isinstance(tool_call_id, str) and tool_call_id:
        fragment.id = tool_call_id

    function = _get_field(tool_call, "function")
    if function is None:
        return

    name = _get_field(function, "name")
    if isinstance(name, str) and name:
        fragment.name = name

    arguments = _get_field(function, "arguments")
    if isinstance(arguments, str):
        fragment.arguments += arguments


def _get_field(value: object, name: str) -> object:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)
