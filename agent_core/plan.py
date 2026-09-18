"""Session-scoped plan state managed independently from Agent turns."""

from dataclasses import dataclass
from threading import Lock
from typing import Callable, Literal, TYPE_CHECKING
from uuid import uuid4

if TYPE_CHECKING:
    from .session import Session


PlanStepStatus = Literal[
    "pending",
    "in_progress",
    "completed",
    "blocked",
]
MAX_PLAN_STEP_OUTCOME_CHARS = 500


@dataclass(frozen=True)
class PlanStep:
    id: str
    title: str
    status: PlanStepStatus
    outcome: str | None = None

    def __post_init__(self) -> None:
        step_id = self.id.strip()
        title = self.title.strip()
        if not step_id:
            raise ValueError("plan step id must be a non-empty string")
        if not title:
            raise ValueError("plan step title must be a non-empty string")
        if self.status not in {
            "pending", "in_progress", "completed", "blocked"
        }:
            raise ValueError(f"invalid plan step status: {self.status}")
        outcome = self.outcome.strip() if self.outcome is not None else None
        if outcome == "":
            outcome = None
        if outcome is not None and len(outcome) > MAX_PLAN_STEP_OUTCOME_CHARS:
            raise ValueError(
                "plan step outcome exceeds maximum length of "
                f"{MAX_PLAN_STEP_OUTCOME_CHARS} characters"
            )
        if outcome is not None and self.status not in {"completed", "blocked"}:
            raise ValueError(
                "plan step outcome is allowed only for completed or blocked steps"
            )
        object.__setattr__(self, "id", step_id)
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "outcome", outcome)


@dataclass(frozen=True)
class PlanSnapshot:
    plan_id: str
    goal: str
    revision: int
    steps: tuple[PlanStep, ...]

    @property
    def is_active(self) -> bool:
        return any(step.status != "completed" for step in self.steps)


PlanUpdateCallback = Callable[[PlanSnapshot], None]


class PlanManager:
    """Own the current plan for one Session and publish durable revisions."""

    def __init__(
        self,
        session: "Session",
        on_update: PlanUpdateCallback | None = None,
    ) -> None:
        self._session = session
        self._on_update = on_update
        self._lock = Lock()

    @property
    def snapshot(self) -> PlanSnapshot | None:
        return self._session.plan

    def update(self, goal: str, steps: tuple[PlanStep, ...]) -> PlanSnapshot:
        with self._lock:
            current = self._session.plan
            if current is None or current.goal != goal:
                snapshot = PlanSnapshot(
                    plan_id=str(uuid4()),
                    goal=goal,
                    revision=1,
                    steps=steps,
                )
            else:
                snapshot = PlanSnapshot(
                    plan_id=current.plan_id,
                    goal=goal,
                    revision=current.revision + 1,
                    steps=steps,
                )
            self._session.set_plan(snapshot)
            if self._on_update is not None:
                self._on_update(snapshot)
            return snapshot


def plan_snapshot_to_dict(snapshot: PlanSnapshot) -> dict[str, object]:
    return {
        "plan_id": snapshot.plan_id,
        "goal": snapshot.goal,
        "revision": snapshot.revision,
        "steps": [
            {
                "id": step.id,
                "title": step.title,
                "status": step.status,
                **(
                    {"outcome": step.outcome}
                    if step.outcome is not None
                    else {}
                ),
            }
            for step in snapshot.steps
        ],
    }


def plan_snapshot_from_dict(data: dict[str, object]) -> PlanSnapshot:
    steps_value = data.get("steps")
    if not isinstance(steps_value, list):
        raise ValueError("plan steps must be an array")
    steps = []
    for value in steps_value:
        if not isinstance(value, dict):
            raise ValueError("plan step must be an object")
        status = value.get("status")
        outcome = value.get("outcome")
        if not isinstance(status, str):
            raise ValueError("plan step status must be a string")
        if outcome is not None and not isinstance(outcome, str):
            raise ValueError("plan step outcome must be a string")
        steps.append(
            PlanStep(
                id=str(value["id"]),
                title=str(value["title"]),
                status=status,
                outcome=outcome,
            )
        )
    return PlanSnapshot(
        plan_id=str(data["plan_id"]),
        goal=str(data["goal"]),
        revision=int(data["revision"]),
        steps=tuple(steps),
    )
