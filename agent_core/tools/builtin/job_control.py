"""Inspect and cancel background work belonging to the parent Session."""

from ..base import JSONValue, Tool, ToolDefinition
from ..context import ToolExecutionContext


class JobControlTool(Tool):
    name = "job_control"

    def available(self, context: ToolExecutionContext) -> bool:
        return context.jobs is not None

    def definition(self, context: ToolExecutionContext) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=(
                "Manage background shell and subagent jobs in this session. "
                "Active job IDs and statuses are provided automatically in the "
                "[Active Jobs] context. get returns one execution "
                "snapshot, including terminal jobs; cancel requests cancellation "
                "and returns the current snapshot (running work may still be stopping). "
                "Snapshots include original details, elapsed_seconds excluding queue "
                "time, bounded progress, error and result_summary, never full output "
                "or the subagent's final report. completed means execution returned, "
                "not that a shell command succeeded: check result_summary.exit_code "
                "and timed_out. Recovered interrupted jobs cannot be cancelled; "
                "their elapsed_seconds is null when the actual duration is unknown. "
                "Full results are delivered automatically; do not poll for reports."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["get", "cancel"]},
                    "job_id": {
                        "type": "string",
                        "description": "The job to inspect or cancel.",
                    },
                },
                "required": ["action", "job_id"],
                "additionalProperties": False,
            },
        )

    def execute(
        self, arguments: dict[str, JSONValue], context: ToolExecutionContext,
    ) -> JSONValue:
        jobs = context.jobs
        if jobs is None:
            raise RuntimeError("job manager is unavailable")
        if not set(arguments) <= {"action", "job_id"}:
            raise ValueError("job_control accepts only 'action' and 'job_id'")
        action = arguments.get("action")
        if action not in ("get", "cancel"):
            raise ValueError("job_control action must be get or cancel")
        job_id = arguments.get("job_id")
        if not isinstance(job_id, str) or not job_id.strip():
            raise ValueError("job_control get/cancel requires a non-empty 'job_id'")
        snapshot = jobs.get(job_id) if action == "get" else jobs.cancel(job_id)
        return {"job": snapshot.to_dict()}
