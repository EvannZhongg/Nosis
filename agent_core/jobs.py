"""Turn-scoped background work managed by the Agent Runtime."""

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from threading import Condition, Event, Lock
from typing import Callable, Literal, TypeAlias
from uuid import uuid4

from .session import Session
from .tools.base import JSONValue, ToolError, ToolOutput, ToolResult
from .tool_result import ToolResultNormalizer


JobStatus: TypeAlias = Literal[
    "submitted", "running", "completed", "failed", "cancelled"
]
JobOutput: TypeAlias = JSONValue | ToolOutput


class JobCancelled(BaseException):
    """Raised by cooperative background work after cancellation."""


class CancellationToken:
    def __init__(self) -> None:
        self._event = Event()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise JobCancelled


@dataclass(frozen=True)
class JobHandle:
    job_id: str
    kind: str
    status: JobStatus

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "job_id": self.job_id,
            "kind": self.kind,
            "status": self.status,
        }


@dataclass(frozen=True)
class JobUpdate:
    job_id: str
    turn_id: str
    kind: str
    status: JobStatus
    output: JobOutput = None
    error: ToolError | None = None

    @property
    def terminal(self) -> bool:
        return self.status in {"completed", "failed", "cancelled"}


@dataclass
class _Job:
    job_id: str
    turn_id: str
    kind: str
    token: CancellationToken
    status: JobStatus = "submitted"
    future: Future[None] | None = None


