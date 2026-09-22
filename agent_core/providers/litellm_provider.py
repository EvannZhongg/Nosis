import json
from contextvars import copy_context
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, Thread
from typing import Callable, Iterable, Iterator

from litellm import completion, get_model_info, token_counter

from agent_core.llm import (
    LLMProvider,
    LLMRequest,
    LLMResponse,
    ProviderCapabilities,
    TokenUsage,
)
from agent_core.content import ImagePart, TextPart
from agent_core.media import (
    UnsupportedImageError,
    encode_data_url,
    probe_image,
)
from agent_core.path_utils import path_for_comparison
from agent_core.session import Message
from agent_core.tools import AnalyzeImageTool, ToolDefinition

from .tool_call_stream import ToolCallStreamAssembler


class LiteLLMProvider(LLMProvider):
    _CANCEL_POLL_SECONDS = 0.1

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
        self._model_info: dict[str, object] | None = None
        self._model_info_loaded = False
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
            try:
                model_info = self._get_model_info()
            except Exception as error:
                raise ValueError(
                    f"LiteLLM has no context limit metadata for model "
                    f"'{model}'; configure 'max_context_tokens' for this "
                    "provider"
                ) from error
            self._max_context_tokens = _get_model_max_context_tokens(
                model_info,
                model,
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

    @property
    def max_output_tokens(self) -> int | None:
        try:
            model_info = self._get_model_info()
        except Exception:
            return None
        value = model_info.get("max_output_tokens")
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 1
        ):
            return None
        return value

    def _get_model_info(self) -> dict[str, object]:
        if not self._model_info_loaded:
            self._model_info = get_model_info(
                model=self._model,
                api_base=self._base_url,
            )
            self._model_info_loaded = True
        assert self._model_info is not None
        return self._model_info

    def count_input_tokens(self, request: LLMRequest) -> int:
        """Price the request without encoding any image.

        Images are counted from their pixel dimensions and the text is
        counted by the tokenizer, so a context check reads image headers
        rather than whole files.  Encoding here would cost a full read
        and a base64 pass per image on every iteration of the agent
        loop, and the tokenizer does not inspect a data URL anyway: it
        prices one by a flat per-image constant regardless of size.
        """
        messages = _request_messages(request, self, encode_media=False)
        tools = _request_tools(request)
        text_tokens = token_counter(
            model=self._model,
            messages=messages,
            tools=tools or None,
        )
        return text_tokens + _media_tokens(request, self)

    def stream(
        self,
        request: LLMRequest,
        on_text_delta: Callable[[str], None],
        on_reasoning_delta: Callable[[str], None] | None = None,
    ) -> LLMResponse:
        return self._stream(
            request,
            on_text_delta,
            on_reasoning_delta,
            check_cancelled=lambda: None,
        )

    def stream_cancellable(
        self,
        request: LLMRequest,
        on_text_delta: Callable[[str], None],
        on_reasoning_delta: Callable[[str], None] | None,
        check_cancelled: Callable[[], None],
    ) -> LLMResponse:
        return self._stream(
            request,
            on_text_delta,
            on_reasoning_delta,
            check_cancelled=check_cancelled,
        )

    def _stream(
        self,
        request: LLMRequest,
        on_text_delta: Callable[[str], None],
        on_reasoning_delta: Callable[[str], None] | None,
        *,
        check_cancelled: Callable[[], None],
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
        if request.max_generation_tokens is not None:
            arguments["max_completion_tokens"] = (
                request.max_generation_tokens
            )

        content = ""
        reasoning = ""
        tool_calls = ToolCallStreamAssembler(self._model)
        usage = None

        chunks = _cancellable_chunks(
            lambda: completion(**arguments),
            check_cancelled,
            self._CANCEL_POLL_SECONDS,
        )
        for chunk in chunks:
            chunk_usage = getattr(chunk, "usage", None)
            if chunk_usage is not None:
                usage = chunk_usage

            choices = getattr(chunk, "choices", None) or []
            if not choices:
                continue

            delta = _get_field(choices[0], "delta")
            if delta is None:
                continue

            # Most OpenAI-compatible providers stream ``content`` as a
            # string.  Some LiteLLM adapters (notably multimodal providers)
            # return the same text as structured content blocks instead,
            # e.g. ``[{"type": "text", "text": "..."}]``.  Normalise both
            # forms before deciding that the response is empty; otherwise a
            # perfectly valid streamed answer is discarded and Agent raises
            # ``LLM response must contain content or tool calls``.
            text = _text_from_content(_get_field(delta, "content"))
            if text:
                content += text
                on_text_delta(text)

            for field in ("reasoning_content", "reasoning", "thinking"):
                value = _get_field(delta, field)
                if isinstance(value, str) and value:
                    reasoning += value
                    if on_reasoning_delta is not None:
                        on_reasoning_delta(value)

            tool_calls.add_batch(_get_field(delta, "tool_calls") or [])

        return LLMResponse(
            content=content or None,
            reasoning=reasoning or None,
            tool_calls=tool_calls.finish(),
            usage=TokenUsage(
                input_tokens=usage.prompt_tokens,
                output_tokens=usage.completion_tokens,
                total_tokens=usage.total_tokens,
            )
            if usage is not None
            else None,
        )


_STREAM_END = object()


def _cancellable_chunks(
    create_stream: Callable[[], Iterable[object]],
    check_cancelled: Callable[[], None],
    poll_seconds: float,
) -> Iterator[object]:
    """Yield a synchronous provider stream without blocking cancellation checks."""
    queue: Queue[object | BaseException] = Queue(maxsize=1)
    stopped = Event()

    def publish(item: object | BaseException) -> bool:
        while not stopped.is_set():
            try:
                queue.put(item, timeout=poll_seconds)
                return True
            except Full:
                continue
        return False

    def read_stream() -> None:
        try:
            for chunk in create_stream():
                if not publish(chunk):
                    return
        except BaseException as error:
            if not publish(error):
                return
        publish(_STREAM_END)

    reader = Thread(
        target=copy_context().run,
        args=(read_stream,),
        name="litellm-stream-reader",
        daemon=True,
    )
    reader.start()
    try:
        while True:
            check_cancelled()
            try:
                item = queue.get(timeout=poll_seconds)
            except Empty:
                continue
            check_cancelled()
            if item is _STREAM_END:
                return
            if isinstance(item, BaseException):
                raise item
            yield item
    finally:
        stopped.set()


def _request_messages(
    request: LLMRequest,
    provider: LiteLLMProvider | None = None,
    *,
    encode_media: bool = True,
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
                media_root=_media_root(request, provider),
                encode_media=encode_media,
            )
            for message in request.messages
        ],
    ]


