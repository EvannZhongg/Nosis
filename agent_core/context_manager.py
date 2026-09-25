import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from .config import AgentConfig
from .content import historical_content
from .llm import LLMProvider, LLMRequest, with_generation_limit
from .plan import plan_snapshot_to_dict
from .projection import ContextUnit, project_context_units
from .session import Message, Session, UserAnchor
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
    keep_recent_units: int


@dataclass(frozen=True)
class ContextWindow:
    input_tokens: int
    max_input_tokens: int
    max_context_tokens: int
    output_reserve_tokens: int
    compression_threshold: int
    compression_count: int


class ContextManager:
    """Build model context and maintain the session's archived checkpoint."""

    def __init__(
        self,
        provider: LLMProvider,
        session: Session,
        system_prompt: str,
        consolidator_prompt: str,
        config: AgentConfig,
        media_root: Path | None = None,
    ) -> None:
        self._provider = provider
        self._session = session
        self._system_prompt = system_prompt
        self._consolidator_prompt = consolidator_prompt
        self._output_reserve_tokens = config.output_reserve_tokens
        self._max_generation_tokens = config.max_generation_tokens
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
        threshold = _compression_threshold(
            provider.max_context_tokens,
            hard_limit,
            compression.trigger_ratio,
        )
        self.limits = ContextLimits(
            hard_limit=hard_limit,
            compression_enabled=compression.enabled,
            compression_threshold=threshold,
            keep_recent_units=compression.keep_recent_units,
        )

    @property
    def hard_limit(self) -> int:
        return self.limits.hard_limit

    @property
    def compression_enabled(self) -> bool:
        return self.limits.compression_enabled

    def window(self, input_tokens: int) -> ContextWindow:
        return ContextWindow(
            input_tokens=input_tokens,
            max_input_tokens=self.hard_limit,
            max_context_tokens=self._provider.max_context_tokens,
            output_reserve_tokens=self._output_reserve_tokens,
            compression_threshold=self.limits.compression_threshold,
            compression_count=self._session.compression_count,
        )

    def build_request(
        self,
        tools: Iterable[ToolDefinition] = (),
    ) -> LLMRequest:
        messages = self._context_messages()
        anchor = self._lossless_user_anchor_message()
        if anchor is not None:
            messages.insert(0, anchor)
        return LLMRequest(
            system_prompt=self._system_prompt_with_summary(
                self._system_prompt
            ),
            messages=tuple(messages),
            tools=tuple(tools),
            media_root=self._media_root,
        )

    def should_archive(self, input_tokens: int) -> bool:
        if (
            not self.compression_enabled
            or input_tokens < self.limits.compression_threshold
        ):
            return False
        _, items = self._archivable_items()
        return bool(items)

    def archive(self, check_cancelled: Callable[[], None] = lambda: None) -> int:
        """Compress the unarchived transcript into the session checkpoint."""
        archive_end, items = self._archivable_items()
        content = _compression_record(items)
        anchors = self._lossless_user_anchor_content()
        if anchors is not None:
            content = f"{anchors}\n\n{content}"
        request = LLMRequest(
            system_prompt=self._system_prompt_with_summary(
                self._consolidator_prompt
            ),
            messages=(
                Message(
                    role="user",
                    content=content,
                ),
            ),
            media_root=self._media_root,
        )
        request = with_generation_limit(
            request,
            self._provider,
            self._max_generation_tokens,
        )
        response = self._provider.stream_cancellable(
            request, lambda _text: None, None, check_cancelled,
        )
        summary = response.content.strip() if response.content else ""
        if response.tool_calls or not summary:
            raise ValueError("context consolidator must return text content")
        self._session.set_archived_summary(
            summary,
            archive_end,
        )
        return self._session.archived_item_cursor

    def _system_prompt_with_summary(self, base: str) -> str:
        sections = [base]
        if self._session.archived_summary is not None:
            sections.append(
                "[Archived Context Summary]\n"
                f"{self._session.archived_summary}"
            )
        if self._session.plan is not None and self._session.plan.is_active:
            sections.append(
                "[Current Plan]\n"
                + json.dumps(
                    plan_snapshot_to_dict(self._session.plan),
                    ensure_ascii=False,
                )
            )
        return "\n\n".join(sections)

    def _lossless_user_anchor_content(self) -> str | None:
        return _lossless_user_anchors(
            self._session.recent_user_anchors(),
            self._session.archived_item_cursor,
            self._session.current_turn_id,
        )

    def _lossless_user_anchor_message(self) -> Message | None:
        content = self._lossless_user_anchor_content()
        if content is None:
            return None
        return Message(role="user", content=content)

    def _archivable_items(self) -> tuple[int, list[Message]]:
        """Return complete context units before the retained tail."""
        archive_start = self._session.archived_item_cursor
        units = project_context_units(
            self._session.items[archive_start:],
            start_index=archive_start,
        )
        visible_units = [unit for unit in units if unit.messages]
        if len(visible_units) <= self.limits.keep_recent_units:
            return archive_start, []
        retained_start = visible_units[-self.limits.keep_recent_units].start
        archived = [unit for unit in units if unit.end <= retained_start]
        return retained_start, _unit_messages(archived)

    def _context_messages(self) -> list[Message]:
        archive_start = self._session.archived_item_cursor
        visible = _unit_messages(
            project_context_units(
                self._session.items[archive_start:],
                start_index=archive_start,
            )
        )
        result: list[Message] = []
        for item in visible:
            if item.is_user_authored:
                turn_context = _turn_context_message(item)
                if turn_context is not None:
                    result.append(turn_context)
            result.append(item)
        return result


