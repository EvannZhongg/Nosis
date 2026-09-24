import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, TypeAlias

from .config import AgentConfig
from .context_manager import ContextManager, ContextWindow, ContextWindowExceededError
from .llm import (
    LLMProvider,
    LLMRequest,
    LLMResponse,
    with_generation_limit,
)
from .session import Message, Session
from .content import AttachmentPart, ImagePart
from .errors import ProviderProtocolError, runtime_error_info
from .tool_result import ToolResultNormalizer
from .tool_batch import ToolBatchExecutor
from .tools import ToolCall, ToolExecutionContext, ToolResult, ToolSet
from .turn_control import AgentCancelled, TurnControl, UserSteer


class ToolCallLimitExceededError(RuntimeError):
    def __init__(self, tool_name: str, limit: int) -> None:
        self.tool_name = tool_name
        self.limit = limit
        super().__init__(
            f"tool call '{tool_name}' exceeded the maximum of "
            f"{limit} identical consecutive executions"
        )


@dataclass(frozen=True)
class AgentRunResult:
    request: LLMRequest
    response: LLMResponse
    items: tuple[Message, ...]
    user_input: str
    request_timestamp_utc: datetime
    response_timestamp_utc: datetime


@dataclass(frozen=True)
class AssistantMessageDeltaEvent:
    text: str
    model_call_index: int

@dataclass(frozen=True)
class ReasoningDeltaEvent:
    text: str
    model_call_index: int


@dataclass(frozen=True)
class AssistantMessageEvent:
    content: str
    timestamp_utc: datetime
    model_call_index: int


@dataclass(frozen=True)
class ToolBatchStartedEvent:
    model_call_index: int
    tool_calls: tuple[ToolCall, ...]


@dataclass(frozen=True)
class ToolCallEvent:
    tool_call: ToolCall
    tool_index: int
    tool_count: int


@dataclass(frozen=True)
class ToolResultEvent:
    tool_result: ToolResult
    tool_index: int
    tool_count: int


@dataclass(frozen=True)
class ToolMediaEvent:
    """Images a tool batch placed into the model's context."""

    attachments: tuple[ImagePart, ...]


@dataclass(frozen=True)
class ContextWindowEvent:
    window: ContextWindow


@dataclass(frozen=True)
class UserSteerAppliedEvent:
    steer_id: str
    text: str
    timestamp_utc: datetime


@dataclass(frozen=True)
class JobStatusEvent:
    job_id: str
    kind: str
    status: str


AgentEvent: TypeAlias = (
    AssistantMessageDeltaEvent
    | ReasoningDeltaEvent
    | AssistantMessageEvent
    | ToolBatchStartedEvent
    | ToolCallEvent
    | ToolResultEvent
    | ToolMediaEvent
    | ContextWindowEvent
    | UserSteerAppliedEvent
    | JobStatusEvent
)