def _media_root(
    request: LLMRequest,
    provider: "LiteLLMProvider | None",
) -> Path | None:
    if request.media_root is not None:
        return request.media_root
    if provider is not None:
        return provider._media_root
    return None


def _media_tokens(
    request: LLMRequest,
    provider: "LiteLLMProvider | None",
) -> int:
    """Sum the estimated cost of every image the request will send."""
    if provider is not None and "image" not in provider.capabilities.input_modalities:
        return 0
    media_root = _media_root(request, provider)
    total = 0
    for message in request.messages:
        for part in message.parts:
            if not isinstance(part, ImagePart):
                continue
            try:
                path = _resolve_media_path(part.path, media_root)
                info = probe_image(path)
            except (FileNotFoundError, ValueError, UnsupportedImageError):
                # An unreadable image is sent as a short text notice, so
                # it costs nothing beyond what the tokenizer counted.
                continue
            total += info.token_estimate
    return total


def _request_tools(request: LLMRequest) -> list[dict[str, object]]:
    return [_tool_definition_to_dict(tool) for tool in request.tools]


def _get_model_max_context_tokens(
    model_info: dict[str, object],
    model: str,
) -> int:
    max_context_tokens = model_info.get("max_input_tokens")
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
    encode_media: bool = True,
) -> dict[str, object]:
    data: dict[str, object] = {
        "role": message.role,
        "content": _content_to_provider_format(
            message,
            include_images=include_images,
            can_analyze_images=can_analyze_images,
            media_root=media_root,
            encode_media=encode_media,
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
    encode_media: bool = True,
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
            if not encode_media:
                # Counting tokens never needs the bytes: the caller adds
                # each image's estimated cost separately. A placeholder
                # keeps the message shape intact for the tokenizer.
                rendered.append({"type": "text", "text": f"[image {part.path}]"})
                continue
            rendered.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": encode_data_url(path, part.mime_type)
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
        # Both sides must be resolved before being compared: a root that
        # still contains a symlink (macOS serves /var as /private/var)
        # would not be a prefix of the resolved candidate, and a
        # legitimate path would be rejected as an escape.
        root = media_root.expanduser().resolve()
        try:
            path_for_comparison(resolved).relative_to(
                path_for_comparison(root)
            )
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


def _get_field(value: object, name: str) -> object:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _text_from_content(value: object) -> str:
    """Extract text from string or OpenAI-style content blocks.

    LiteLLM normally exposes streamed deltas as strings, but adapters for
    some providers expose a list of typed blocks.  Only the text field is
    considered so metadata or image blocks cannot accidentally become part
    of the assistant message.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return "".join(_text_from_content(item) for item in value)
    text = _get_field(value, "text")
    return text if isinstance(text, str) else ""
