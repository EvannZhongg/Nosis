"""GUI projection of the bridge's authoritative runtime event stream."""

from dataclasses import dataclass, field

from interfaces.bridge.protocol import runtime_state_message


TURN_END_MESSAGE_TYPES = frozenset(
    {"turn_completed", "turn_cancelled", "turn_failed"}
)
CONTEXT_WINDOW_FIELDS = (
    "input_tokens",
    "max_input_tokens",
    "max_context_tokens",
    "output_reserve_tokens",
    "compression_threshold",
    "compression_count",
)


@dataclass(slots=True)
class RuntimeProjection:
    """State needed by the GUI to reconnect to an existing bridge."""

    provider: str | None
    workspace: str
    phase: str = "inactive"
    turn_id: str | None = None
    approval: dict[str, object] | None = None
    question: dict[str, object] | None = None
    permission_preset: str = "ask_for_approval"
    context_window: dict[str, object] | None = None
    plan: dict[str, object] | None = None
    runtime_warnings: tuple[str, ...] = ()
    jobs: dict[str, dict[str, object]] = field(default_factory=dict)

    @property
    def running(self) -> bool:
        return self.phase in {
            "starting",
            "running",
            "waiting_approval",
            "waiting_user",
        }

    def apply_client_message(self, message: dict[str, object]) -> None:
        message_type = message.get("type")
        if message_type == "user_turn":
            self.phase = "starting"
            self.turn_id = str(message.get("turn_id"))
            self.approval = None
            self.question = None
        elif message_type == "approval_response":
            self.approval = None
        elif message_type == "user_question_response":
            self.question = None

    def apply_bridge_message(self, message: dict[str, object]) -> None:
        message_type = message.get("type")
        if message_type == "approval_request":
            self.approval = message
            self.question = None
        elif message_type == "user_question":
            self.approval = None
            self.question = message
        elif message_type in {"session_ready", "permission_changed"}:
            preset = message.get("permission_preset", message.get("preset"))
            if isinstance(preset, str):
                self.permission_preset = preset
            if message_type == "session_ready":
                provider = message.get("provider")
                if isinstance(provider, str):
                    self.provider = provider
        elif message_type == "provider_changed":
            provider = message.get("provider")
            if isinstance(provider, str):
                self.provider = provider
        elif message_type == "workspace_changed":
            workspace = message.get("workspace")
            if isinstance(workspace, str):
                self.workspace = workspace
        elif message_type == "runtime_state":
            self._replace_runtime_state(message)
        elif message_type == "plan_updated":
            plan = message.get("plan")
            self.plan = plan if isinstance(plan, dict) else None
        elif message_type == "context_window":
            self.context_window = {
                key: message[key] for key in CONTEXT_WINDOW_FIELDS
            }
        elif message_type == "job_status":
            self._apply_job_status(message)
        elif message_type in TURN_END_MESSAGE_TYPES:
            self.phase = "idle"
            self.turn_id = None
            self.approval = None
            self.question = None
            self.jobs.clear()
            self.runtime_warnings = ()
        elif message_type == "fatal":
            self.phase = "failed"
            self.turn_id = None
            self.approval = None
            self.question = None
            self.jobs.clear()

    def bridge_finished(self, *, unexpected: bool, returncode: int | None) -> None:
        if unexpected and (self.running or returncode != 0):
            self.phase = "failed"
        self.approval = None
        self.question = None
        self.jobs.clear()

    def runtime_state(self, *, event_sequence: int) -> dict[str, object]:
        return runtime_state_message(
            phase=self.phase,
            turn_id=self.turn_id,
            approval=self.approval,
            question=self.question,
            provider=self.provider,
            permission_preset=self.permission_preset,
            context_window=self.context_window,
            jobs=list(self.jobs.values()),
            runtime_warnings=self.runtime_warnings,
            plan=self.plan,
            event_sequence=event_sequence,
        )

    def _replace_runtime_state(self, message: dict[str, object]) -> None:
        phase = message.get("phase")
        if isinstance(phase, str):
            self.phase = phase
        provider = message.get("provider")
        if isinstance(provider, str):
            self.provider = provider
        permission_preset = message.get("permission_preset")
        if isinstance(permission_preset, str):
            self.permission_preset = permission_preset
        turn_id = message.get("turn_id")
        self.turn_id = turn_id if isinstance(turn_id, str) else None
        approval = message.get("approval")
        self.approval = approval if isinstance(approval, dict) else None
        question = message.get("question")
        self.question = question if isinstance(question, dict) else None
        context_window = message.get("context_window")
        self.context_window = (
            context_window if isinstance(context_window, dict) else None
        )
        jobs = message.get("jobs")
        self.jobs = (
            {
                str(job["job_id"]): job
                for job in jobs
                if isinstance(job, dict) and isinstance(job.get("job_id"), str)
            }
            if isinstance(jobs, list)
            else {}
        )
        warnings = message.get("runtime_warnings")
        self.runtime_warnings = (
            tuple(warning for warning in warnings if isinstance(warning, str))
            if isinstance(warnings, list)
            else ()
        )
        plan = message.get("plan")
        self.plan = plan if isinstance(plan, dict) else None

    def _apply_job_status(self, message: dict[str, object]) -> None:
        job_id = message.get("job_id")
        if not isinstance(job_id, str):
            return
        if message.get("status") in {"completed", "failed", "cancelled"}:
            self.jobs.pop(job_id, None)
        else:
            self.jobs[job_id] = message
