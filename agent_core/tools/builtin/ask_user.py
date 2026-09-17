from ..base import JSONValue, Tool, ToolDefinition
from ..context import ToolExecutionContext


class AskUserTool(Tool):
    name = "ask_user"

    def available(self, context: ToolExecutionContext) -> bool:
        return context.ask_user is not None

    def definition(self, context: ToolExecutionContext) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=(
                "Ask the user to choose from a short list when their input is "
                "needed to continue. Use this instead of guessing a preference."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The concise question shown to the user.",
                    },
                    "options": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "label": {"type": "string"},
                                "description": {"type": "string"},
                                "recommended": {"type": "boolean"},
                            },
                            "required": ["id", "label"],
                            "additionalProperties": False,
                        },
                    },
                    "allow_free_text": {
                        "type": "boolean",
                        "description": (
                            "Whether the user may provide an answer outside the "
                            "listed options."
                        ),
                        "default": False,
                    },
                },
                "required": ["question", "options"],
                "additionalProperties": False,
            },
        )

    def execute(
        self,
        arguments: dict[str, JSONValue],
        context: ToolExecutionContext,
    ) -> JSONValue:
        unknown_arguments = set(arguments) - {
            "question",
            "options",
            "allow_free_text",
        }
        if unknown_arguments:
            fields = ", ".join(sorted(unknown_arguments))
            raise ValueError(f"unknown argument(s): {fields}")
        question = arguments.get("question")
        if not isinstance(question, str) or not question.strip():
            raise ValueError("'question' must be a non-empty string")

        raw_options = arguments.get("options")
        if not isinstance(raw_options, list) or not raw_options:
            raise ValueError("'options' must be a non-empty array")

        options: list[dict[str, JSONValue]] = []
        option_ids: set[str] = set()
        for index, raw_option in enumerate(raw_options):
            if not isinstance(raw_option, dict):
                raise ValueError(f"'options[{index}]' must be an object")
            unknown = set(raw_option) - {
                "id",
                "label",
                "description",
                "recommended",
            }
            if unknown:
                fields = ", ".join(sorted(unknown))
                raise ValueError(
                    f"unknown field(s) in 'options[{index}]': {fields}"
                )
            option_id = raw_option.get("id")
            label = raw_option.get("label")
            description = raw_option.get("description")
            recommended = raw_option.get("recommended", False)
            if not isinstance(option_id, str) or not option_id.strip():
                raise ValueError(
                    f"'options[{index}].id' must be a non-empty string"
                )
            option_id = option_id.strip()
            if option_id in option_ids:
                raise ValueError(f"duplicate option id: {option_id}")
            if not isinstance(label, str) or not label.strip():
                raise ValueError(
                    f"'options[{index}].label' must be a non-empty string"
                )
            if description is not None and not isinstance(description, str):
                raise ValueError(
                    f"'options[{index}].description' must be a string"
                )
            if not isinstance(recommended, bool):
                raise ValueError(
                    f"'options[{index}].recommended' must be a boolean"
                )
            label = label.strip()
            option_ids.add(option_id)
            option: dict[str, JSONValue] = {
                "id": option_id,
                "label": label,
            }
            if description:
                option["description"] = description
            if recommended:
                option["recommended"] = True
            options.append(option)

        allow_free_text = arguments.get("allow_free_text", False)
        if not isinstance(allow_free_text, bool):
            raise ValueError("'allow_free_text' must be a boolean")
        if context.ask_user is None:
            raise RuntimeError("user interaction is unavailable")
        question = question.strip()
        result = context.ask_user(question, options, allow_free_text)
        if isinstance(result, dict) and result.get("type") == "option":
            option_id = result.get("id")
            label = result.get("label")
            context.session.record_user_interaction(
                f"Question: {question}\n"
                f"User selected: {option_id} ({label})",
                "question_response",
            )
        elif isinstance(result, dict) and result.get("type") == "text":
            answer = result.get("text")
            if isinstance(answer, str):
                context.session.record_user_interaction(
                    f"Question: {question}\nUser answer: {answer}",
                    "question_response",
                )
        return result