class Agent:
    """One conversation loop over one Session and one ToolSet.

    An Agent owns nothing shared: its ToolSet is a view of the Runtime's
    catalog, and its context carries the Runtime dependencies that the
    tools need.
    """

    def __init__(
        self,
        provider: LLMProvider,
        session: Session,
        system_prompt: str,
        consolidator_prompt: str,
        config: AgentConfig,
        tools: ToolSet,
        context: ToolExecutionContext,
        now: Callable[[], datetime] | None = None,
        tool_result_normalizer: ToolResultNormalizer | None = None,
    ) -> None:
        self._provider = provider
        self._session = session
        self._config = config
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._tools = tools
        self._execution_context = context
        tool_result_normalizer = (
            tool_result_normalizer
            or ToolResultNormalizer(
                context.workspace,
                session.session_id,
                sessions_directory=context.sessions_directory,
            )
        )
        if context.jobs is not None:
            context.jobs.set_result_normalizer(tool_result_normalizer)
        self._tool_batch_executor = ToolBatchExecutor(
            tools=tools,
            session=session,
            result_normalizer=tool_result_normalizer,
            now=self._now,
        )
        self._context = ContextManager(
            provider=provider,
            session=session,
            system_prompt=system_prompt,
            consolidator_prompt=consolidator_prompt,
            config=config,
            media_root=context.workspace.path,
        )

    def context_window(self) -> ContextWindow:
        request = self._context.build_request(self._tools.definitions)
        return self._context.window(self._provider.count_input_tokens(request))

    def run(
        self,
        user_input: str,
        on_event: Callable[[AgentEvent], None] | None = None,
        attachments: tuple[AttachmentPart, ...] = (),
        turn_id: str | None = None,
        turn_control: TurnControl | None = None,
    ) -> AgentRunResult:
        try:
            result = self._run(
                user_input,
                on_event,
                attachments,
                turn_id,
                turn_control,
            )
        except KeyboardInterrupt:
            self._cancel_jobs(self._session.current_turn_id)
            self._session.finish_turn("cancelled")
            raise
        except Exception as error:
            self._cancel_jobs(self._session.current_turn_id)
            self._session.finish_turn(
                "failed",
                error=runtime_error_info(error),
            )
            raise
        except BaseException as error:
            self._cancel_jobs(self._session.current_turn_id)
            self._session.finish_turn(
                "cancelled"
                if (
                    isinstance(error, AgentCancelled)
                    or (turn_control is not None and turn_control.cancelled)
                )
                else "interrupted",
                error=runtime_error_info(error),
            )
            raise
        self._session.finish_turn("completed")
        return result

    def _cancel_jobs(self, turn_id: str | None) -> None:
        if turn_id is not None and self._execution_context.jobs is not None:
            self._execution_context.jobs.cancel_and_discard(turn_id)

    def _run(
        self,
        user_input: str,
        on_event: Callable[[AgentEvent], None] | None = None,
        attachments: tuple[AttachmentPart, ...] = (),
        turn_id: str | None = None,
        turn_control: TurnControl | None = None,
    ) -> AgentRunResult:
        run_item_start = len(self._session.items)
        turn_id = self._session.begin_turn(turn_id)
        model_call_index = 0
        previous_tool_call_key: tuple[str, str] | None = None
        identical_tool_calls = 0
        request_timestamp_utc = self._now().astimezone(timezone.utc)

        self._session.add_item(
            "user",
            user_input,
            timestamp_utc=request_timestamp_utc,
            attachments=attachments,
        )
        while True:
            _raise_if_cancelled(turn_control)
            applied_jobs, _pending_jobs = _apply_job_results(
                self._session,
                self._execution_context,
                turn_id,
                self._now,
            )
            if applied_jobs:
                previous_tool_call_key = None
                identical_tool_calls = 0
            request = self._context.build_request(self._tools.definitions)
            input_tokens = self._provider.count_input_tokens(request)
            if on_event is not None:
                on_event(ContextWindowEvent(self._context.window(input_tokens)))
            if self._context.should_archive(input_tokens):
                self._context.archive()
                _apply_steering(
                    self._session,
                    turn_control,
                    self._now,
                    on_event,
                )
                continue
            if input_tokens > self._context.hard_limit:
                raise ContextWindowExceededError(
                    input_tokens=input_tokens,
                    max_context_tokens=self._provider.max_context_tokens,
                    output_reserve_tokens=(
                        self._config.output_reserve_tokens
                    ),
                )
            request = with_generation_limit(
                request,
                self._provider,
                self._config.max_generation_tokens,
            )
            model_call_index += 1

            def on_text_delta(
                text: str,
                model_call_index: int = model_call_index,
            ) -> None:
                if on_event is not None:
                    on_event(
                        AssistantMessageDeltaEvent(
                            text=text,
                            model_call_index=model_call_index,
                        )
                    )

            def on_reasoning_delta(text: str, model_call_index: int = model_call_index) -> None:
                if on_event is not None:
                    on_event(ReasoningDeltaEvent(text=text, model_call_index=model_call_index))

            try:
                response = self._provider.stream_cancellable(
                    request,
                    on_text_delta,
                    on_reasoning_delta,
                    lambda: _raise_if_cancelled(turn_control),
                )
            except ProviderProtocolError as error:
                raise ProviderProtocolError(
                    str(error),
                    details={
                        **error.details,
                        "model_call_index": model_call_index,
                    },
                ) from error
            usage = response.usage
            self._session.model_completed(
                model_call_index,
                input_tokens=(usage.input_tokens if usage is not None else None),
                output_tokens=(
                    usage.output_tokens if usage is not None else None
                ),
                total_tokens=(usage.total_tokens if usage is not None else None),
                has_tool_calls=bool(response.tool_calls),
            )
            _raise_if_cancelled(turn_control)

            if response.tool_calls:
                next_tool_call_key = previous_tool_call_key
                next_identical_calls = identical_tool_calls
                for tool_call in response.tool_calls:
                    tool_call_key = _tool_call_key(tool_call)
                    if tool_call_key == next_tool_call_key:
                        next_identical_calls += 1
                    else:
                        next_tool_call_key = tool_call_key
                        next_identical_calls = 1

                    if (
                        next_identical_calls
                        > self._config.max_same_tool_calls
                    ):
                        raise ToolCallLimitExceededError(
                            tool_call.name,
                            self._config.max_same_tool_calls,
                        )
                previous_tool_call_key = next_tool_call_key
                identical_tool_calls = next_identical_calls

                assistant_timestamp_utc = self._now().astimezone(timezone.utc)
                self._session.add_item(
                    role="assistant",
                    content=response.content,
                    timestamp_utc=assistant_timestamp_utc,
                    tool_calls=response.tool_calls,
                    reasoning=response.reasoning,
                )
                if response.content and on_event is not None:
                    on_event(
                        AssistantMessageEvent(
                            content=response.content,
                            timestamp_utc=assistant_timestamp_utc,
                            model_call_index=model_call_index,
                        )
                    )
                if on_event is not None:
                    on_event(
                        ToolBatchStartedEvent(
                            model_call_index=model_call_index,
                            tool_calls=response.tool_calls,
                        )
                    )

                def emit_call(
                    call: ToolCall,
                    index: int,
                    tool_count: int,
                ) -> None:
                    if on_event is not None:
                        on_event(
                            ToolCallEvent(
                                tool_call=call,
                                tool_index=index,
                                tool_count=tool_count,
                            )
                        )

                def emit_result(
                    result: ToolResult,
                    index: int,
                    tool_count: int,
                ) -> None:
                    if on_event is not None:
                        on_event(
                            ToolResultEvent(
                                tool_result=result,
                                tool_index=index,
                                tool_count=tool_count,
                            )
                        )

                def emit_media(media: tuple[ImagePart, ...]) -> None:
                    if on_event is not None:
                        on_event(ToolMediaEvent(attachments=media))

                self._tool_batch_executor.execute(
                    response.tool_calls,
                    turn_id=turn_id,
                    on_call=emit_call,
                    on_result=emit_result,
                    on_media=emit_media,
                )
                _raise_if_cancelled(turn_control)
                applied_jobs, _pending_jobs = _apply_job_results(
                    self._session,
                    self._execution_context,
                    turn_id,
                    self._now,
                )
                if applied_jobs:
                    previous_tool_call_key = None
                    identical_tool_calls = 0
                if _apply_steering(
                    self._session,
                    turn_control,
                    self._now,
                    on_event,
                ):
                    previous_tool_call_key = None
                    identical_tool_calls = 0
                continue

            if response.content is None:
                if response.reasoning:
                    raise ValueError(
                        "LLM response contained reasoning but no final content "
                        "or tool calls; the model may have exhausted its output "
                        "token limit before producing an answer"
                    )
                raise ValueError(
                    "LLM response must contain content or tool calls"
                )

            response_timestamp_utc = self._now().astimezone(timezone.utc)
            self._session.add_item(
                "assistant",
                response.content,
                timestamp_utc=response_timestamp_utc,
                reasoning=response.reasoning,
            )
            if on_event is not None:
                on_event(
                    AssistantMessageEvent(
                        content=response.content,
                        timestamp_utc=response_timestamp_utc,
                        model_call_index=model_call_index,
                    )
                )
                on_event(ContextWindowEvent(self.context_window()))
            applied_jobs, pending_jobs = _apply_job_results(
                self._session,
                self._execution_context,
                turn_id,
                self._now,
            )
            if applied_jobs:
                previous_tool_call_key = None
                identical_tool_calls = 0
                continue
            jobs = self._execution_context.jobs
            if jobs is not None and pending_jobs:
                while jobs.has_pending(turn_id):
                    _raise_if_cancelled(turn_control)
                    if jobs.wait_for_update(turn_id):
                        break
                    if _apply_steering(
                        self._session,
                        turn_control,
                        self._now,
                        on_event,
                    ):
                        previous_tool_call_key = None
                        identical_tool_calls = 0
                        break
                _raise_if_cancelled(turn_control)
                continue
            if turn_control is not None:
                steers = turn_control.finish()
                if steers:
                    _append_steering(
                        self._session,
                        steers,
                        self._now,
                        on_event,
                    )
                    previous_tool_call_key = None
                    identical_tool_calls = 0
                    continue
            return AgentRunResult(
                request=request,
                response=response,
                items=tuple(self._session.items[run_item_start:]),
                user_input=user_input,
                request_timestamp_utc=request_timestamp_utc,
                response_timestamp_utc=response_timestamp_utc,
            )


