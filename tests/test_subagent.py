import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

from agent_core import (
    AgentConfig,
    JobManager,
    JsonlSessionStore,
    LLMProvider,
    LLMResponse,
    Session,
    SubagentRole,
    SubagentRoleRegistry,
    SubagentRuntime,
    SubagentTool,
    ToolCall,
    ToolConfig,
    ToolExecutionContext,
    Workspace,
    WorkspaceInstruction,
    WorkspaceInstructions,
    builtin_catalog,
)
from agent_core.llm import LLMRequest
from agent_core.session_paths import session_directory, workspace_key


SUBAGENT_CONFIG = AgentConfig(
    max_same_tool_calls=5,
    output_reserve_tokens=100,
    tools=ToolConfig(enabled=()),
    workspace_instruction_files=(),
)
SUBAGENT_PROMPT = (
    "Role {{role}}: {{role_description}}\nWorkspace: {{workspace}}"
)
CONSOLIDATOR_PROMPT = "Consolidate the conversation."


class StaticProvider(LLMProvider):
    def __init__(self, answer: str = "child answer") -> None:
        self.answer = answer
        self.requests = []

    @property
    def max_context_tokens(self) -> int:
        return 1000

    def count_input_tokens(self, request: LLMRequest) -> int:
        return 1

    def stream(
        self,
        request: LLMRequest,
        on_text_delta,
        on_reasoning_delta=None,
    ) -> LLMResponse:
        self.requests.append(request)
        on_text_delta(self.answer)
        return LLMResponse(content=self.answer)


class ToolCallingProvider(StaticProvider):
    def __init__(self) -> None:
        super().__init__()
        self._responses = iter(
            (
                LLMResponse(
                    content=None,
                    tool_calls=(
                        ToolCall(
                            "call-child",
                            "read_file",
                            {"path": "large.txt"},
                        ),
                    ),
                ),
                LLMResponse(content=self.answer),
            )
        )

    def stream(
        self,
        request: LLMRequest,
        on_text_delta,
        on_reasoning_delta=None,
    ) -> LLMResponse:
        self.requests.append(request)
        response = next(self._responses)
        if response.content:
            on_text_delta(response.content)
        return response


def role(
    name: str,
    description: str,
    tools: tuple[str, ...] = (),
    *,
    provider: LLMProvider | None = None,
) -> SubagentRole:
    return SubagentRole(
        name=name,
        description=description,
        tools=tools,
        provider=provider or StaticProvider(),
    )


RESEARCHER = role(
    "researcher",
    "Read the workspace and report findings.",
    ("read_file", "list_directory"),
)
CODER = role(
    "coder",
    "Implement a change.",
    ("read_file", "edit_file"),
)


def runtime(
    catalog,
    roles,
    instructions: WorkspaceInstructions | None = None,
) -> SubagentRuntime:
    return SubagentRuntime(
        config=SUBAGENT_CONFIG,
        catalog=catalog,
        roles=SubagentRoleRegistry(roles),
        subagent_prompt_template=SUBAGENT_PROMPT,
        consolidator_prompt=CONSOLIDATOR_PROMPT,
        workspace_instructions=(
            instructions or WorkspaceInstructions((), "empty")
        ),
    )


def context(root: Path, roles, *, session: Session | None = None):
    catalog = builtin_catalog()
    return ToolExecutionContext(
        workspace=Workspace(root),
        session=session or Session(),
        sessions_directory=root / "sessions",
        subagents=runtime(catalog, roles),
    )


class SubagentRoleRegistryTest(unittest.TestCase):
    def test_rejects_duplicate_role_names(self) -> None:
        with self.assertRaisesRegex(ValueError, "already registered"):
            SubagentRoleRegistry((RESEARCHER, RESEARCHER))

    def test_reports_available_roles_for_an_unknown_name(self) -> None:
        registry = SubagentRoleRegistry((RESEARCHER, CODER))

        with self.assertRaisesRegex(
            ValueError, "unknown subagent role 'writer'.*researcher, coder"
        ):
            registry.get("writer")


