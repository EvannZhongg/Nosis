"""Execution of one model-emitted batch of tool calls."""

from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Callable

from .content import ImagePart
from .session import Session, ToolExecutionStatus
from .tool_result import ToolResultNormalizer
from .tools import ToolCall, ToolResult, ToolSet
from .turn_control import AgentCancelled


ToolCallCallback = Callable[[ToolCall, int, int], None]
ToolResultCallback = Callable[[ToolResult, int, int], None]
ToolMediaCallback = Callable[[tuple[ImagePart, ...]], None]


class ToolBatchExecutor:
    """Execute, journal, normalize, and clean up a tool-call batch.

    Calls run concurrently only when every tool in a multi-call batch opts
    into concurrency. Tool messages are journaled as calls finish, while the
    ContextManager remains responsible for presenting them to providers in
    the model's original call order.
    """

    def __init__(
        self,
        tools: ToolSet,
        session: Session,
        result_normalizer: ToolResultNormalizer,
        now: Callable[[], datetime],
    ) -> None:
        self._tools = tools
        self._session = session
        self._result_normalizer = result_normalizer
        self._now = now

    def execute(
        self,
        calls: tuple[ToolCall, ...],
        *,
        turn_id: str,
        on_call: ToolCallCallback | None = None,
        on_result: ToolResultCallback | None = None,
        on_media: ToolMediaCallback | None = None,
    ) -> None:
        indexed_calls = tuple(enumerate(calls, start=1))
        completed: dict[int, tuple[ToolCall, ToolResult]] = {}

        try:
            if self._is_concurrent_batch(calls):
                self._execute_concurrently(
                    indexed_calls,
                    completed,
                    turn_id,
                    on_call,
                    on_result,
                )
            else:
                self._execute_sequentially(
                    indexed_calls,
                    completed,
                    turn_id,
                    on_call,
                    on_result,
                )

            executed = tuple(
                completed[index][1] for index, _call in indexed_calls
            )
            self._commit_media(executed, on_media)
        finally:
            self._cleanup_results(completed)

    def _is_concurrent_batch(self, calls: tuple[ToolCall, ...]) -> bool:
        return len(calls) > 1 and all(
            self._tools.is_concurrent(call.name) for call in calls
        )

    def _execute_sequentially(
        self,
        indexed_calls: tuple[tuple[int, ToolCall], ...],
        completed: dict[int, tuple[ToolCall, ToolResult]],
        turn_id: str,
        on_call: ToolCallCallback | None,
        on_result: ToolResultCallback | None,
    ) -> None:
        tool_count = len(indexed_calls)
        active_call: ToolCall | None = None
        try:
            for index, call in indexed_calls:
                active_call = call
                self._session.tool_started(call, turn_id)
                if on_call is not None:
                    on_call(call, index, tool_count)
                result = self._tools.execute(call)
                completed[index] = (call, result)
                self._finish_call(call, result, turn_id)
                active_call = None
                self._commit_normalized_result(
                    call, result, index, tool_count, on_result
                )
        except BaseException as error:
            if active_call is not None:
                self._session.tool_finished(
                    active_call,
                    _failure_status(error),
                    error=str(error),
                    turn_id=turn_id,
                )
            raise

    def _execute_concurrently(
        self,
        indexed_calls: tuple[tuple[int, ToolCall], ...],
        completed: dict[int, tuple[ToolCall, ToolResult]],
        turn_id: str,
        on_call: ToolCallCallback | None,
        on_result: ToolResultCallback | None,
    ) -> None:
        tool_count = len(indexed_calls)
        with ThreadPoolExecutor(max_workers=tool_count) as executor:
            futures: dict[Future[ToolResult], tuple[int, ToolCall]] = {}
            first_error: BaseException | None = None

            # Invocation events retain model call order. Completion events
            # are emitted as workers finish, without waiting on earlier calls.
            for index, call in indexed_calls:
                self._session.tool_started(call, turn_id)
                try:
                    if on_call is not None:
                        on_call(call, index, tool_count)
                    future = executor.submit(self._tools.execute, call)
                except BaseException as error:
                    self._session.tool_finished(
                        call,
                        _failure_status(error),
                        error=str(error),
                        turn_id=turn_id,
                    )
                    first_error = error
                    break
                futures[future] = (index, call)

            observed: set[int] = set()

            def settle(future: Future[ToolResult]) -> None:
                nonlocal first_error
                index, call = futures[future]
                observed.add(index)
                try:
                    result = future.result()
                except BaseException as error:
                    self._session.tool_finished(
                        call,
                        _failure_status(error),
                        error=str(error),
                        turn_id=turn_id,
                    )
                    if first_error is None:
                        first_error = error
                    return

                completed[index] = (call, result)
                try:
                    self._finish_call(call, result, turn_id)
                    self._commit_normalized_result(
                        call, result, index, tool_count, on_result
                    )
                except BaseException as error:
                    if first_error is None:
                        first_error = error

            try:
                for future in as_completed(futures):
                    settle(future)
            except BaseException as error:
                # A coordinator interruption cannot safely stop running
                # tools. Drain them so every resulting fact reaches Journal.
                first_error = first_error or error
            finally:
                for future, (index, _call) in futures.items():
                    if index not in observed:
                        settle(future)

            if first_error is not None:
                raise first_error

    def _finish_call(
        self,
        call: ToolCall,
        result: ToolResult,
        turn_id: str,
    ) -> None:
        status: ToolExecutionStatus = (
            "failed" if result.error is not None else "completed"
        )
        self._session.tool_finished(
            call,
            status,
            error=result.error.message if result.error is not None else None,
            turn_id=turn_id,
        )

    def _commit_normalized_result(
        self,
        call: ToolCall,
        result: ToolResult,
        index: int,
        tool_count: int,
        on_result: ToolResultCallback | None,
    ) -> None:
        normalized_content = self._result_normalizer.normalize(result)
        self._session.add_item(
            "tool",
            normalized_content,
            self._now().astimezone(timezone.utc),
            tool_call_id=call.id,
        )
        if on_result is not None:
            on_result(result, index, tool_count)

    def _commit_media(
        self,
        results: tuple[ToolResult, ...],
        on_media: ToolMediaCallback | None,
    ) -> None:
        # Images follow all tool messages. OpenAI-compatible providers reject
        # a batch when another message interrupts its tool-result sequence.
        media = tuple(
            attachment
            for result in results
            for attachment in result.attachments
        )
        if not media:
            return
        self._session.add_item(
            role="user",
            content=_media_notice(media),
            timestamp_utc=self._now().astimezone(timezone.utc),
            attachments=media,
            origin="tool_media",
        )
        if on_media is not None:
            on_media(media)

    @staticmethod
    def _cleanup_results(
        completed: dict[int, tuple[ToolCall, ToolResult]],
    ) -> None:
        for _call, result in completed.values():
            if result.artifact_cleanup is not None:
                result.artifact_cleanup()


def _failure_status(error: BaseException) -> ToolExecutionStatus:
    if isinstance(error, AgentCancelled):
        return "cancelled"
    if isinstance(error, KeyboardInterrupt):
        # SIGINT may arrive after an irreversible side effect but before the
        # tool returns, so completion cannot be inferred.
        return "unknown"
    return "failed"


def _media_notice(attachments: tuple[ImagePart, ...]) -> str:
    """Describe images as output, not as a new instruction from the user."""
    listed = "\n".join(f"- {part.path}" for part in attachments)
    noun = "image" if len(attachments) == 1 else "images"
    return (
        f"[Tool output] The {noun} requested by the preceding tool "
        f"call:\n{listed}"
    )
