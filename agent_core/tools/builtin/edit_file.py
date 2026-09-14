from ..base import JSONValue, Tool, ToolDefinition
from ..context import ToolExecutionContext


class EditFileTool(Tool):
    name = "edit_file"

    def definition(self, context: ToolExecutionContext) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=(
                "Replace one exact text occurrence in a UTF-8 workspace file."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path relative to the workspace root.",
                    },
                    "old_text": {
                        "type": "string",
                        "description": "Exact text to replace.",
                    },
                    "new_text": {
                        "type": "string",
                        "description": "Replacement text.",
                    },
                },
                "required": ["path", "old_text", "new_text"],
                "additionalProperties": False,
            },
        )

    def execute(
        self,
        arguments: dict[str, JSONValue],
        context: ToolExecutionContext,
    ) -> JSONValue:
        path = arguments.get("path")
        old_text = arguments.get("old_text")
        new_text = arguments.get("new_text")

        if not isinstance(path, str) or not path:
            raise ValueError("edit_file requires a non-empty string 'path'")
        if not isinstance(old_text, str) or not old_text:
            raise ValueError(
                "edit_file requires a non-empty string 'old_text'"
            )
        if not isinstance(new_text, str):
            raise ValueError("edit_file requires a string 'new_text'")
        if set(arguments) != {"path", "old_text", "new_text"}:
            raise ValueError(
                "edit_file accepts only 'path', 'old_text', and 'new_text'"
            )

        workspace = context.workspace
        file_path = workspace.resolve_path(path)
        content = file_path.read_bytes().decode("utf-8")
        occurrences = content.count(old_text)
        if occurrences == 0:
            raise ValueError("old_text was not found in the file")
        if occurrences > 1:
            raise ValueError(
                f"old_text appears {occurrences} times; it must be unique"
            )

        file_path.write_bytes(
            content.replace(old_text, new_text, 1).encode("utf-8")
        )
        return {
            "path": file_path.relative_to(workspace.path).as_posix(),
            "replacements": 1,
        }
