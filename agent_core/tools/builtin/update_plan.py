from ...plan import (
    MAX_PLAN_STEP_OUTCOME_CHARS,
    PlanStep,
    plan_snapshot_to_dict,
)
from ..base import JSONValue, Tool, ToolDefinition
from ..context import ToolExecutionContext


class UpdatePlanTool(Tool):
    name = "update_plan"

    def available(self, context: ToolExecutionContext) -> bool:
        return context.plan is not None

    def definition(self, context: ToolExecutionContext) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=(
                "Create or replace the current plan for multi-step work. Send "
                "the complete plan whenever progress changes, including after "
                "finishing a step. Keep at most one step in_progress."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "goal": {
                        "type": "string",
                        "description": "The concise goal this plan accomplishes.",
                    },
                    "steps": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "title": {"type": "string"},
                                "status": {
                                    "type": "string",
                                    "enum": [
                                        "pending",
                                        "in_progress",
                                        "completed",
                                        "blocked",
                                    ],
                                },
                                "outcome": {
                                    "type": "string",
                                    "maxLength": MAX_PLAN_STEP_OUTCOME_CHARS,
                                    "description": (
                                        "A concise final result for a completed "
                                        "step, or a concise reason for a blocked "
                                        "step. Use only with completed or blocked "
                                        "steps. Do not include logs or full tool output."
                                    ),
                                },
                            },
                            "required": ["id", "title", "status"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["goal", "steps"],
                "additionalProperties": False,
            },
        )

    def execute(
        self,
        arguments: dict[str, JSONValue],
        context: ToolExecutionContext,
    ) -> JSONValue:
        if context.plan is None:
            raise RuntimeError("plan manager is unavailable")
        unknown = set(arguments) - {"goal", "steps"}
        if unknown:
            raise ValueError(
                f"unknown update_plan argument(s): {', '.join(sorted(unknown))}"
            )
        goal = arguments.get("goal")
        if not isinstance(goal, str) or not goal.strip():
            raise ValueError("goal must be a non-empty string")
        values = arguments.get("steps")
        if not isinstance(values, list) or not values:
            raise ValueError("steps must be a non-empty array")

        steps = []
        ids: set[str] = set()
        in_progress = 0
        for value in values:
            if not isinstance(value, dict):
                raise ValueError("each plan step must be an object")
            unknown_step = set(value) - {"id", "title", "status", "outcome"}
            if unknown_step:
                raise ValueError(
                    "unknown plan step field(s): "
                    + ", ".join(sorted(unknown_step))
                )
            step_id = value.get("id")
            title = value.get("title")
            status = value.get("status")
            outcome = value.get("outcome")
            if not isinstance(step_id, str) or not step_id.strip():
                raise ValueError("plan step id must be a non-empty string")
            step_id = step_id.strip()
            if step_id in ids:
                raise ValueError(f"duplicate plan step id: {step_id}")
            if not isinstance(title, str):
                raise ValueError("plan step title must be a non-empty string")
            if not isinstance(status, str):
                raise ValueError(f"invalid plan step status: {status}")
            if outcome is not None and not isinstance(outcome, str):
                raise ValueError("plan step outcome must be a string")
            ids.add(step_id)
            in_progress += status == "in_progress"
            steps.append(PlanStep(step_id, title, status, outcome))
        if in_progress > 1:
            raise ValueError("plan may contain at most one in_progress step")

        snapshot = context.plan.update(goal.strip(), tuple(steps))
        return plan_snapshot_to_dict(snapshot)
