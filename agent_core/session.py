"""Runtime session state and append-only journal semantics."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Literal
from threading import Lock
from uuid import uuid4

from .content import Content, ImagePart, TextPart, content_parts
from .errors import (
    RuntimeErrorInfo,
    runtime_error_from_dict,
)
from .permissions import PermissionPreset
from .plan import PlanSnapshot, plan_snapshot_from_dict, plan_snapshot_to_dict
from .tools import ToolCall

MessageRole = Literal["system", "user", "assistant", "tool"]
MessageOrigin = Literal["conversation", "tool_media", "job_result"]
UserAnchorSource = Literal[
    "user_input",
    "steering",
    "question_response",
    "approval_response",
]
TurnStatus = Literal[
    "running", "completed", "cancelled", "interrupted", "unknown", "failed"
]
ToolExecutionStatus = Literal[
    "started", "completed", "failed", "cancelled", "unknown"
]
JobExecutionStatus = Literal[
    "submitted", "running", "completed", "failed", "cancelled", "interrupted"
]


@dataclass(frozen=True)
class Message:
    role: MessageRole
    content: Content
    timestamp_utc: datetime | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    reasoning: str | None = None
    origin: MessageOrigin = "conversation"

    @property
    def parts(self):
        return content_parts(self.content)

    @property
    def is_tool_media(self) -> bool:
        return self.origin == "tool_media"

    @property
    def is_user_authored(self) -> bool:
        return self.role == "user" and self.origin == "conversation"


@dataclass(frozen=True)
class JournalEvent:
    seq: int
    event_id: str
    event_type: str
    turn_id: str | None
    timestamp_utc: datetime
    tool_call_id: str | None = None
    payload: dict[str, object] = field(default_factory=dict)


@dataclass
class Turn:
    turn_id: str
    status: TurnStatus
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: RuntimeErrorInfo | None = None


@dataclass
class ToolExecution:
    tool_call_id: str
    turn_id: str | None
    name: str
    arguments: dict[str, object]
    status: ToolExecutionStatus
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: str | None = None


@dataclass
class JobExecution:
    job_id: str
    turn_id: str
    kind: str
    status: JobExecutionStatus
    submitted_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: str | None = None


@dataclass(frozen=True)
class UserAnchor:
    turn_id: str
    content: Content
    timestamp_utc: datetime
    source: UserAnchorSource
    item_index: int | None = None


JournalSink = Callable[[tuple[JournalEvent, ...]], None]


@dataclass
class Session:
    session_id: str = field(default_factory=lambda: str(uuid4()))
    items: list[Message] = field(default_factory=list)
    workspace: str | None = None
    archived_summary: str | None = None
    archived_item_cursor: int = 0
    compression_count: int = 0
    permission_preset: PermissionPreset = PermissionPreset.ASK_FOR_APPROVAL
    plan: PlanSnapshot | None = None
    journal: list[JournalEvent] = field(default_factory=list, repr=False)
    turns: dict[str, Turn] = field(default_factory=dict, repr=False)
    tool_executions: dict[str, ToolExecution] = field(default_factory=dict, repr=False)
    jobs: dict[str, JobExecution] = field(default_factory=dict, repr=False)
    user_anchors: list[UserAnchor] = field(
        default_factory=list,
        repr=False,
        compare=False,
    )
    _sink: JournalSink | None = field(default=None, repr=False, compare=False)
    _current_turn_id: str | None = field(default=None, repr=False, compare=False)
    _journal_lock: Lock = field(default_factory=Lock, repr=False, compare=False)

    def attach_journal_sink(self, sink: JournalSink | None) -> None:
        self._sink = sink

    def apply_event(self, event: JournalEvent) -> None:
        """Replay one durable event into runtime state."""
        payload = event.payload
        if event.event_type == "message_appended" and isinstance(
            payload.get("message"), dict
        ):
            message = message_from_dict(payload["message"])
            item_index = len(self.items)
            self.items.append(message)
            if (
                event.turn_id is not None
                and message.is_user_authored
            ):
                self.user_anchors.append(
                    UserAnchor(
                        turn_id=event.turn_id,
                        content=message.content,
                        timestamp_utc=message.timestamp_utc or event.timestamp_utc,
                        source=(
                            payload.get("user_source")
                            if payload.get("user_source") == "steering"
                            else "user_input"
                        ),
                        item_index=item_index,
                    )
                )
        elif event.event_type == "turn_started" and event.turn_id:
            self.turns[event.turn_id] = Turn(
                event.turn_id,
                "running",
                started_at=event.timestamp_utc,
            )
        elif event.event_type.startswith("turn_") and event.turn_id:
            turn = self.turns.get(event.turn_id)
            if turn is None:
                turn = Turn(event.turn_id, "unknown")
                self.turns[event.turn_id] = turn
            turn.status = payload.get("status", "unknown")
            turn.finished_at = event.timestamp_utc
            turn.error = (
                runtime_error_from_dict(payload["error"])
                if "error" in payload
                else None
            )
        elif event.event_type == "tool_started" and event.tool_call_id:
            self.tool_executions[event.tool_call_id] = ToolExecution(
                tool_call_id=event.tool_call_id,
                turn_id=event.turn_id,
                name=str(payload.get("name", "")),
                arguments=dict(payload.get("arguments") or {}),
                status="started",
                started_at=event.timestamp_utc,
            )
        elif event.event_type in (
            "tool_completed",
            "tool_failed",
            "tool_cancelled",
            "tool_unknown",
        ) and event.tool_call_id:
            execution = self.tool_executions.get(event.tool_call_id)
            if execution is None:
                execution = ToolExecution(
                    event.tool_call_id,
                    event.turn_id,
                    str(payload.get("name", "")),
                    {},
                    "unknown",
                )
                self.tool_executions[event.tool_call_id] = execution
            execution.status = payload.get("status", "unknown")
            execution.completed_at = event.timestamp_utc
            execution.error = (
                payload.get("error")
                if isinstance(payload.get("error"), str)
                else None
            )
        elif event.event_type == "tool_recovered" and event.tool_call_id:
            execution = self.tool_executions.get(event.tool_call_id)
            if execution is not None:
                execution.status = "unknown"
                execution.completed_at = event.timestamp_utc
        elif event.event_type == "job_submitted":
            job_id = payload.get("job_id")
            if isinstance(job_id, str) and event.turn_id is not None:
                self.jobs[job_id] = JobExecution(
                    job_id=job_id,
                    turn_id=event.turn_id,
                    kind=str(payload.get("kind", "")),
                    status="submitted",
                    submitted_at=event.timestamp_utc,
                )
        elif event.event_type == "job_started":
            job_id = payload.get("job_id")
            if isinstance(job_id, str) and event.turn_id is not None:
                job = self.jobs.get(job_id)
                if job is None:
                    job = JobExecution(
                        job_id, event.turn_id, str(payload.get("kind", "")), "running"
                    )
                    self.jobs[job_id] = job
                job.status = "running"
                job.started_at = event.timestamp_utc
        elif event.event_type in {
            "job_completed", "job_failed", "job_cancelled", "job_interrupted"
        }:
            job_id = payload.get("job_id")
            if isinstance(job_id, str) and event.turn_id is not None:
                job = self.jobs.get(job_id)
                if job is None:
                    job = JobExecution(
                        job_id,
                        event.turn_id,
                        str(payload.get("kind", "")),
                        "interrupted",
                    )
                    self.jobs[job_id] = job
                status = payload.get("status")
                job.status = (
                    status
                    if status in {
                        "submitted",
                        "running",
                        "completed",
                        "failed",
                        "cancelled",
                        "interrupted",
                    }
                    else "interrupted"
                )
                job.completed_at = event.timestamp_utc
                job.error = (
                    payload.get("error")
                    if isinstance(payload.get("error"), str)
                    else None
                )
        elif event.event_type == "context_archived":
            self.archived_summary = (
                payload.get("summary")
                if isinstance(payload.get("summary"), str)
                else None
            )
            self.archived_item_cursor = int(payload["item_cursor"])
            self.compression_count += 1
        elif event.event_type == "user_interaction_recorded" and event.turn_id:
            content = payload.get("content")
            source = payload.get("source")
            anchor_source: UserAnchorSource | None
            if isinstance(content, str) and source == "question_response":
                anchor_source = "question_response"
            elif isinstance(content, str) and source == "approval_response":
                anchor_source = "approval_response"
            else:
                anchor_source = None
            if anchor_source is not None:
                self.user_anchors.append(
                    UserAnchor(
                        turn_id=event.turn_id,
                        content=content,
                        timestamp_utc=event.timestamp_utc,
                        source=anchor_source,
                    )
                )
        elif event.event_type == "permission_preset_changed":
            self.permission_preset = PermissionPreset(str(payload["preset"]))
        elif event.event_type == "plan_updated" and isinstance(
            payload.get("plan"), dict
        ):
            self.plan = plan_snapshot_from_dict(payload["plan"])

    def recover(self) -> tuple[JournalEvent, ...]:
        recovered: list[JournalEvent] = []
        next_seq = self.journal[-1].seq + 1 if self.journal else 1
        for turn in self.turns.values():
            if turn.status == "running":
                recovered.append(
                    JournalEvent(
                        seq=next_seq + len(recovered),
                        event_id=str(uuid4()),
                        event_type="turn_recovered",
                        turn_id=turn.turn_id,
                        timestamp_utc=datetime.now(timezone.utc),
                        payload={"status": "unknown"},
                    )
                )
        for execution in self.tool_executions.values():
            if execution.status == "started":
                recovered.append(
                    JournalEvent(
                        seq=next_seq + len(recovered),
                        event_id=str(uuid4()),
                        event_type="tool_recovered",
                        turn_id=execution.turn_id,
                        timestamp_utc=datetime.now(timezone.utc),
                        tool_call_id=execution.tool_call_id,
                        payload={"status": "unknown"},
                    )
                )
        for job in self.jobs.values():
            if job.status in {"submitted", "running"}:
                recovered.append(
                    JournalEvent(
                        seq=next_seq + len(recovered),
                        event_id=str(uuid4()),
                        event_type="job_interrupted",
                        turn_id=job.turn_id,
                        timestamp_utc=datetime.now(timezone.utc),
                        payload={
                            "job_id": job.job_id,
                            "kind": job.kind,
                            "status": "interrupted",
                        },
                    )
                )
        if recovered and self._sink is not None:
            self._sink(tuple(recovered))
        self.journal.extend(recovered)
        for event in recovered:
            self.apply_event(event)
        return tuple(recovered)

    def begin_turn(self, turn_id: str | None = None) -> str:
        # Client turn ids are correlation hints, not durable identities.  A
        # restarted frontend may reuse an old id, so never overwrite the
        # existing turn state during recovery.
        if turn_id is None or turn_id in self.turns:
            turn_id = str(uuid4())
        self._event("turn_started", turn_id, {"status": "running"})
        self._current_turn_id = turn_id
        return turn_id

    @property
    def current_turn_id(self) -> str | None:
        return self._current_turn_id

    def recent_user_anchors(self) -> tuple[UserAnchor, ...]:
        """Return user inputs from the active turn and its predecessor."""
        active_turn_id = self._current_turn_id
        if active_turn_id is None:
            return ()
        turn_ids = list(self.turns)
        try:
            active_index = turn_ids.index(active_turn_id)
        except ValueError:
            return ()
        selected = {active_turn_id}
        if active_index > 0:
            selected.add(turn_ids[active_index - 1])
        return tuple(
            anchor for anchor in self.user_anchors
            if anchor.turn_id in selected
        )

    def record_user_interaction(
        self,
        content: str,
        source: UserAnchorSource,
    ) -> None:
        if self._current_turn_id is None:
            return
        self._event(
            "user_interaction_recorded",
            self._current_turn_id,
            {"content": content, "source": source},
        )

    def finish_turn(
        self,
        status: TurnStatus,
        turn_id: str | None = None,
        *,
        error: RuntimeErrorInfo | None = None,
    ) -> None:
        turn_id = turn_id or self._current_turn_id
        if turn_id is None:
            return
        payload: dict[str, object] = {"status": status}
        if error is not None:
            payload["error"] = {
                "type": error.type,
                "message": error.message,
                "details": dict(error.details),
            }
        self._event(f"turn_{status}", turn_id, payload)
        if turn_id == self._current_turn_id:
            self._current_turn_id = None

    def tool_started(self, call: ToolCall, turn_id: str | None = None) -> None:
        turn_id = turn_id or self._current_turn_id
        self._event(
            "tool_started",
            turn_id,
            {"name": call.name, "arguments": dict(call.arguments)},
            call.id,
        )

    def model_completed(
        self,
        model_call_index: int,
        *,
        input_tokens: int | None,
        output_tokens: int | None,
        total_tokens: int | None,
        has_tool_calls: bool,
    ) -> None:
        self._event(
            "model_completed",
            self._current_turn_id,
            {
                "model_call_index": model_call_index,
                "has_tool_calls": has_tool_calls,
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "total_tokens": total_tokens,
                },
            },
        )

    def tool_finished(
        self,
        call: ToolCall,
        status: ToolExecutionStatus,
        *,
        error: str | None = None,
        turn_id: str | None = None,
    ) -> None:
        turn_id = turn_id or self._current_turn_id
        payload: dict[str, object] = {"status": status, "name": call.name}
        if error is not None:
            payload["error"] = error
        self._event(f"tool_{status}", turn_id, payload, call.id)

    def job_submitted(self, job_id: str, kind: str, turn_id: str) -> None:
        self._event(
            "job_submitted",
            turn_id,
            {"job_id": job_id, "kind": kind, "status": "submitted"},
        )

    def job_started(self, job_id: str, kind: str, turn_id: str) -> None:
        self._event(
            "job_started",
            turn_id,
            {"job_id": job_id, "kind": kind, "status": "running"},
        )

    def job_finished(
        self,
        job_id: str,
        kind: str,
        status: Literal["completed", "failed", "cancelled"],
        *,
        turn_id: str,
        error: str | None = None,
    ) -> None:
        payload: dict[str, object] = {
            "job_id": job_id,
            "kind": kind,
            "status": status,
        }
        if error is not None:
            payload["error"] = error
        self._event(f"job_{status}", turn_id, payload)

    def set_archived_summary(
        self, summary: str, item_cursor: int
    ) -> None:
        item_cursor = max(0, min(item_cursor, len(self.items)))
        self._event(
            "context_archived",
            self._current_turn_id,
            {"summary": summary, "item_cursor": item_cursor},
        )

    def set_permission_preset(self, preset: PermissionPreset) -> None:
        if preset is self.permission_preset:
            return
        self._event(
            "permission_preset_changed",
            self._current_turn_id,
            {"preset": preset.value},
        )

    def set_plan(self, snapshot: PlanSnapshot) -> None:
        self._event(
            "plan_updated",
            None,
            {"plan": plan_snapshot_to_dict(snapshot)},
        )

    def add_item(
        self,
        role: MessageRole,
        content: Content,
        timestamp_utc: datetime | None = None,
        tool_calls: tuple[ToolCall, ...] = (),
        tool_call_id: str | None = None,
        reasoning: str | None = None,
        attachments: tuple[ImagePart, ...] = (),
        origin: MessageOrigin = "conversation",
        user_source: UserAnchorSource = "user_input",
    ) -> None:
        if attachments:
            parts = list(content_parts(content))
            parts.extend(attachments)
            content = tuple(parts)
        message = Message(
            role,
            content,
            timestamp_utc,
            tool_calls,
            tool_call_id,
            reasoning,
            origin,
        )
        self._event(
            "message_appended",
            self._current_turn_id,
            {
                "message": message_to_dict(message),
                **(
                    {"user_source": user_source}
                    if user_source != "user_input"
                    else {}
                ),
            },
            tool_call_id,
        )

    def _event(
        self,
        event_type: str,
        turn_id: str | None,
        payload: dict[str, object],
        tool_call_id: str | None = None,
    ) -> JournalEvent:
        with self._journal_lock:
            event = JournalEvent(
                seq=(self.journal[-1].seq + 1 if self.journal else 1),
                event_id=str(uuid4()),
                event_type=event_type,
                turn_id=turn_id,
                timestamp_utc=datetime.now(timezone.utc),
                tool_call_id=tool_call_id,
                payload=payload,
            )
            if self._sink is not None:
                self._sink((event,))
            self.journal.append(event)
            self.apply_event(event)
        return event


def message_to_dict(message: Message) -> dict[str, object]:
    data: dict[str, object] = {
        "role": message.role,
        "content": _content_to_dict(message),
    }
    if message.timestamp_utc is not None:
        data["timestamp_utc"] = _format_utc(message.timestamp_utc)
    if message.tool_calls:
        data["tool_calls"] = [
            {"id": call.id, "name": call.name, "arguments": call.arguments}
            for call in message.tool_calls
        ]
    if message.tool_call_id is not None:
        data["tool_call_id"] = message.tool_call_id
    if message.reasoning is not None:
        data["reasoning"] = message.reasoning
    if message.origin != "conversation":
        data["origin"] = message.origin
    return data


def message_from_dict(data: dict[str, object]) -> Message:
    content = data.get("content")
    if isinstance(content, list):
        parts = []
        for part in content:
            if not isinstance(part, dict):
                raise ValueError("message content part must be an object")
            if part.get("type") == "text":
                parts.append(TextPart(str(part.get("text", ""))))
            elif part.get("type") == "image":
                parts.append(
                    ImagePart(
                        str(part.get("path", "")),
                        str(part.get("mime_type", "image/png")),
                    )
                )
            else:
                raise ValueError("unknown message content part type")
        content = tuple(parts)
    calls = tuple(
        ToolCall(call["id"], call["name"], call["arguments"])
        for call in data.get("tool_calls", [])
    )
    timestamp = data.get("timestamp_utc")
    return Message(
        role=data["role"],
        content=content,
        timestamp_utc=(
            _parse_utc(timestamp) if isinstance(timestamp, str) else None
        ),
        tool_calls=calls,
        tool_call_id=data.get("tool_call_id"),
        reasoning=(
            data.get("reasoning")
            if isinstance(data.get("reasoning"), str)
            else None
        ),
        origin=(
            data["origin"]
            if data.get("origin") in {"tool_media", "job_result"}
            else "conversation"
        ),
    )


def _content_to_dict(message: Message) -> object:
    parts = message.parts
    if not parts:
        return None
    if all(isinstance(part, TextPart) for part in parts):
        return "".join(part.text for part in parts)
    return [
        {"type": "text", "text": part.text}
        if isinstance(part, TextPart)
        else {
            "type": "image",
            "path": part.path,
            "mime_type": part.mime_type,
        }
        for part in parts
    ]


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
        timezone.utc
    )
