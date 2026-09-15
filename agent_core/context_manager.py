from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .config import AgentConfig
from .content import historical_content
from .llm import LLMProvider, LLMRequest, with_generation_limit
from .prompts import load_consolidator_prompt
from .session import Message, Session
from .tools import ToolDefinition


class ContextWindowExceededError(RuntimeError):
    def __init__(
        self,
        input_tokens: int,
        max_context_tokens: int,
        output_reserve_tokens: int,
    ) -> None:
        self.input_tokens = input_tokens
        self.max_context_tokens = max_context_tokens
        self.output_reserve_tokens = output_reserve_tokens
        self.max_input_tokens = max_context_tokens - output_reserve_tokens
        super().__init__(
            f"input context contains {input_tokens} tokens, exceeding the "
            f"maximum of {self.max_input_tokens} tokens "
            f"({max_context_tokens} context tokens minus "
            f"{output_reserve_tokens} reserved output tokens)"
        )


@dataclass(frozen=True)
class ContextLimits:
    hard_limit: int
    compression_enabled: bool
    compression_threshold: int


class ContextManager:
    """Build model context and maintain the session's archived checkpoint."""

    def __init__(
        self,
        provider: LLMProvider,
        session: Session,
        system_prompt: str,
        config: AgentConfig,
        media_root: Path | None = None,
    ) -> None:
        self._provider = provider
        self._session = session
        self._system_prompt = system_prompt
        self._output_reserve_tokens = config.output_reserve_tokens
        self._max_generation_tokens = config.max_generation_tokens
        self._turn_start: int | None = None
        self._media_root = media_root

        if self._output_reserve_tokens >= provider.max_context_tokens:
            raise ValueError(
                "output_reserve_tokens must be less than the provider's "
                "max_context_tokens"
            )
        hard_limit = (
            provider.max_context_tokens - self._output_reserve_tokens
        )
        if hard_limit <= 1:
            raise ValueError(
                "max_context_tokens minus output_reserve_tokens must be "
                "greater than 1"
            )
        compression = config.context
        trigger_ratio = _compression_trigger_ratio(
            provider.max_context_tokens,
            compression.trigger_ratio,
        )
        threshold = max(1, int(hard_limit * trigger_ratio))
        self.limits = ContextLimits(
            hard_limit=hard_limit,
            compression_enabled=compression.enabled,
            compression_threshold=threshold,
        )

    @property
    def hard_limit(self) -> int:
        return self.limits.hard_limit

    @property
    def compression_enabled(self) -> bool:
        return self.limits.compression_enabled

    def begin_turn(self, turn_start: int) -> None:
        self._turn_start = turn_start

    def build_request(
        self,
        tools: Iterable[ToolDefinition] = (),
    ) -> LLMRequest:
        system_prompt = self._system_prompt
        if self._session.archived_summary is not None:
            system_prompt = (
                f"{system_prompt}\n\n[Archived Context Summary]\n"
                f"{self._session.archived_summary}"
            )
        return LLMRequest(
            system_prompt=system_prompt,
            messages=tuple(self._context_messages()),
            tools=tuple(tools),
            media_root=self._media_root,
        )

    def should_archive(self, input_tokens: int) -> bool:
        return (
            self.compression_enabled
            and input_tokens >= self.limits.compression_threshold
            and bool(self._archivable_items())
        )

    def archive(self) -> int:
        """Compress the unarchived transcript into the session checkpoint."""
        items = self._archivable_items()
        previous = self._session.archived_summary
        system_prompt = load_consolidator_prompt()
        if previous is not None:
            system_prompt = (
                f"{system_prompt}\n\n[Archived Context Summary]\n{previous}"
            )
        historical_items = [_historical_message(item) for item in items]
        request = LLMRequest(
            system_prompt=system_prompt,
            messages=tuple([_timeline_message(historical_items), *historical_items]),
            media_root=self._media_root,
        )
        request = with_generation_limit(
            request,
            self._provider,
            self._max_generation_tokens,
        )
        response = self._provider.stream(request, lambda _text: None, None)
        summary = response.content.strip() if response.content else ""
        if response.tool_calls or not summary:
            raise ValueError("context consolidator must return text content")
        self._session.set_archived_summary(
            summary,
            self._session.archived_item_count + len(items),
        )
        return self._session.archived_item_count

    def _archivable_items(self) -> list[Message]:
        """Return only complete turns preceding the active run."""
        turn_start = self._turn_start
        if turn_start is None:
            raise RuntimeError(
                "begin_turn must be called before archiving context"
            )
        archive_start = self._session.archived_item_count
        projected = self._session.provider_messages()
        archive_end = max(archive_start, min(turn_start, len(projected)))
        return projected[archive_start:archive_end]

    def _context_messages(self) -> list[Message]:
        items = self._session.provider_messages()
        if self._turn_start is None:
            return list(items)
        historical_count = max(
            0,
            min(
                self._turn_start - self._session.archived_item_count,
                len(items),
            ),
        )
        historical = [_historical_message(item) for item in items[:historical_count]]
        visible = historical + list(items[historical_count:])
        result: list[Message] = []
        for index, item in enumerate(visible):
            # A turn begins where the person spoke. A synthesized media
            # message sits inside a turn, so it must not open a second
            # timeline block in the middle of one.
            if item.role == "user" and not item.is_tool_media:
                end = next(
                    (offset for offset in range(index + 1, len(visible))
                     if visible[offset].role == "user"
                     and not visible[offset].is_tool_media),
                    len(visible),
                )
                result.append(_timeline_message(visible[index:end]))
            result.append(item)
        return result


def _historical_message(item: Message) -> Message:
    timestamp_utc = item.timestamp_utc
    if item.role == "tool" or (item.role == "assistant" and item.tool_calls):
        timestamp_utc = None
    return Message(
        role=item.role,
        content=historical_content(item.content),
        timestamp_utc=timestamp_utc,
        tool_calls=item.tool_calls,
        tool_call_id=item.tool_call_id,
        # Thinking-mode providers (notably DeepSeek) require every assistant
        # reasoning_content value to be echoed in later requests.  Dropping
        # it while converting a turn to historical context makes the next
        # request invalid, including when the assistant step had no tools.
        reasoning=item.reasoning if item.role == "assistant" else None,
        origin=item.origin,
    )


def _timeline_message(items: list[Message]) -> Message:
    lines = []
    for item in items:
        if item.timestamp_utc is None:
            continue
        timestamp = item.timestamp_utc.astimezone().isoformat(timespec="seconds")
        detail = item.role
        if item.role == "assistant" and item.tool_calls:
            detail = "assistant step (" + ", ".join(call.name for call in item.tool_calls) + ")"
        elif item.role == "tool":
            detail = f"tool result ({item.tool_call_id or 'unknown'})"
        elif item.is_tool_media:
            # This message only exists because it is the one place an
            # image can travel. Labelling it "user" would read as the
            # person speaking again in the middle of their own turn.
            detail = "tool images"
        lines.append(f"- {timestamp} — {detail}")
    return Message(
        role="system",
        content=(
            "[Conversation Timeline]\n"
            "Use these timestamps only to understand chronology and elapsed "
            "time. Do not reproduce them in responses.\n"
            + "\n".join(lines)
        ),
    )


def _compression_trigger_ratio(
    max_context_tokens: int,
    trigger_ratio: float | None,
) -> float:
    if trigger_ratio is not None:
        return trigger_ratio
    if max_context_tokens <= 32 * 1024:
        return 0.75
    if max_context_tokens <= 256 * 1024:
        return 0.80
    return 200_000 / max_context_tokens