class SubagentToolTest(unittest.TestCase):
    def test_exposes_every_role_in_its_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            definition = SubagentTool().definition(
                context(Path(directory), (RESEARCHER, CODER))
            )

        self.assertEqual(
            definition.parameters["properties"]["role"]["enum"],
            ["researcher", "coder"],
        )
        self.assertEqual(
            set(definition.parameters["required"]), {"role", "task"}
        )
        self.assertIn(RESEARCHER.description, definition.description)
        self.assertIn(CODER.description, definition.description)

    def test_is_available_only_with_roles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            empty_runtime = runtime(builtin_catalog(), ())

            self.assertFalse(
                SubagentTool().available(
                    ToolExecutionContext(
                        workspace=Workspace(root), session=Session()
                    )
                )
            )
            self.assertFalse(
                SubagentTool().available(
                    ToolExecutionContext(
                        workspace=Workspace(root),
                        session=Session(),
                        subagents=empty_runtime,
                    )
                )
            )
            self.assertTrue(
                SubagentTool().available(context(root, (RESEARCHER,)))
            )

    def test_rejects_an_unknown_role(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "unknown subagent role"):
                SubagentTool().execute(
                    {"role": "writer", "task": "do it"},
                    context(Path(directory), (RESEARCHER,)),
                )

    def test_validates_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tool_context = context(Path(directory), (RESEARCHER,))
            tool = SubagentTool()

            with self.assertRaisesRegex(ValueError, "'role'"):
                tool.execute({"task": "do it"}, tool_context)
            with self.assertRaisesRegex(ValueError, "'task'"):
                tool.execute(
                    {"role": "researcher", "task": " "}, tool_context
                )
            with self.assertRaisesRegex(ValueError, "accepts only"):
                tool.execute(
                    {"role": "researcher", "task": "x", "extra": 1},
                    tool_context,
                )

    def test_background_call_returns_a_job_handle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = Session("parent")
            base = context(root, (RESEARCHER,), session=session)
            jobs = JobManager(session, max_workers=1)
            tool_context = replace(base, jobs=jobs)
            session.begin_turn("turn-1")

            result = SubagentTool().execute(
                {
                    "role": "researcher",
                    "task": "look around",
                    "background": True,
                },
                tool_context,
            )
            jobs.wait_for_turn("turn-1")

            self.assertEqual(result["kind"], "subagent")
            self.assertEqual(result["status"], "submitted")
            self.assertIn(
                "background",
                SubagentTool().definition(tool_context).parameters["properties"],
            )
            jobs.close()


