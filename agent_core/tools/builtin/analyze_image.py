from ...content import TextPart
from ...session import Message
from ...llm import LLMRequest
from ..base import JSONValue, Tool, ToolDefinition
from ..context import ToolExecutionContext
from ..paths import resolve_image


class AnalyzeImageTool(Tool):
    """Ask a vision-capable provider about an image stored in the workspace.

    This is the fallback for a model that cannot see images: it spends a
    second provider call to turn pixels into text.  A model that accepts
    image input gets ``read_image`` instead and looks for itself.
    """

    name = "analyze_image"

    def available(self, context: ToolExecutionContext) -> bool:
        return context.vision_provider is not None and not context.vision_input

    def definition(self, context: ToolExecutionContext) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description="Analyze an image attachment and answer a question about it.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "question": {"type": "string"},
                },
                "required": ["path", "question"],
            },
        )

    def execute(
        self,
        arguments: dict[str, JSONValue],
        context: ToolExecutionContext,
    ) -> JSONValue:
        path = arguments.get("path")
        question = arguments.get("question")
        if not isinstance(path, str) or not path:
            raise ValueError("path must be a non-empty string")
        if not isinstance(question, str) or not question:
            raise ValueError("question must be a non-empty string")
        provider = context.vision_provider
        if provider is None:
            raise ValueError("this runtime has no vision provider")
        if "image" not in provider.capabilities.input_modalities:
            raise ValueError("the configured provider does not support image input")
        # The media type is read from the file itself, so a mislabelled
        # extension cannot make the request claim the wrong format.
        image, _info = resolve_image(path, context)
        response = provider.stream(
            LLMRequest(
                system_prompt="Analyze the supplied image and answer the user's question.",
                messages=(
                    Message(
                        role="user",
                        content=(
                            TextPart(text=question),
                            image,
                        ),
                    ),
                ),
                media_root=context.workspace.path,
            ),
            lambda _text: None,
        )
        if response.tool_calls or not response.content:
            raise ValueError("image analysis provider returned no text")
        return response.content
