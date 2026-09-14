"""Tool for delegating a task to a sub-agent role."""
from ..base import JSONValue, Tool, ToolDefinition
from ..context import ToolExecutionContext


class SubagentTool(Tool):
    """Delegate a task to one of the Runtime's sub-agent roles.

    A single entry point for every role: the role list is part of the
    schema, so new roles arrive as configuration instead of as new tools.
    """

    name = "subagent"
    # Independent child sessions share no state, so a batch of
    # delegations runs in parallel.
    concurrent = True

    def available(self, context: ToolExecutionContext) -> bool:
        return context.subagents is not None and bool(
            context.subagents.roles
        )

    def definition(self, context: ToolExecutionContext) -> ToolDefinition:
        roles = list(context.subagents.roles) if context.subagents else []
        described = "; ".join(
            f"{role.name}: {role.description}" for role in roles
        )
        return ToolDefinition(
            name=self.name,
            description=(
                "Delegate a task to an independent sub-agent role. Returns "
                f"only the sub-agent's final report. Roles — {described}"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "role": {
                        "type": "string",
                        "enum": [role.name for role in roles],
                        "description": "Which sub-agent role to run.",
                    },
                    "task": {
                        "type": "string",
                        "description": (
                            "Self-contained description of the task; the "
                            "sub-agent does not see this conversation."
                        ),
                    },
                },
                "required": ["role", "task"],
                "additionalProperties": False,
            },
        )

    def execute(
        self,
        arguments: dict[str, JSONValue],
        context: ToolExecutionContext,
    ) -> JSONValue:
        role = arguments.get("role")
        task = arguments.get("task")
        if not isinstance(role, str) or not role.strip():
            raise ValueError("subagent requires a non-empty string 'role'")
        if not isinstance(task, str) or not task.strip():
            raise ValueError("subagent requires a non-empty string 'task'")
        if set(arguments) != {"role", "task"}:
            raise ValueError("subagent accepts only 'role' and 'task'")
        if context.subagents is None:
            raise ValueError("this runtime has no sub-agent roles")
        return context.subagents.run(role.strip(), task.strip(), context)
