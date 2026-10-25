"""Deterministic providers and Agent inputs shared across test layers."""

from pathlib import Path

from agent_core import (
    Agent, AgentConfig, LLMProvider, LLMRequest, LLMResponse, Session, Tool,
    ToolCatalog, ToolConfig, ToolDefinition, ToolExecutionContext, Workspace,
)


AGENT_CONFIG = AgentConfig(
    max_same_tool_calls=5,
    output_reserve_tokens=100,
    tools=ToolConfig(enabled=()),
    workspace_instruction_files=(),
)

CONSOLIDATOR_PROMPT = "Consolidate the conversation."

TEST_WORKSPACE = Workspace(Path(__file__).parent)


def tool_set(*tools, session=None, workspace=TEST_WORKSPACE, policy=None, **context_fields):
    """Select every given Tool, as a Runtime would from its catalog."""
    context = ToolExecutionContext(
        workspace=workspace,
        session=session if session is not None else Session(),
        **context_fields,
    )
    catalog = ToolCatalog(tools)
    return catalog.select(catalog.names, context, policy=policy)


def make_agent(
    provider,
    session,
    *,
    tools=(),
    config=AGENT_CONFIG,
    now=None,
    system_prompt="You are helpful.",
) -> Agent:
    context = ToolExecutionContext(workspace=TEST_WORKSPACE, session=session)
    catalog = ToolCatalog(tools)
    return Agent(
        provider=provider,
        session=session,
        system_prompt=system_prompt,
        consolidator_prompt=CONSOLIDATOR_PROMPT,
        config=config,
        tools=catalog.select(catalog.names, context),
        context=context,
        now=now,
    )


class MockProvider(LLMProvider):
    def __init__(
        self,
        responses: list[str | LLMResponse],
        input_tokens: int | list[int] = 1,
        max_context_tokens: int = 1000,
        max_output_tokens: int | None = None,
    ) -> None:
        self._responses = iter(responses)
        self._input_tokens = (
            iter(input_tokens) if isinstance(input_tokens, list) else input_tokens
        )
        self._max_context_tokens = max_context_tokens
        self._max_output_tokens = max_output_tokens
        self.counted_requests: list[LLMRequest] = []
        self.requests: list[LLMRequest] = []

    @property
    def max_context_tokens(self) -> int:
        return self._max_context_tokens

    @property
    def max_output_tokens(self) -> int | None:
        return self._max_output_tokens

    def count_input_tokens(self, request: LLMRequest) -> int:
        self.counted_requests.append(request)
        if isinstance(self._input_tokens, int):
            return self._input_tokens
        return next(self._input_tokens)

    def stream(
        self,
        request: LLMRequest,
        on_text_delta,
        on_reasoning_delta=None,
    ) -> LLMResponse:
        self.requests.append(request)
        response = next(self._responses)
        if isinstance(response, str):
            response = LLMResponse(content=response)
        if response.reasoning and on_reasoning_delta is not None:
            on_reasoning_delta(response.reasoning)
        if response.content:
            on_text_delta(response.content)
        return response


class EchoTool(Tool):
    name = "echo"

    def definition(self, context) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description="Echo the provided text.",
            parameters={
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                },
                "required": ["text"],
                "additionalProperties": False,
            },
        )

    def execute(self, arguments, context):
        return {"text": arguments["text"]}