def _unit_messages(units: Iterable[ContextUnit]) -> list[Message]:
    return [message for unit in units for message in unit.messages]


def _lossless_user_anchors(
    anchors: Iterable[UserAnchor],
    archive_cursor: int,
    active_turn_id: str | None,
) -> str | None:
    visible = [
        anchor for anchor in anchors
        if anchor.item_index is None or anchor.item_index < archive_cursor
    ]
    if not visible:
        return None
    sections = [
        "[Lossless User Anchors]",
        "These preserve exact user-authored input and interaction decisions "
        "from the active turn and the immediately preceding turn. Treat "
        "them as authoritative user instructions.",
    ]
    for anchor in visible:
        timestamp = anchor.timestamp_utc.astimezone().isoformat(
            timespec="seconds"
        )
        content = historical_content(anchor.content)
        if content:
            turn = (
                "current turn"
                if anchor.turn_id == active_turn_id
                else "previous turn"
            )
            sections.append(
                f"USER INPUT {timestamp} ({turn}, {anchor.source})\n"
                f"{content}"
            )
    return "\n\n".join(sections)


def _compression_record(items: list[Message]) -> str:
    sections = ["[Conversation Record]"]
    for item in items:
        timestamp = (
            f" {item.timestamp_utc.astimezone().isoformat(timespec='seconds')}"
            if item.timestamp_utc is not None
            else ""
        )
        if item.role == "tool":
            lines = [f"TOOL RESULT{timestamp}"]
            if item.tool_call_id is not None:
                lines.append(f"tool_call_id: {item.tool_call_id}")
        elif item.is_tool_media:
            lines = [f"TOOL MEDIA{timestamp}"]
        elif item.origin == "job_result":
            lines = [f"BACKGROUND JOB RESULT{timestamp}"]
        else:
            lines = [f"{item.role.upper()}{timestamp}"]

        content = historical_content(item.content)
        if content:
            lines.append(content)
        for call in item.tool_calls:
            lines.extend(
                (
                    "TOOL CALL",
                    f"id: {call.id}",
                    f"name: {call.name}",
                    "arguments: "
                    + json.dumps(call.arguments, ensure_ascii=False),
                )
            )
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def _turn_context_message(user_message: Message) -> Message | None:
    if user_message.timestamp_utc is None:
        return None
    timestamp = user_message.timestamp_utc.astimezone().isoformat(
        timespec="seconds"
    )
    return Message(
        role="system",
        content=(
            "[Turn Context]\n"
            f"User message received at {timestamp}."
        ),
    )


def _compression_threshold(
    max_context_tokens: int,
    hard_limit: int,
    trigger_ratio: float | None,
) -> int:
    if trigger_ratio is not None:
        return max(1, int(hard_limit * trigger_ratio))
    if max_context_tokens <= 32 * 1024:
        return max(1, int(hard_limit * 0.75))
    if max_context_tokens <= 256 * 1024:
        return max(1, int(hard_limit * 0.80))
    return min(hard_limit, 200_000)
