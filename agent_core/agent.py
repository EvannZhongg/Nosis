import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, TypeAlias

from .config import AgentConfig
from .context_manager import ContextManager, ContextWindowExceededError
from .llm import (
    LLMProvider,
    LLMRequest,
    LLMResponse,
    with_generation_limit,
)
from .session import Message, Session, ToolExecutionStatus
from .content import ImagePart
from .tool_result import ToolResultNormalizer
from .tools import ToolCall, ToolExecutionContext, ToolResult, ToolSet
from .turn_control import TurnControl, UserSteer


class ToolCallLimitExceededError(RuntimeError):
    def __init__(self, tool_name: str, limit: int) -> None:
        self.tool_name = tool_name
        self.limit = limit
        super().__init__(
            f"tool call '{tool_name}' exceeded the maximum of "
            f"{limit} identical consecutive executions"
        )


class AgentCancelled(BaseException):
    """Unwind the Runtime when the interface cancels active work."""


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
class ContextArchivedEvent:
    checkpoint_number: int


@dataclass(frozen=True)
class UserSteerAppliedEvent:
    steer_id: str
    text: str
    timestamp_utc: datetime


AgentEvent: TypeAlias = (
    AssistantMessageDeltaEvent
    | ReasoningDeltaEvent
    | AssistantMessageEvent
    | ToolBatchStartedEvent
    | ToolCallEvent
    | ToolResultEvent
    | ToolMediaEvent
    | ContextArchivedEvent
    | UserSteerAppliedEvent
)