def _raise_if_cancelled(turn_control: TurnControl | None) -> None:
    if turn_control is not None and turn_control.cancelled:
        raise AgentCancelled


def _apply_steering(
    session: Session,
    turn_control: TurnControl | None,
    now: Callable[[], datetime],
    on_event: Callable[[AgentEvent], None] | None,
) -> bool:
    if turn_control is None:
        return False
    steers = turn_control.drain_steering()
    if not steers:
        return False
    _append_steering(session, steers, now, on_event)
    return True


def _append_steering(
    session: Session,
    steers: tuple[UserSteer, ...],
    now: Callable[[], datetime],
    on_event: Callable[[AgentEvent], None] | None,
) -> None:
    for steer in steers:
        timestamp_utc = now().astimezone(timezone.utc)
        session.add_item(
            "user",
            steer.text,
            timestamp_utc=timestamp_utc,
            user_source="steering",
        )
        if on_event is not None:
            on_event(
                UserSteerAppliedEvent(
                    steer_id=steer.steer_id,
                    text=steer.text,
                    timestamp_utc=timestamp_utc,
                )
            )


def _apply_job_results(
    session: Session,
    context: ToolExecutionContext,
    turn_id: str,
    now: Callable[[], datetime],
) -> tuple[bool, bool]:
    jobs = context.jobs
    if jobs is None:
        return False, False
    terminal, pending = jobs.drain_terminal_state(turn_id)
    if not terminal:
        return False, pending
    blocks = []
    for update in terminal:
        if update.error is not None:
            normalized = ToolResult(
                tool_call_id=update.job_id,
                name=update.kind,
                error=update.error,
            ).to_content()
        elif update.status == "cancelled":
            normalized = json.dumps(
                {"ok": False, "error": {"type": "cancelled"}},
                ensure_ascii=False,
            )
        else:
            normalized = (
                update.output
                if isinstance(update.output, str)
                else json.dumps(update.output, ensure_ascii=False)
            )
        blocks.append(
            "[Background job result]\n"
            f"job_id: {update.job_id}\n"
            f"kind: {update.kind}\n"
            f"status: {update.status}\n"
            f"result: {normalized}"
        )
    session.add_item(
        "user",
        "\n\n".join(blocks),
        timestamp_utc=now().astimezone(timezone.utc),
        origin="job_result",
    )
    return True, pending


def _tool_call_key(tool_call: ToolCall) -> tuple[str, str]:
    normalized_arguments = json.dumps(
        tool_call.arguments,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return tool_call.name, normalized_arguments