class JobManager:
    """Runs background work and queues facts for Agent safe points.

    Workers may update durable Job state, but only the Agent thread consumes
    outputs and appends provider-visible messages to the parent Session.
    """

    def __init__(self, session: Session, *, max_workers: int = 8) -> None:
        self._session = session
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="nosis-job",
        )
        self._jobs: dict[str, _Job] = {}
        self._updates: list[JobUpdate] = []
        self._condition = Condition(Lock())
        self._closed = False
        self._on_update: Callable[[JobUpdate], None] | None = None
        self._normalizer: ToolResultNormalizer | None = None

    def set_update_callback(
        self, callback: Callable[[JobUpdate], None] | None
    ) -> None:
        with self._condition:
            self._on_update = callback

    def set_result_normalizer(self, normalizer: ToolResultNormalizer) -> None:
        with self._condition:
            self._normalizer = normalizer

    def _publish_locked(self, update: JobUpdate) -> None:
        self._updates.append(update)
        callback = self._on_update
        if callback is not None:
            callback(update)

    def submit(
        self,
        kind: str,
        turn_id: str,
        run: Callable[[CancellationToken], JobOutput],
    ) -> JobHandle:
        job_id = f"job_{uuid4().hex[:12]}"
        job = _Job(job_id, turn_id, kind, CancellationToken())
        with self._condition:
            if self._closed:
                raise RuntimeError("job manager is closed")
            self._jobs[job_id] = job
            self._publish_locked(JobUpdate(job_id, turn_id, kind, "submitted"))
            self._session.job_submitted(job_id, kind, turn_id)
            try:
                future = self._executor.submit(self._run, job, run)
            except BaseException:
                self._jobs.pop(job_id, None)
                self._updates = [
                    update for update in self._updates
                    if update.job_id != job_id
                ]
                raise
            job.future = future
            self._condition.notify_all()
        return JobHandle(job_id, kind, "submitted")

    def _run(
        self,
        job: _Job,
        run: Callable[[CancellationToken], JobOutput],
    ) -> None:
        with self._condition:
            if job.token.cancelled:
                self._finish_locked(job, "cancelled")
                return
            job.status = "running"
            self._session.job_started(job.job_id, job.kind, job.turn_id)
            self._publish_locked(
                JobUpdate(job.job_id, job.turn_id, job.kind, "running")
            )
            self._condition.notify_all()
        try:
            output = run(job.token)
        except JobCancelled:
            with self._condition:
                self._finish_locked(job, "cancelled")
        except BaseException as error:
            with self._condition:
                if job.token.cancelled:
                    self._finish_locked(job, "cancelled")
                else:
                    error_payload = ToolError(type(error).__name__, str(error))
                    self._finish_locked(job, "failed", error=error_payload)
        else:
            try:
                normalized = self._normalize_output(job, output)
            except BaseException as error:
                error_payload = ToolError(type(error).__name__, str(error))
                with self._condition:
                    self._finish_locked(job, "failed", error=error_payload)
            else:
                with self._condition:
                    self._finish_locked(job, "completed", output=normalized)

    def _normalize_output(self, job: _Job, output: JobOutput) -> JSONValue:
        normalizer = self._normalizer
        if normalizer is None:
            if isinstance(output, ToolOutput):
                cleanup = output.artifact_cleanup
                if cleanup is not None:
                    cleanup()
                return output.output
            return output
        if isinstance(output, ToolOutput) and output.attachments:
            cleanup = output.artifact_cleanup
            if cleanup is not None:
                cleanup()
            raise ValueError("background jobs cannot return image attachments")
        result = ToolResult(
            tool_call_id=job.job_id,
            name=job.kind,
            output=output.output if isinstance(output, ToolOutput) else output,
            attachments=output.attachments if isinstance(output, ToolOutput) else (),
            artifact_writer=(
                output.artifact_writer if isinstance(output, ToolOutput) else None
            ),
            artifact_cleanup=(
                output.artifact_cleanup if isinstance(output, ToolOutput) else None
            ),
        )
        return normalizer.normalize(result)

    def _finish_locked(
        self,
        job: _Job,
        status: Literal["completed", "failed", "cancelled"],
        *,
        output: JobOutput = None,
        error: ToolError | None = None,
    ) -> None:
        job.status = status
        self._session.job_finished(
            job.job_id,
            job.kind,
            status,
            turn_id=job.turn_id,
            error=error.message if error is not None else None,
        )
        self._publish_locked(
            JobUpdate(
                job.job_id,
                job.turn_id,
                job.kind,
                status,
                output=output,
                error=error,
            )
        )
        self._condition.notify_all()

    def drain_updates(self, turn_id: str) -> tuple[JobUpdate, ...]:
        with self._condition:
            selected = tuple(
                update for update in self._updates if update.turn_id == turn_id
            )
            self._updates = [
                update for update in self._updates if update.turn_id != turn_id
            ]
            for update in selected:
                if update.terminal:
                    self._jobs.pop(update.job_id, None)
            return selected

    def drain_terminal_state(
        self, turn_id: str
    ) -> tuple[tuple[JobUpdate, ...], bool]:
        with self._condition:
            selected = tuple(
                update
                for update in self._updates
                if update.turn_id == turn_id and update.terminal
            )
            terminal_ids = {update.job_id for update in selected}
            self._updates = [
                update for update in self._updates
                if update.job_id not in terminal_ids
            ]
            for update in selected:
                self._jobs.pop(update.job_id, None)
            pending = any(
                job.turn_id == turn_id
                and job.status in {"submitted", "running"}
                for job in self._jobs.values()
            )
            return selected, pending

    def snapshot(self) -> tuple[JobHandle, ...]:
        with self._condition:
            return tuple(
                JobHandle(job.job_id, job.kind, job.status)
                for job in self._jobs.values()
            )

    def wait_for_update(self, turn_id: str, timeout: float = 0.1) -> bool:
        with self._condition:
            if any(
                update.turn_id == turn_id and update.terminal
                for update in self._updates
            ):
                return True
            self._condition.wait(timeout)
            return any(
                update.turn_id == turn_id and update.terminal
                for update in self._updates
            )

    def cancel_turn(self, turn_id: str) -> None:
        with self._condition:
            jobs = [job for job in self._jobs.values() if job.turn_id == turn_id]
            for job in jobs:
                if job.status in {"submitted", "running"}:
                    job.token.cancel()
                    if job.future is not None and job.future.cancel():
                        self._finish_locked(job, "cancelled")
            self._condition.notify_all()

    def wait_for_turn(self, turn_id: str) -> None:
        with self._condition:
            while any(
                job.turn_id == turn_id
                and job.status in {"submitted", "running"}
                for job in self._jobs.values()
            ):
                self._condition.wait(0.1)

    def close(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
            turn_ids = {
                job.turn_id
                for job in self._jobs.values()
                if job.status in {"submitted", "running"}
            }
        for turn_id in turn_ids:
            self.cancel_turn(turn_id)
        for turn_id in turn_ids:
            self.wait_for_turn(turn_id)
        self._executor.shutdown(wait=True, cancel_futures=True)

    def cancel_and_discard(self, turn_id: str) -> None:
        self.cancel_turn(turn_id)
        self.wait_for_turn(turn_id)
        self.drain_updates(turn_id)