def _media_notice(attachments: tuple[ImagePart, ...]) -> str:
    """Describe the images that follow in the same message.

    The model is told these came from its own tool call so it does not
    read them as a new instruction from the person it is talking to.
    """
    listed = "\n".join(f"- {part.path}" for part in attachments)
    noun = "image" if len(attachments) == 1 else "images"
    return (
        f"[Tool output] The {noun} requested by the preceding tool "
        f"call:\n{listed}"
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
        self._tool_result_normalizer = (
            tool_result_normalizer
            or ToolResultNormalizer(
                context.workspace,
                session.session_id,
                sessions_directory=context.sessions_directory,
            )
        )
        self._context = ContextManager(
            provider=provider,
            session=session,
            system_prompt=system_prompt,
            config=config,
            media_root=context.workspace.path,
        )

    def run(
        self,
        user_input: str,
        on_event: Callable[[AgentEvent], None] | None = None,
        attachments: tuple[ImagePart, ...] = (),
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
            self._session.finish_turn("cancelled")
            raise
        except Exception as error:
            self._session.finish_turn(
                "failed",
                error=str(error),
            )
            raise
        except BaseException as error:
            self._session.finish_turn(
                "cancelled"
                if isinstance(error, AgentCancelled)
                else "interrupted",
                error=str(error),
            )
            raise
        self._session.finish_turn("completed")
        return result

    def _run(
        self,
        user_input: str,
        on_event: Callable[[AgentEvent], None] | None = None,
        attachments: tuple[ImagePart, ...] = (),
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
            request = self._context.build_request(self._tools.definitions)
            input_tokens = self._provider.count_input_tokens(request)
            if self._context.should_archive(input_tokens):
                checkpoint_number = self._context.archive()
                if on_event is not None:
                    on_event(ContextArchivedEvent(checkpoint_number))
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

            response = self._provider.stream_cancellable(
                request,
                on_text_delta,
                on_reasoning_delta,
                lambda: _raise_if_cancelled(turn_control),
            )
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
                tool_count = len(response.tool_calls)

                def emit_call(call: ToolCall, index: int) -> None:
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
                ) -> None:
                    if on_event is not None:
                        on_event(
                            ToolResultEvent(
                                tool_result=result,
                                tool_index=index,
                                tool_count=tool_count,
                            )
                        )

                indexed_calls = list(enumerate(response.tool_calls, start=1))
                completed: dict[int, tuple[ToolCall, ToolResult]] = {}
                # Only tools that declare themselves concurrent may share a
                # batch across threads; they hold no invocation state, so
                # parallel calls cannot observe each other.
                if len(indexed_calls) > 1 and all(
                    self._tools.is_concurrent(call.name)
                    for _, call in indexed_calls
                ):
                    with ThreadPoolExecutor(
                        max_workers=len(indexed_calls)
                    ) as executor:
                        futures = {}
                        first_error: BaseException | None = None
                        # Invocation events retain the model's call order.
                        # Completion events are emitted later by
                        # as_completed(), without waiting for an earlier
                        # invocation that is still running.
                        for index, call in indexed_calls:
                            self._session.tool_started(call, turn_id)
                            try:
                                future = executor.submit(
                                    self._tools.execute, call
                                )
                            except BaseException as error:
                                self._session.tool_finished(
                                    call,
                                    _tool_failure_status(error),
                                    error=str(error),
                                    turn_id=turn_id,
                                )
                                first_error = error
                                break
                            futures[future] = (index, call)

                        observed: set[int] = set()

                        try:
                            for index, call in futures.values():
                                emit_call(call, index)
                        except BaseException as error:
                            first_error = error

                        def settle_future(future) -> None:
                            nonlocal first_error
                            index, call = futures[future]
                            observed.add(index)
                            try:
                                result = future.result()
                            except BaseException as error:
                                self._session.tool_finished(
                                    call,
                                    _tool_failure_status(error),
                                    error=str(error),
                                    turn_id=turn_id,
                                )
                                if first_error is None:
                                    first_error = error
                                return

                            completed[index] = (call, result)
                            status = (
                                "failed"
                                if result.error is not None
                                else "completed"
                            )
                            self._session.tool_finished(
                                call,
                                status,
                                error=(
                                    result.error.message
                                    if result.error is not None
                                    else None
                                ),
                                turn_id=turn_id,
                            )
                            try:
                                normalized_content = (
                                    self._tool_result_normalizer.normalize(
                                        result
                                    )
                                )
                                self._session.add_item(
                                    "tool",
                                    normalized_content,
                                    self._now().astimezone(timezone.utc),
                                    tool_call_id=call.id,
                                )
                                emit_result(result, index)
                            except BaseException as error:
                                if first_error is None:
                                    first_error = error

                        try:
                            for future in as_completed(futures):
                                settle_future(future)
                        except BaseException as error:
                            # SIGINT can interrupt the coordinator while tools
                            # are still running.  They cannot be safely killed;
                            # drain them and journal every resulting fact.
                            first_error = first_error or error
                        finally:
                            for future, (index, _call) in futures.items():
                                if index not in observed:
                                    settle_future(future)
                        if first_error is not None:
                            _cleanup_completed_results(completed)
                            raise first_error
                else:
                    active_call: ToolCall | None = None
                    try:
                        for index, call in indexed_calls:
                            active_call = call
                            self._session.tool_started(call, turn_id)
                            emit_call(call, index)
                            result = self._tools.execute(call)
                            completed[index] = (call, result)
                            status = (
                                "failed"
                                if result.error is not None
                                else "completed"
                            )
                            self._session.tool_finished(
                                call,
                                status,
                                error=(
                                    result.error.message
                                    if result.error is not None
                                    else None
                                ),
                                turn_id=turn_id,
                            )
                            active_call = None
                            normalized_content = (
                                self._tool_result_normalizer.normalize(result)
                            )
                            self._session.add_item(
                                "tool",
                                normalized_content,
                                self._now().astimezone(timezone.utc),
                                tool_call_id=call.id,
                            )
                            emit_result(result, index)
                    except BaseException as error:
                        if active_call is not None:
                            self._session.tool_finished(
                                active_call,
                                _tool_failure_status(error),
                                error=str(error),
                                turn_id=turn_id,
                            )
                        _cleanup_completed_results(completed)
                        raise

                # Session commit order is independent from completion order:
                # providers receive one result for each call in the exact
                # order in which the model emitted those calls.
                executed = [
                    (index, completed[index][0], completed[index][1])
                    for index, _ in indexed_calls
                ]
                try:
                    # Images arrive after every tool result, never between
                    # two of them: a provider rejects an assistant step whose
                    # tool calls are not each answered by the message that
                    # follows, so one batch yields at most one media message.
                    media = tuple(
                        attachment
                        for _, _, result in executed
                        for attachment in result.attachments
                    )
                    if media:
                        self._session.add_item(
                            role="user",
                            content=_media_notice(media),
                            timestamp_utc=self._now().astimezone(timezone.utc),
                            attachments=media,
                            origin="tool_media",
                        )
                        if on_event is not None:
                            on_event(ToolMediaEvent(attachments=media))
                finally:
                    for _, _, tool_result in executed:
                        _cleanup_tool_result(tool_result)
                _raise_if_cancelled(turn_control)
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

def _tool_call_key(tool_call: ToolCall) -> tuple[str, str]:
    normalized_arguments = json.dumps(
        tool_call.arguments,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return tool_call.name, normalized_arguments


def _tool_failure_status(error: BaseException) -> ToolExecutionStatus:
    if isinstance(error, AgentCancelled):
        return "cancelled"
    if isinstance(error, KeyboardInterrupt):
        # SIGINT may arrive after an irreversible side effect but before the
        # tool returns, so completion cannot be inferred.
        return "unknown"
    return "failed"


def _cleanup_tool_result(result: ToolResult) -> None:
    if result.artifact_cleanup is not None:
        result.artifact_cleanup()


def _cleanup_completed_results(
    completed: dict[int, tuple[ToolCall, ToolResult]],
) -> None:
    for _, result in completed.values():
        _cleanup_tool_result(result)
