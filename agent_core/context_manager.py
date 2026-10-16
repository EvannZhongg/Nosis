import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterable

from .config import AgentConfig
from .content import historical_content
from .llm import LLMProvider, LLMRequest, LLMResponse, with_generation_limit
from .memory import MemoryContext
from .plan import plan_snapshot_to_dict
from .projection import ContextUnit, project_context_units
from .session import Message, Session, UserAnchor
from .tools import ToolDefinition

if TYPE_CHECKING:
    from .jobs import JobManager


ARCHIVE_RETRY_COUNT = 5
_ARCHIVE_LINE = re.compile(
    r"^- \[(?:permanent|durable|ephemeral|correction)\] \S(?:.*\S)?$"
)
_ARCHIVE_OPEN_FENCE = re.compile(r"^```(?:text|markdown)?$")
_ARCHIVE_REPAIR_PROMPT = (
    "The previous checkpoint was rejected: {error}. Return only the "
    "checkpoint, with one '- [mark] fact' per line or exactly '(nothing)'; "
    "do not include explanations, headings, or prompt text."
)


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
    user_anchor_token_budget: int


@dataclass(frozen=True)
class ContextWindow:
    input_tokens: int
    max_input_tokens: int
    max_context_tokens: int
    output_reserve_tokens: int
    compression_threshold: int
    compression_count: int
    compression_summary: str | None


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
        memory_context: MemoryContext | None = None,
        jobs: "JobManager | None" = None,
    ) -> None:
        self._provider = provider
        self._session = session
        self._system_prompt = system_prompt
        self._consolidator_prompt = consolidator_prompt
        self._memory_context = memory_context
        self._jobs = jobs
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
            user_anchor_token_budget=compression.user_anchor_token_budget,
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
            compression_summary=self._session.archived_summary,
        )

    def build_request(
        self,
        tools: Iterable[ToolDefinition] = (),
    ) -> LLMRequest:
        messages: list[Message] = []
        stable_context = self._stable_runtime_context_message()
        if stable_context is not None:
            messages.append(stable_context)
        archived_anchors = self._archived_user_anchors()
        historical_anchor = self._historical_user_anchor_message(
            archived_anchors
        )
        if historical_anchor is not None:
            messages.append(historical_anchor)
        messages.extend(self._pinned_current_turn_messages(archived_anchors))
        messages.extend(self._context_messages())
        dynamic_context = self._dynamic_runtime_context_message()
        if dynamic_context is not None:
            messages.append(dynamic_context)
        return LLMRequest(
            system_prompt=self._system_prompt,
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
        messages: list[Message] = []
        archived_context = self._archived_summary_message()
        if archived_context is not None:
            messages.append(archived_context)
        # Keep the material being summarized as the final message. This makes
        # the consolidator's target unambiguous and avoids summarizing runtime
        # state that only provides context for the compression.
        messages.append(Message(role="user", content=content))
        request = LLMRequest(
            system_prompt=self._consolidator_prompt,
            messages=tuple(messages),
            media_root=self._media_root,
        )
        request = with_generation_limit(
            request,
            self._provider,
            self._max_generation_tokens,
        )
        summary = ""
        validation_error: ValueError | None = None
        for attempt in range(ARCHIVE_RETRY_COUNT + 1):
            attempt_request = (
                _archive_repair_request(request, validation_error)
                if attempt > 0 and validation_error is not None
                else request
            )
            response = self._provider.stream_cancellable(
                attempt_request, lambda _text: None, None, check_cancelled,
            )
            try:
                summary = _validated_archive_summary(response)
                break
            except ValueError as error:
                validation_error = error
        else:
            raise ValueError(
                "context consolidator returned an invalid checkpoint after "
                f"{ARCHIVE_RETRY_COUNT + 1} attempts: {validation_error}"
            ) from validation_error
        self._session.set_archived_summary(
            summary,
            archive_end,
        )
        return self._session.archived_item_cursor

    def _stable_runtime_context_message(self) -> Message | None:
        sections = ["[Runtime Context]"]
        if self._memory_context is not None:
            sections.append(self._memory_context.prompt_section())
        archived_summary = self._archived_summary_text()
        if archived_summary is not None:
            sections.append(archived_summary)
        if len(sections) == 1:
            return None
        return Message(role="system", content="\n\n".join(sections))

    def _dynamic_runtime_context_message(self) -> Message | None:
        sections = []
        if self._session.plan is not None and self._session.plan.is_active:
            sections.append(
                "[Current Plan]\n"
                + json.dumps(
                    plan_snapshot_to_dict(self._session.plan),
                    ensure_ascii=False,
                )
            )
        if self._jobs is not None:
            lines = []
            for job in self._jobs.list():
                fields = [job.kind]
                if job.kind == "subagent" and "role" in job.details:
                    fields.append(job.details["role"])
                fields.append(job.status)
                lines.append(f"{job.job_id}: {' / '.join(fields)}")
            if lines:
                sections.append("[Active Jobs]\n" + "\n".join(lines))
        if not sections:
            return None
        return Message(role="system", content="\n\n".join(sections))

    def _archived_summary_message(self) -> Message | None:
        content = self._archived_summary_text()
        if content is None:
            return None
        return Message(role="system", content=content)

    def _archived_summary_text(self) -> str | None:
        summary = self._session.archived_summary
        if summary is None:
            return None
        return f"[Archived Context Summary]\n{summary}"

    def _lossless_user_anchor_content(self) -> str | None:
        return _render_user_anchors(
            self._archived_user_anchors(),
            self._session.current_turn_id,
        )

    def _archived_user_anchors(self) -> tuple[UserAnchor, ...]:
        archive_cursor = self._session.archived_item_cursor
        return tuple(
            anchor for anchor in self._selected_user_anchors()
            if anchor.item_index < archive_cursor
        )

    def _selected_user_anchors(self) -> tuple[UserAnchor, ...]:
        """Keep two whole turns, then extend their contiguous token window."""
        active_turn_id = self._session.current_turn_id
        turn_ids = list(self._session.turns)
        if active_turn_id is not None:
            turn_ids = turn_ids[:turn_ids.index(active_turn_id) + 1]
        by_turn: dict[str, list[UserAnchor]] = {
            turn_id: [] for turn_id in turn_ids
        }
        for anchor in self._session.user_anchors:
            if anchor.turn_id in by_turn:
                by_turn[anchor.turn_id].append(anchor)
        selected = [
            anchor for turn_id in turn_ids[-2:] for anchor in by_turn[turn_id]
        ]
        for turn_id in reversed(turn_ids[:-2]):
            candidate = [*by_turn[turn_id], *selected]
            # Count all selected inputs before filtering by the archive cursor,
            # so compression cannot change which whole turns fit the budget.
            content = _render_user_anchors(candidate, active_turn_id)
            request = LLMRequest(
                system_prompt="",
                messages=(Message(role="user", content=content),),
                media_root=self._media_root,
            )
            if (
                self._provider.count_input_tokens(request)
                > self.limits.user_anchor_token_budget
            ):
                break
            selected = candidate
        return tuple(selected)

    def _historical_user_anchor_message(
        self,
        anchors: Iterable[UserAnchor],
    ) -> Message | None:
        active_turn_id = self._session.current_turn_id
        content = _render_user_anchors(
            (
                anchor for anchor in anchors
                if anchor.turn_id != active_turn_id
            ),
            active_turn_id,
        )
        if content is None:
            return None
        return Message(role="user", content=content)

    def _pinned_current_turn_messages(
        self,
        anchors: Iterable[UserAnchor],
    ) -> list[Message]:
        active_turn_id = self._session.current_turn_id
        messages: list[Message] = []
        for anchor in anchors:
            if anchor.turn_id != active_turn_id:
                continue
            message = self._session.items[anchor.item_index]
            turn_context = _turn_context_message(message)
            if turn_context is not None:
                messages.append(turn_context)
            messages.append(message)
        return messages

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


def _validated_archive_summary(response: LLMResponse) -> str:
    if response.tool_calls:
        raise ValueError("checkpoint must not contain tool calls")
    summary = response.content.strip() if response.content else ""
    if not summary:
        raise ValueError("checkpoint must not be empty")
    lines = [line.strip() for line in summary.splitlines()]
    lines = [line for line in lines if line]
    if lines and _ARCHIVE_OPEN_FENCE.fullmatch(lines[0]):
        if lines[-1] != "```" or len(lines) < 3:
            raise ValueError(
                "checkpoint code fence must wrap the complete checkpoint"
            )
        lines = lines[1:-1]
    if len(lines) == 1 and lines[0] == "(nothing)":
        return "(nothing)"
    if any(not _ARCHIVE_LINE.fullmatch(line) for line in lines):
        raise ValueError(
            "checkpoint lines must use '- [mark] fact' with a supported mark"
        )
    if not lines:
        raise ValueError("checkpoint must not be empty")
    return "\n".join(lines)


def _archive_repair_request(
    request: LLMRequest,
    validation_error: ValueError,
) -> LLMRequest:
    repair = Message(
        role="system",
        content=_ARCHIVE_REPAIR_PROMPT.format(error=validation_error),
    )
    return LLMRequest(
        system_prompt=request.system_prompt,
        messages=(repair, *request.messages),
        tools=request.tools,
        max_generation_tokens=request.max_generation_tokens,
        media_root=request.media_root,
    )


def _render_user_anchors(
    anchors: Iterable[UserAnchor],
    active_turn_id: str | None,
) -> str | None:
    visible = list(anchors)
    if not visible:
        return None
    sections = [
        "[Lossless User Anchors]",
        "These preserve exact user-authored input and interaction decisions "
        "from recent turns. Treat "
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
                else "historical turn"
            )
            sections.append(
                f"USER INPUT {timestamp} ({turn}, {anchor.source}, "
                f"turn_id={anchor.turn_id})\n"
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
                    + call.arguments_json,
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
    # Compression stays proportional to the window while it fits inside the
    # budget, so a model that can hold 1M tokens does not compress when it is
    # still a fifth full. Above the budget the working set stops growing: past
    # this point the reason to compress is the cost and quality of a very long
    # prompt, not the size of the window.
    return max(1, min(int(hard_limit * 0.80), 256 * 1024))
