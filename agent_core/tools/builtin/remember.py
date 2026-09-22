"""Submit a candidate to the Runtime's long-term memory manager."""

from ...memory import MemoryCandidate
from ..base import JSONValue, Tool, ToolDefinition
from ..context import ToolExecutionContext


class RememberTool(Tool):
    name = "remember"

    def available(self, context: ToolExecutionContext) -> bool:
        return context.memory is not None

    def definition(self, context: ToolExecutionContext) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=(
                "Mark one durable user preference, stable fact, or workspace "
                "decision as worth retaining across sessions. This submits a "
                "candidate only; the Runtime later reconciles it with existing "
                "memory. Use global scope for facts or preferences that apply "
                "across workspaces, and workspace scope for decisions or facts "
                "specific to the current workspace."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["preference", "fact", "decision"],
                    },
                    "scope": {
                        "type": "string",
                        "enum": ["global", "workspace"],
                    },
                    "content": {
                        "type": "string",
                        "description": "A concise, self-contained memory candidate.",
                    },
                },
                "required": ["kind", "scope", "content"],
                "additionalProperties": False,
            },
        )

    def execute(
        self,
        arguments: dict[str, JSONValue],
        context: ToolExecutionContext,
    ) -> JSONValue:
        if context.memory is None:
            raise RuntimeError("memory manager is unavailable")
        unknown = set(arguments) - {"kind", "scope", "content"}
        if unknown:
            raise ValueError(
                f"unknown remember argument(s): {', '.join(sorted(unknown))}"
            )
        kind = arguments.get("kind")
        scope = arguments.get("scope")
        content = arguments.get("content")
        if kind not in {"preference", "fact", "decision"}:
            raise ValueError("kind must be preference, fact, or decision")
        if scope not in {"global", "workspace"}:
            raise ValueError("scope must be global or workspace")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("content must be a non-empty string")
        context.memory.remember(
            MemoryCandidate(kind=kind, scope=scope, content=content.strip())
        )
        return {"accepted": True, "scope": scope, "kind": kind}
