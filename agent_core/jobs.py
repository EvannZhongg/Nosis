"""Turn-scoped background work managed by the Agent Runtime."""

from concurrent.futures import Future, ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass, field
from threading import Condition, Event, Lock
from time import monotonic
from typing import Callable, Literal, TypeAlias
from uuid import uuid4

from .session import JobExecutionStatus, Session
from .tools.base import JSONValue, ToolError, ToolOutput, ToolResult
from .tool_result import ToolResultNormalizer


JobStatus: TypeAlias = JobExecutionStatus
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
class JobSnapshot:
    job_id: str
    kind: str
    status: JobStatus
    details: dict[str, str]
    elapsed_seconds: float | None
    progress: dict[str, JSONValue] | None
    error: str | None
    result_summary: dict[str, JSONValue] | None
    cancel_requested: bool

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "job_id": self.job_id,
            "kind": self.kind,
            "status": self.status,
            "details": dict(self.details),
            "elapsed_seconds": self.elapsed_seconds,
            "progress": deepcopy(self.progress),
            "error": self.error,
            "result_summary": deepcopy(self.result_summary),
            "cancel_requested": self.cancel_requested,
        }


@dataclass(frozen=True)
class JobResult:
    """Execution-owned summary, separate from the full result delivery."""

    output: JobOutput
    result_summary: dict[str, JSONValue]


@dataclass(frozen=True)
class JobContext:
    cancellation: CancellationToken
    report_progress: Callable[[dict[str, JSONValue]], None]


@dataclass(frozen=True)
class JobUpdate:
    job_id: str
    turn_id: str
    kind: str
    status: JobStatus
    details: dict[str, str]
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
    details: dict[str, str]
    status: JobStatus = "submitted"
    future: Future[None] | None = None
    started_at: float | None = None
    progress: dict[str, JSONValue] | None = None
    progress_lock: Lock = field(default_factory=Lock)


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
        run: Callable[[JobContext], JobOutput | JobResult],
        *,
        details: dict[str, str],
    ) -> JobHandle:
        job_id = f"job_{uuid4().hex[:12]}"
        job = _Job(job_id, turn_id, kind, CancellationToken(), dict(details))
        with self._condition:
            if self._closed:
                raise RuntimeError("job manager is closed")
            self._jobs[job_id] = job
            self._publish_locked(JobUpdate(job_id, turn_id, kind, "submitted", job.details))
            self._session.job_submitted(job_id, kind, turn_id, details=job.details)
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
        run: Callable[[JobContext], JobOutput | JobResult],
    ) -> None:
        with self._condition:
            if job.token.cancelled:
                self._finish_locked(job, "cancelled")
                return
            job.status = "running"
            job.started_at = monotonic()
            self._session.job_started(job.job_id, job.kind, job.turn_id)
            self._publish_locked(
                JobUpdate(job.job_id, job.turn_id, job.kind, "running", job.details)
            )
            self._condition.notify_all()
        try:
            output = run(JobContext(
                cancellation=job.token,
                report_progress=lambda progress: self._report_progress(job, progress),
            ))
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
            summary = None
            if isinstance(output, JobResult):
                summary = output.result_summary
                output = output.output
            try:
                normalized = self._normalize_output(job, output)
            except BaseException as error:
                error_payload = ToolError(type(error).__name__, str(error))
                with self._condition:
                    self._finish_locked(
                        job, "failed", error=error_payload, result_summary=summary,
                    )
            else:
                with self._condition:
                    self._finish_locked(
                        job, "completed", output=normalized, result_summary=summary,
                    )

    def _report_progress(self, job: _Job, progress: dict[str, JSONValue]) -> None:
        snapshot = deepcopy(progress)
        # Progress is transient: publishing it never acquires the manager lock
        # or writes to the Session journal. The terminal event saves it once.
        with job.progress_lock:
            if job.status == "running":
                job.progress = snapshot

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
            artifact_preview=(
                output.artifact_preview if isinstance(output, ToolOutput) else None
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
        result_summary: dict[str, JSONValue] | None = None,
    ) -> None:
        with job.progress_lock:
            job.status = status
            progress = deepcopy(job.progress)
        self._session.job_finished(
            job.job_id,
            job.kind,
            status,
            turn_id=job.turn_id,
            error=error.message if error is not None else None,
            elapsed_seconds=(
                monotonic() - job.started_at if job.started_at is not None else 0.0
            ),
            result_summary=deepcopy(result_summary),
            progress=progress,
        )
        self._publish_locked(
            JobUpdate(
                job.job_id,
                job.turn_id,
                job.kind,
                status,
                job.details,
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

    def _snapshot_locked(self, job_id: str) -> JobSnapshot:
        record = self._session.jobs.get(job_id)
        if record is None:
            raise ValueError(f"unknown job_id: {job_id}")
        job = self._jobs.get(job_id)
        if job is not None:
            with job.progress_lock:
                progress = deepcopy(job.progress)
        else:
            progress = deepcopy(record.progress)
        elapsed = record.elapsed_seconds
        if job is not None and job.status == "running" and job.started_at is not None:
            elapsed = monotonic() - job.started_at
        return JobSnapshot(
            job_id=record.job_id,
            kind=record.kind,
            status=record.status,
            details=dict(record.details),
            elapsed_seconds=elapsed,
            progress=progress,
            error=record.error,
            result_summary=deepcopy(record.result_summary),
            cancel_requested=(
                job is not None and job.status in {"submitted", "running"}
                and job.token.cancelled
            ),
        )

    def get(self, job_id: str) -> JobSnapshot:
        with self._condition:
            return self._snapshot_locked(job_id)

    def list(self) -> tuple[JobSnapshot, ...]:
        with self._condition:
            return tuple(
                self._snapshot_locked(job.job_id)
                for job in self._jobs.values()
                if job.status in {"submitted", "running"}
            )

    def cancel(self, job_id: str) -> JobSnapshot:
        with self._condition:
            snapshot = self._snapshot_locked(job_id)
            if snapshot.status in {"submitted", "running"}:
                self._cancel_locked(self._jobs[job_id])
            return self._snapshot_locked(job_id)

    def _cancel_locked(self, job: _Job) -> None:
        if job.status in {"submitted", "running"}:
            job.token.cancel()
            if job.future is not None and job.future.cancel():
                self._finish_locked(job, "cancelled")
        self._condition.notify_all()

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
                self._cancel_locked(job)

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