class SubagentRuntimeTest(unittest.TestCase):
    def test_inherits_workspace_instructions_in_system_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            provider = StaticProvider()
            configured_role = role(
                "researcher",
                "Reads.",
                provider=provider,
            )
            instructions = WorkspaceInstructions(
                (
                    WorkspaceInstruction(
                        "<workspace>/AGENTS.md",
                        "subagents must follow this rule",
                    ),
                ),
                "instructions",
            )
            tool_context = ToolExecutionContext(
                workspace=Workspace(root),
                session=Session(),
                sessions_directory=root / "sessions",
                subagents=runtime(
                    builtin_catalog(),
                    (configured_role,),
                    instructions,
                ),
            )

            SubagentTool().execute(
                {"role": "researcher", "task": "inspect"},
                tool_context,
            )

        self.assertIn(
            "subagents must follow this rule",
            provider.requests[0].system_prompt,
        )

    def test_keeps_child_transcript_out_of_the_session_list(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = Session()
            tool_context = context(root, (RESEARCHER,), session=parent)

            result = SubagentTool().execute(
                {
                    "role": "researcher",
                    "task": "write the quarterly summary",
                },
                tool_context,
            )

            subagents = (
                session_directory(
                    tool_context.sessions_directory,
                    tool_context.workspace.path,
                    parent.session_id,
                )
                / "subagents"
            )
            transcripts = list(subagents.rglob("*.jsonl"))
            child_id = transcripts[0].parent.name
            child = JsonlSessionStore(
                subagents, group_by_workspace=False
            ).load(child_id)
            visible_sessions = JsonlSessionStore(
                tool_context.sessions_directory
            ).list_sessions()

        self.assertEqual(result, "child answer")
        self.assertEqual(len(transcripts), 1)
        self.assertEqual(
            [item.content for item in child.items],
            ["write the quarterly summary", "child answer"],
        )
        self.assertEqual(visible_sessions, [])

    def test_saves_child_tool_results_beside_the_child_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "large.txt").write_text("x" * 20000, encoding="utf-8")
            parent = Session("parent")
            provider = ToolCallingProvider()
            child_role = role(
                "researcher",
                "Reads.",
                ("read_file",),
                provider=provider,
            )
            tool_context = context(root, (child_role,), session=parent)

            result = SubagentTool().execute(
                {"role": "researcher", "task": "read the large file"},
                tool_context,
            )

            subagents = (
                session_directory(
                    tool_context.sessions_directory,
                    tool_context.workspace.path,
                    parent.session_id,
                )
                / "subagents"
            )
            transcript = next(subagents.rglob("*.jsonl"))
            child_id = transcript.parent.name
            artifact = transcript.parent / "call-child.txt"
            child = JsonlSessionStore(
                subagents, group_by_workspace=False
            ).load(child_id)
            tool_message = next(
                item for item in child.items if item.role == "tool"
            )
            artifact_path = (
                f".nosis/sessions/{workspace_key(root)}/parent/subagents/"
                f"{child_id}/call-child.txt"
            )
            misplaced_artifact = (
                tool_context.sessions_directory
                / workspace_key(root)
                / child_id
                / "call-child.txt"
            )

            self.assertEqual(result, "child answer")
            self.assertTrue(artifact.is_file())
            self.assertEqual(
                json.loads(tool_message.content)["artifact_path"],
                artifact_path,
            )
            self.assertFalse(misplaced_artifact.exists())

    def test_role_receives_only_its_declared_tools(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reader_provider = StaticProvider()
            writer_provider = StaticProvider()
            roles = (
                role(
                    "reader",
                    "Reads.",
                    ("read_file", "list_directory"),
                    provider=reader_provider,
                ),
                role(
                    "writer",
                    "Writes.",
                    ("read_file", "edit_file"),
                    provider=writer_provider,
                ),
            )
            tool_context = context(root, roles)
            tool = SubagentTool()

            tool.execute({"role": "reader", "task": "look"}, tool_context)
            tool.execute({"role": "writer", "task": "change"}, tool_context)

        self.assertEqual(
            [item.name for item in reader_provider.requests[0].tools],
            ["read_file", "list_directory"],
        )
        self.assertEqual(
            [item.name for item in writer_provider.requests[0].tools],
            ["read_file", "edit_file"],
        )

    def test_child_cannot_delegate_again(self) -> None:
        captured = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = builtin_catalog()
            recursive = role(
                "recursive",
                "Tries to delegate again.",
                ("subagent", "read_file"),
            )
            subagents = runtime(catalog, (recursive,))
            tool_context = ToolExecutionContext(
                workspace=Workspace(root),
                session=Session(),
                sessions_directory=root / "sessions",
                subagents=subagents,
            )
            original_select = catalog.select

            def record_select(names, child_context, **kwargs):
                tools = original_select(names, child_context, **kwargs)
                captured.append(
                    (
                        [item.name for item in tools.definitions],
                        child_context.subagents,
                    )
                )
                return tools

            catalog.select = record_select
            SubagentTool().execute(
                {"role": "recursive", "task": "delegate again"},
                tool_context,
            )

        self.assertEqual(captured, [(["read_file"], None)])

    def test_roles_run_on_their_own_providers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            roles = (
                role("cheap", "Reads.", provider=StaticProvider("cheap")),
                role("strong", "Writes.", provider=StaticProvider("strong")),
            )
            tool_context = context(Path(directory), roles)
            tool = SubagentTool()

            cheap = tool.execute(
                {"role": "cheap", "task": "look"}, tool_context
            )
            strong = tool.execute(
                {"role": "strong", "task": "change"}, tool_context
            )

        self.assertEqual((cheap, strong), ("cheap", "strong"))
        for configured_role in roles:
            request = configured_role.provider.requests[0]
            self.assertIn(f"Role {configured_role.name}", request.system_prompt)
            self.assertIn(
                configured_role.description,
                request.system_prompt,
            )

    def test_parallel_calls_get_separate_transcripts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = Session()
            tool_context = context(
                root, (RESEARCHER, CODER), session=parent
            )
            tool = SubagentTool()
            subagents = tool_context.subagents
            assert subagents is not None
            original_run = subagents.run
            barrier = threading.Barrier(8)

            def synchronized_run(role_name, task, parent_context):
                barrier.wait()
                return original_run(role_name, task, parent_context)

            subagents.run = synchronized_run

            with ThreadPoolExecutor(max_workers=8) as executor:
                results = list(
                    executor.map(
                        lambda index: tool.execute(
                            {
                                "role": "researcher" if index % 2 else "coder",
                                "task": f"task {index}",
                            },
                            tool_context,
                        ),
                        range(8),
                    )
                )

            transcript_directory = (
                session_directory(
                    tool_context.sessions_directory,
                    tool_context.workspace.path,
                    parent.session_id,
                )
                / "subagents"
            )
            transcripts = list(transcript_directory.rglob("*.jsonl"))
            tasks = [
                JsonlSessionStore(
                    transcript.parent.parent,
                    group_by_workspace=False,
                ).load(transcript.parent.name).items[0].content
                for transcript in transcripts
            ]

        self.assertEqual(results, ["child answer"] * 8)
        self.assertEqual(len(transcripts), 8)
        self.assertEqual(
            sorted(tasks), sorted(f"task {index}" for index in range(8))
        )

    def test_child_does_not_mutate_parent_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Session()
            tool_context = context(
                Path(directory), (RESEARCHER,), session=parent
            )

            SubagentTool().execute(
                {"role": "researcher", "task": "look around"},
                tool_context,
            )

        self.assertEqual(parent.items, [])


if __name__ == "__main__":
    unittest.main()
