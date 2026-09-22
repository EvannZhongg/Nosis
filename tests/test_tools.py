import json
import os
import subprocess
import threading
import unittest
import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_core import (
    ApplyPatchTool,
    AskUserTool,
    CommandExecutionResult,
    EditFileTool,
    ExecutionRouter,
    ExecutionScope,
    FULL_ACCESS_AUTHORITY,
    JsonlSessionStore,
    ListDirectoryTool,
    ReadFileTool,
    SearchFilesTool,
    Session,
    ShellTool,
    Tool,
    ToolCall,
    ToolCatalog,
    ToolDefinition,
    ToolExecutionContext,
    ToolResult,
    WORKSPACE_ONLY_AUTHORITY,
    WebSearchTool,
    Workspace,
    builtin_catalog,
)
from agent_core.permissions import PermissionPreset
from agent_core.scheduler import CronTrigger, IntervalTrigger, SchedulerService
from agent_core.tools.builtin.schedule import (
    CreateScheduledTaskTool,
    DeleteScheduledTaskTool,
    ListScheduledTasksTool,
    UpdateScheduledTaskTool,
)
from agent_core.tools.budget import MAX_TOOL_RESULT_CHARS
from agent_core.tools.builtin.read_file import (
    MAX_FILE_SIZE_BYTES as MAX_READ_FILE_SIZE_BYTES,
)


def _symlinks_available() -> bool:
    """Report whether this account may create symlinks.

    Windows needs Developer Mode or administrator rights.
    """
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory, "target.txt")
        target.write_text("x", encoding="utf-8")
        try:
            Path(directory, "link.txt").symlink_to(target)
        except OSError:
            return False
    return True


SYMLINKS_AVAILABLE = _symlinks_available()

# Tools that never touch the filesystem still need a workspace in their
# context; the test package directory is a stable, existing one.
_TMP_WORKSPACE = Workspace(Path(__file__).parent)


class _Bound:
    """A Tool plus the context a test calls it with.

    Production code reaches tools through a ToolSet; these per-tool tests
    exercise one implementation directly, so they bind a context once.
    """

    def __init__(self, tool, workspace, **fields):
        self._tool = tool
        self._context = context_for(workspace, **fields)

    @property
    def definition(self):
        return self._tool.definition(self._context)

    def execute(self, arguments):
        context = self._context
        if context.execution_router is not None:
            call = ToolCall("test-call", self._tool.name, arguments)
            context = replace(
                context,
                execution=context.execution_router.resolve(call),
            )
        return self._tool.execute(arguments, context)


def context_for(workspace, **fields):
    return ToolExecutionContext(
        workspace=workspace,
        session=fields.pop("session", None) or Session(),
        **fields,
    )


def scheduled_context_for(workspace, **fields):
    session = fields.pop("session", None) or Session()
    router = fields.pop("execution_router", None) or ExecutionRouter(
        UnusedExecutor(),
        UnusedExecutor(),
        authority=lambda: session.permission_preset.authority,
    )
    return context_for(
        workspace,
        session=session,
        execution_router=router,
        **fields,
    )


def execute_scheduled(arguments, context):
    router = context.execution_router
    assert router is not None
    call = ToolCall("test-call", "create_scheduled_task", arguments)
    return CreateScheduledTaskTool().execute(
        arguments,
        replace(context, execution=router.resolve(call)),
    )


class FailingTool(Tool):
    name = "failing"

    def definition(self, context) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description="Always fails.",
            parameters={"type": "object", "properties": {}},
        )

    def execute(self, arguments, context):
        raise ValueError("bad input")


class NeedsExecutorTool(Tool):
    name = "needs_executor"

    def available(self, context) -> bool:
        return context.execution_router is not None

    def definition(self, context) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description="Needs a command executor.",
            parameters={"type": "object", "properties": {}},
        )

    def execute(self, arguments, context):
        return "ran"


class ConcurrentTool(FailingTool):
    name = "concurrent"
    concurrent = True


class ToolCatalogTest(unittest.TestCase):
    def test_rejects_duplicate_tool_names(self) -> None:
        with self.assertRaisesRegex(ValueError, "already registered"):
            ToolCatalog((FailingTool(), FailingTool()))

    def test_selects_only_the_names_a_role_asks_for(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = context_for(Workspace(Path(directory)))
            catalog = builtin_catalog()

            tools = catalog.select(("read_file", "list_directory"), context)

            self.assertEqual(
                [definition.name for definition in tools.definitions],
                ["read_file", "list_directory"],
            )

    def test_skips_names_the_catalog_does_not_know(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = context_for(Workspace(Path(directory)))

            tools = builtin_catalog().select(("read_file", "nope"), context)

            self.assertEqual(
                [definition.name for definition in tools.definitions],
                ["read_file"],
            )

    def test_omits_tools_whose_runtime_dependency_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            catalog = ToolCatalog((NeedsExecutorTool(),))

            without = catalog.select(
                ("needs_executor",), context_for(workspace)
            )
            with_executor = catalog.select(
                ("needs_executor",),
                context_for(
                    workspace,
                    execution_router=ExecutionRouter(
                        UnusedExecutor(), UnusedExecutor()
                    ),
                ),
            )

            self.assertEqual(without.definitions, ())
            self.assertEqual(
                [d.name for d in with_executor.definitions],
                ["needs_executor"],
            )

    def test_two_roles_share_one_tool_instance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = context_for(Workspace(Path(directory)))
            catalog = builtin_catalog()

            reader = catalog.select(("read_file", "list_directory"), context)
            writer = catalog.select(("read_file", "edit_file"), context)

            self.assertIs(
                reader._tools["read_file"],
                writer._tools["read_file"],
            )

    def test_extend_keeps_the_original_catalog_unchanged(self) -> None:
        base = ToolCatalog((FailingTool(),))

        extended = base.extend((NeedsExecutorTool(),))

        self.assertEqual(base.names, ("failing",))
        self.assertEqual(
            extended.names, ("failing", "needs_executor")
        )


class ScheduledTaskToolTest(unittest.TestCase):
    def test_create_schema_declares_trigger_properties_without_one_of(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            definition = CreateScheduledTaskTool().definition(
                scheduled_context_for(Workspace(Path(directory)))
            )

        trigger_schema = definition.parameters["properties"]["trigger"]
        self.assertNotIn("oneOf", trigger_schema)
        self.assertEqual(
            trigger_schema["properties"]["type"]["enum"],
            ["once", "interval", "cron"],
        )
        self.assertIn("explicit UTC offset", trigger_schema["properties"]["at"]["description"])
        self.assertEqual(
            trigger_schema["properties"]["at"]["pattern"],
            "(?:Z|[+-][0-9]{2}:[0-9]{2})$",
        )
        self.assertEqual(
            definition.parameters["properties"]["execution_scope"]["enum"],
            ["workspace"],
        )

    def test_full_access_may_create_an_unattended_host_schedule(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = Workspace(root)
            sessions_directory = root / "sessions"
            store = JsonlSessionStore(sessions_directory)
            session = Session("origin")
            session.permission_preset = PermissionPreset.FULL_ACCESS
            scheduler = SchedulerService(root / "schedule.jsonl")
            context = scheduled_context_for(
                workspace,
                session=session,
                sessions_directory=sessions_directory,
                scheduler=scheduler,
            )
            tool = CreateScheduledTaskTool()

            self.assertEqual(
                tool.definition(context).parameters["properties"]
                ["execution_scope"]["enum"],
                ["workspace", "host"],
            )
            result = execute_scheduled(
                {
                    "prompt": "host scheduled prompt",
                    "trigger": {
                        "type": "once",
                        "at": "2099-01-01T00:00:00+00:00",
                    },
                    "execution_scope": "host",
                },
                context,
            )

            self.assertEqual(result["execution_scope"], "host")
            self.assertIs(
                scheduler.schedules[0].execution_scope,
                ExecutionScope.HOST,
            )
            self.assertEqual(
                store.permission_preset_for(result["schedule_session_id"]),
                PermissionPreset.FULL_ACCESS,
            )

    def test_non_full_access_cannot_create_a_host_schedule(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scheduler = SchedulerService(root / "schedule.jsonl")
            context = scheduled_context_for(
                Workspace(root),
                sessions_directory=root / "sessions",
                scheduler=scheduler,
            )

            with self.assertRaisesRegex(
                PermissionError,
                "host scheduled tasks require Full Access",
            ):
                execute_scheduled(
                    {
                        "prompt": "host scheduled prompt",
                        "trigger": {
                            "type": "once",
                            "at": "2099-01-01T00:00:00+00:00",
                        },
                        "execution_scope": "host",
                    },
                    context,
                )

            self.assertEqual(scheduler.schedules, [])

    def test_runtime_authority_caps_a_full_access_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = Session("origin")
            session.permission_preset = PermissionPreset.FULL_ACCESS
            scheduler = SchedulerService(root / "schedule.jsonl")
            context = scheduled_context_for(
                Workspace(root),
                session=session,
                sessions_directory=root / "sessions",
                scheduler=scheduler,
                execution_router=ExecutionRouter(
                    UnusedExecutor(),
                    UnusedExecutor(),
                    authority=WORKSPACE_ONLY_AUTHORITY,
                ),
            )

            self.assertEqual(
                CreateScheduledTaskTool().definition(context).parameters
                ["properties"]["execution_scope"]["enum"],
                ["workspace"],
            )
            with self.assertRaisesRegex(
                PermissionError,
                "host scheduled tasks require Full Access",
            ):
                execute_scheduled(
                    {
                        "prompt": "host scheduled prompt",
                        "trigger": {
                            "type": "once",
                            "at": "2099-01-01T00:00:00+00:00",
                        },
                        "execution_scope": "host",
                    },
                    context,
                )

    def test_delete_schedule_is_model_callable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scheduler = SchedulerService(root / "schedule.jsonl")
            context = scheduled_context_for(
                Workspace(root),
                sessions_directory=root / "sessions",
                scheduler=scheduler,
            )
            created = execute_scheduled(
                {
                    "prompt": "scheduled prompt",
                    "trigger": {"type": "once", "at": "2099-01-01T00:00:00+00:00"},
                },
                context,
            )
            toolset = builtin_catalog().select(("delete_scheduled_task",), context)
            result = toolset.execute(
                ToolCall(
                    id="delete-1",
                    name="delete_scheduled_task",
                    arguments={"schedule_id": created["schedule_id"]},
                )
            )

            self.assertEqual(json.loads(result.to_content())["ok"], True)
            self.assertEqual(scheduler.schedules, [])

    def test_create_interval_schedule(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scheduler = SchedulerService(root / "schedule.jsonl")
            context = scheduled_context_for(
                Workspace(root),
                sessions_directory=root / "sessions",
                scheduler=scheduler,
            )
            execute_scheduled(
                {
                    "prompt": "scheduled prompt",
                    "trigger": {"type": "interval", "seconds": 90},
                },
                context,
            )
            self.assertIsInstance(scheduler.schedules[0].trigger, IntervalTrigger)
            self.assertEqual(scheduler.schedules[0].trigger.seconds, 90)

    def test_update_trigger_and_end_at_preserves_schedule_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scheduler = SchedulerService(root / "schedule.jsonl")
            context = scheduled_context_for(
                Workspace(root),
                sessions_directory=root / "sessions",
                scheduler=scheduler,
            )
            created = execute_scheduled(
                {
                    "prompt": "scheduled prompt",
                    "trigger": {"type": "interval", "seconds": 1200},
                },
                context,
            )

            result = UpdateScheduledTaskTool().execute(
                {
                    "schedule_id": created["schedule_id"],
                    "trigger": {
                        "type": "cron",
                        "expression": "0 10 * * *",
                        "timezone": "Asia/Shanghai",
                    },
                    "end_at": "2099-12-31T23:59:00+08:00",
                },
                context,
            )

            schedule = scheduler.schedules[0]
            self.assertEqual(result["schedule_id"], created["schedule_id"])
            self.assertEqual(
                result["schedule_session_id"], created["schedule_session_id"]
            )
            self.assertIsInstance(schedule.trigger, CronTrigger)
            self.assertEqual(schedule.trigger.expression, "0 10 * * *")
            self.assertEqual(schedule.trigger.timezone, "Asia/Shanghai")
            self.assertEqual(
                result["end_at"], "2099-12-31T23:59:00+08:00"
            )

    def test_schedule_tools_reject_coercion_and_unknown_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scheduler = SchedulerService(root / "schedule.jsonl")
            context = scheduled_context_for(
                Workspace(root),
                sessions_directory=root / "sessions",
                scheduler=scheduler,
            )
            created = execute_scheduled(
                {
                    "prompt": "scheduled prompt",
                    "trigger": {"type": "interval", "seconds": 60},
                },
                context,
            )

            with self.assertRaisesRegex(ValueError, "prompt must be"):
                UpdateScheduledTaskTool().execute(
                    {"schedule_id": created["schedule_id"], "prompt": " "},
                    context,
                )
            with self.assertRaisesRegex(ValueError, "enabled must be"):
                UpdateScheduledTaskTool().execute(
                    {"schedule_id": created["schedule_id"], "enabled": 1},
                    context,
                )
            with self.assertRaisesRegex(
                ValueError, "unsupported schedule update field"
            ):
                UpdateScheduledTaskTool().execute(
                    {"schedule_id": created["schedule_id"], "unknown": True},
                    context,
                )
            with self.assertRaisesRegex(ValueError, "schedule_id must be"):
                UpdateScheduledTaskTool().execute(
                    {"schedule_id": 123, "prompt": "updated"},
                    context,
                )
            with self.assertRaisesRegex(ValueError, "schedule_id must be"):
                DeleteScheduledTaskTool().execute({"schedule_id": 123}, context)

    def test_list_includes_trigger_and_end_at(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scheduler = SchedulerService(root / "schedule.jsonl")
            context = scheduled_context_for(
                Workspace(root),
                sessions_directory=root / "sessions",
                scheduler=scheduler,
            )
            execute_scheduled(
                {
                    "prompt": "scheduled prompt",
                    "trigger": {"type": "interval", "seconds": 1200},
                    "end_at": "2099-12-31T23:59:00+00:00",
                },
                context,
            )

            item = ListScheduledTasksTool().execute({}, context)[0]

            self.assertEqual(
                item["trigger"],
                {"type": "interval", "seconds": 1200, "start_at": None},
            )
            self.assertEqual(item["end_at"], "2099-12-31T23:59:00+00:00")

    def test_missing_trigger_fields_return_retryable_messages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = scheduled_context_for(
                Workspace(Path(directory)),
                scheduler=SchedulerService(Path(directory) / "schedule.jsonl"),
            )
            tool = CreateScheduledTaskTool()
            with self.assertRaisesRegex(ValueError, "once trigger requires 'at'"):
                execute_scheduled({"prompt": "p", "trigger": {"type": "once"}}, context)
            with self.assertRaisesRegex(ValueError, "cron trigger requires 'expression'"):
                execute_scheduled({"prompt": "p", "trigger": {"type": "cron"}}, context)
            with self.assertRaisesRegex(ValueError, "Retry with seconds"):
                execute_scheduled({"prompt": "p", "trigger": {"type": "interval"}}, context)
            with self.assertRaisesRegex(ValueError, "start_at must be"):
                execute_scheduled(
                    {
                        "prompt": "p",
                        "trigger": {
                            "type": "interval",
                            "seconds": 60,
                            "start_at": 123,
                        },
                    },
                    context,
                )
            with self.assertRaisesRegex(ValueError, "end_at must be"):
                execute_scheduled(
                    {
                        "prompt": "p",
                        "trigger": {"type": "interval", "seconds": 60},
                        "end_at": 123,
                    },
                    context,
                )

    def test_delete_missing_schedule_reports_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = scheduled_context_for(
                Workspace(Path(directory)),
                scheduler=SchedulerService(Path(directory) / "schedule.jsonl"),
            )
            with self.assertRaisesRegex(ValueError, "scheduled task not found"):
                DeleteScheduledTaskTool().execute({"schedule_id": "missing"}, context)
            with self.assertRaisesRegex(ValueError, "scheduled task not found"):
                UpdateScheduledTaskTool().execute({"schedule_id": "missing"}, context)
    def test_new_schedule_session_inherits_provider_and_caps_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = Workspace(root)
            sessions_directory = root / "sessions"
            store = JsonlSessionStore(sessions_directory)
            session = Session("origin")
            session.permission_preset = PermissionPreset.WORKSPACE_ACCESS
            store.bind_workspace(session.session_id, workspace.path)
            store.set_provider(session.session_id, "configured", workspace.path)
            scheduler = SchedulerService(root / "schedule.jsonl")
            context = scheduled_context_for(
                workspace,
                session=session,
                sessions_directory=sessions_directory,
                scheduler=scheduler,
            )

            result = execute_scheduled(
                {
                    "prompt": "scheduled prompt",
                    "trigger": {
                        "type": "once",
                        "at": "2099-01-01T00:00:00+00:00",
                    },
                },
                context,
            )

            scheduled_session = result["schedule_session_id"]
            self.assertEqual(
                store.permission_preset_for(scheduled_session),
                PermissionPreset.WORKSPACE_ACCESS,
            )
            self.assertEqual(
                store.provider_for(scheduled_session),
                "configured",
            )

            session.permission_preset = PermissionPreset.FULL_ACCESS
            second = execute_scheduled(
                {
                    "prompt": "another scheduled prompt",
                    "trigger": {
                        "type": "once",
                        "at": "2099-01-02T00:00:00+00:00",
                    },
                },
                context,
            )
            self.assertEqual(
                store.permission_preset_for(second["schedule_session_id"]),
                PermissionPreset.WORKSPACE_ACCESS,
            )


class ToolSetTest(unittest.TestCase):
    def test_returns_structured_execution_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tools = ToolCatalog((FailingTool(),)).select(
                ("failing",), context_for(Workspace(Path(directory)))
            )

            result = tools.execute(
                ToolCall(id="call-1", name="failing", arguments={})
            )

        self.assertEqual(
            json.loads(result.to_content()),
            {
                "ok": False,
                "error": {
                    "type": "ValueError",
                    "message": "bad input",
                },
            },
        )

    def test_returns_tool_not_found_for_an_unselected_tool(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tools = builtin_catalog().select(
                ("read_file",), context_for(Workspace(Path(directory)))
            )

            result = tools.execute(
                ToolCall(id="call-1", name="shell", arguments={})
            )

        self.assertEqual(
            json.loads(result.to_content())["error"]["type"],
            "tool_not_found",
        )

    def test_returns_structured_policy_error_before_tool_execution(
        self,
    ) -> None:
        executed = []

        class RecordingTool(Tool):
            name = "recording"

            def definition(self, context) -> ToolDefinition:
                return ToolDefinition(
                    name=self.name,
                    description="Record execution.",
                    parameters={"type": "object", "properties": {}},
                )

            def execute(self, arguments, context):
                executed.append(arguments)
                return None

        class DenyPolicy:
            def authorize(self, call, context):
                raise PermissionError(f"{call.name} was denied")

        with tempfile.TemporaryDirectory() as directory:
            tools = ToolCatalog((RecordingTool(),)).select(
                ("recording",),
                context_for(Workspace(Path(directory))),
                policy=DenyPolicy(),
            )

            result = tools.execute(
                ToolCall(id="call-1", name="recording", arguments={})
            )

        self.assertEqual(executed, [])
        self.assertEqual(
            json.loads(result.to_content()),
            {
                "ok": False,
                "error": {
                    "type": "PermissionError",
                    "message": "recording was denied",
                },
            },
        )

    def test_reports_concurrency_only_for_selected_concurrent_tools(self) -> None:
        tools = ToolCatalog((ConcurrentTool(), FailingTool())).select(
            ("concurrent", "failing"), context_for(_TMP_WORKSPACE)
        )

        self.assertTrue(tools.is_concurrent("concurrent"))
        self.assertFalse(tools.is_concurrent("failing"))
        self.assertFalse(tools.is_concurrent("unknown"))


class AskUserToolTest(unittest.TestCase):
    def test_returns_the_runtime_answer(self) -> None:
        requests = []
        session = Session("s")
        session.begin_turn("turn-1")
        tool = _Bound(
            AskUserTool(),
            _TMP_WORKSPACE,
            session=session,
            ask_user=lambda question, options, allow_free_text: (
                requests.append((question, options, allow_free_text))
                or {"type": "option", "id": "sqlite", "label": "SQLite"}
            ),
        )

        result = tool.execute(
            {
                "question": "Which cache?",
                "options": [
                    {"id": "memory", "label": "Memory"},
                    {
                        "id": "sqlite",
                        "label": "SQLite",
                        "description": "Persistent",
                        "recommended": True,
                    },
                ],
                "allow_free_text": True,
            }
        )

        self.assertEqual(
            result,
            {"type": "option", "id": "sqlite", "label": "SQLite"},
        )
        self.assertEqual(requests[0][0], "Which cache?")
        self.assertTrue(requests[0][2])
        anchor = session.user_anchors[-1]
        self.assertEqual(anchor.source, "question_response")
        self.assertIn("sqlite (SQLite)", anchor.content)

    def test_requires_unique_option_ids(self) -> None:
        tool = _Bound(
            AskUserTool(),
            _TMP_WORKSPACE,
            ask_user=lambda *_args: None,
        )

        with self.assertRaisesRegex(ValueError, "duplicate option id"):
            tool.execute(
                {
                    "question": "Which?",
                    "options": [
                        {"id": "same", "label": "First"},
                        {"id": "same", "label": "Second"},
                    ],
                }
            )

    def test_records_free_text_as_a_user_anchor(self) -> None:
        session = Session("s")
        session.begin_turn("turn-1")
        tool = _Bound(
            AskUserTool(),
            _TMP_WORKSPACE,
            session=session,
            ask_user=lambda *_args: {"type": "text", "text": "Redis"},
        )

        tool.execute(
            {
                "question": "Which cache?",
                "options": [{"id": "sqlite", "label": "SQLite"}],
                "allow_free_text": True,
            }
        )

        anchor = session.user_anchors[-1]
        self.assertEqual(anchor.source, "question_response")
        self.assertIn("User answer: Redis", anchor.content)

    def test_is_unavailable_without_an_interaction_callback(self) -> None:
        tools = builtin_catalog().select(
            ("ask_user",), context_for(_TMP_WORKSPACE)
        )

        self.assertEqual(tools.definitions, ())


class ToolStatelessnessTest(unittest.TestCase):
    """A shared Tool must keep nothing from one invocation to the next."""

    def test_no_builtin_tool_holds_instance_state(self) -> None:
        for tool in _catalog_tools(builtin_catalog()):
            with self.subTest(tool=tool.name):
                self.assertEqual(
                    vars(tool),
                    {},
                    f"{type(tool).__name__} stores per-instance state",
                )

    def test_one_instance_serves_two_workspaces_at_once(self) -> None:
        """A shared Tool reads its workspace from the call, not from self."""
        with (
            tempfile.TemporaryDirectory() as first,
            tempfile.TemporaryDirectory() as second,
        ):
            (Path(first) / "note.txt").write_text("first", encoding="utf-8")
            (Path(second) / "note.txt").write_text("second", encoding="utf-8")
            catalog = builtin_catalog()
            sets = {
                name: catalog.select(
                    ("read_file",), context_for(Workspace(Path(root)))
                )
                for name, root in (("first", first), ("second", second))
            }

            results = {}
            barrier = threading.Barrier(2)

            def read(name):
                barrier.wait()
                for _ in range(20):
                    results[name] = sets[name].execute(
                        ToolCall(
                            id=name,
                            name="read_file",
                            arguments={"path": "note.txt"},
                        )
                    )

            threads = [
                threading.Thread(target=read, args=(name,))
                for name in sets
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertIn("1| first", results["first"].output["content"])
            self.assertIn("1| second", results["second"].output["content"])


def _catalog_tools(catalog):
    return [catalog._tools[name] for name in catalog.names]


class UnusedExecutor:
    def execute(self, command, timeout_seconds=60):
        raise AssertionError("executor should not be called")


class FakeExa:
    def __init__(self) -> None:
        self.calls = []
        self.results = [
            SimpleNamespace(
                title="Exa API",
                url="https://docs.exa.ai/reference/search",
                published_date="2025-01-02",
                highlights=["Search the web", "Returns ranked results"],
            ),
            SimpleNamespace(
                title="Exa",
                url="https://exa.ai",
                published_date=None,
                highlights=None,
            ),
        ]

    def search(self, query, **options):
        self.calls.append({"query": query, **options})
        return SimpleNamespace(results=self.results)


class WebSearchToolTest(unittest.TestCase):
    def test_returns_ranked_results_with_highlights(self) -> None:
        client = FakeExa()
        tool = _Bound(WebSearchTool(), _TMP_WORKSPACE)

        with patch(
            "exa_py.Exa",
            return_value=client,
        ) as exa:
            result = tool.execute({"query": "exa search api"})

        exa.assert_called_once_with()
        self.assertEqual(
            client.calls,
            [
                {
                    "query": "exa search api",
                    "type": "auto",
                    "num_results": 5,
                    "include_domains": None,
                    "exclude_domains": None,
                    "contents": {
                        "highlights": {"max_characters": 1000}
                    },
                }
            ],
        )
        self.assertEqual(
            result,
            {
                "query": "exa search api",
                "results": [
                    {
                        "title": "Exa API",
                        "url": "https://docs.exa.ai/reference/search",
                        "published_date": "2025-01-02",
                        "highlights": [
                            "Search the web",
                            "Returns ranked results",
                        ],
                    },
                    {
                        "title": "Exa",
                        "url": "https://exa.ai",
                        "published_date": None,
                        "highlights": None,
                    },
                ],
            },
        )

    def test_passes_result_limit_and_domain_filters(self) -> None:
        client = FakeExa()
        tool = _Bound(WebSearchTool(), _TMP_WORKSPACE)

        with patch(
            "exa_py.Exa",
            return_value=client,
        ):
            tool.execute(
                {
                    "query": "pytest fixtures",
                    "num_results": 3,
                    "include_domains": ["docs.pytest.org"],
                    "exclude_domains": ["medium.com"],
                }
            )

        self.assertEqual(
            client.calls,
            [
                {
                    "query": "pytest fixtures",
                    "type": "auto",
                    "num_results": 3,
                    "include_domains": ["docs.pytest.org"],
                    "exclude_domains": ["medium.com"],
                    "contents": {
                        "highlights": {"max_characters": 1000}
                    },
                }
            ],
        )

    def test_missing_api_key_fails_the_call_not_the_tool(self) -> None:
        # Only the key is removed: clearing the environment would also
        # take USERPROFILE, which session paths resolve through
        # Path.home() on Windows.
        with patch.dict("os.environ"):
            os.environ.pop("EXA_API_KEY", None)
            tool = _Bound(WebSearchTool(), _TMP_WORKSPACE)

            with self.assertRaisesRegex(ValueError, "EXA_API_KEY"):
                tool.execute({"query": "exa"})

    def test_rejects_invalid_arguments(self) -> None:
        tool = _Bound(WebSearchTool(), _TMP_WORKSPACE)

        with patch(
            "exa_py.Exa",
            return_value=FakeExa(),
        ):
            with self.assertRaisesRegex(
                ValueError,
                "non-empty string 'query'",
            ):
                tool.execute({})
            with self.assertRaisesRegex(
                ValueError,
                "non-empty string 'query'",
            ):
                tool.execute({"query": ""})
            for num_results in (0, 11, True, "3"):
                with self.subTest(num_results=num_results):
                    with self.assertRaisesRegex(
                        ValueError,
                        "between 1 and 10",
                    ):
                        tool.execute(
                            {"query": "exa", "num_results": num_results}
                        )
            with self.assertRaisesRegex(ValueError, "non-empty array"):
                tool.execute({"query": "exa", "include_domains": []})
            with self.assertRaisesRegex(ValueError, "non-empty strings"):
                tool.execute({"query": "exa", "exclude_domains": [""]})
            with self.assertRaisesRegex(ValueError, "accepts only"):
                tool.execute({"query": "exa", "extra": True})


class ReadFileToolTest(unittest.TestCase):
    def test_reads_utf8_file_with_line_numbers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            (workspace.path / "notes.txt").write_text(
                "你好，Nosis。\n第二行\n",
                encoding="utf-8",
                newline="\n",
            )

            result = _Bound(ReadFileTool(), workspace).execute({"path": "notes.txt"})

            self.assertEqual(
                result,
                {
                    "path": "notes.txt",
                    "file_size_bytes": len(
                        "你好，Nosis。\n第二行\n".encode("utf-8")
                    ),
                    "content": (
                        "1| 你好，Nosis。\n"
                        "2| 第二行\n\n"
                        "(End of file — 2 lines total)"
                    ),
                },
            )

    def test_reads_requested_line_range(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            (workspace.path / "notes.txt").write_text(
                "\n".join(f"line {number}" for number in range(1, 6)),
                encoding="utf-8",
                newline="\n",
            )

            result = _Bound(ReadFileTool(), workspace).execute(
                {
                    "path": "notes.txt",
                    "offset": 2,
                    "limit": 2,
                }
            )

            self.assertEqual(
                result,
                {
                    "path": "notes.txt",
                    "file_size_bytes": len(
                        "\n".join(
                            f"line {number}" for number in range(1, 6)
                        ).encode("utf-8")
                    ),
                    "content": (
                        "2| line 2\n"
                        "3| line 3\n\n"
                        "(Showing lines 2-3. "
                        "Use offset=4 to continue.)"
                    ),
                },
            )

    def test_streams_file_without_reading_all_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            (workspace.path / "notes.txt").write_text(
                "first\nsecond\nthird\n",
                encoding="utf-8",
            )

            with patch.object(
                Path,
                "read_bytes",
                side_effect=AssertionError(
                    "read_file must not load the whole file"
                ),
            ):
                result = _Bound(ReadFileTool(), workspace).execute(
                    {
                        "path": "notes.txt",
                        "offset": 2,
                        "limit": 1,
                    }
                )

            self.assertEqual(
                result["content"],
                "2| second\n\n"
                "(Showing line 2. Use offset=3 to continue.)",
            )

    def test_stops_at_the_line_limit_when_the_budget_allows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            (workspace.path / "notes.txt").write_text(
                "\n".join(f"line {number}" for number in range(1, 2002)),
                encoding="utf-8",
            )

            result = _Bound(ReadFileTool(), workspace).execute(
                {"path": "notes.txt", "limit": 900}
            )

            content_lines = result["content"].splitlines()
            self.assertEqual(content_lines[0], "1| line 1")
            self.assertEqual(content_lines[899], "900| line 900")
            self.assertEqual(
                content_lines[-1],
                "(Showing lines 1-900. Use offset=901 to continue.)",
            )

    def test_returns_end_marker_when_offset_reaches_end(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            (workspace.path / "notes.txt").write_text(
                "first\nsecond\nthird\n",
                encoding="utf-8",
            )

            result = _Bound(ReadFileTool(), workspace).execute(
                {
                    "path": "notes.txt",
                    "offset": 3,
                    "limit": 10,
                }
            )

            self.assertEqual(
                result["content"],
                "3| third\n\n(End of file — 3 lines total)",
            )

    def test_returns_end_marker_for_offset_past_end(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            (workspace.path / "notes.txt").write_text(
                "only line\n",
                encoding="utf-8",
            )

            result = _Bound(ReadFileTool(), workspace).execute(
                {"path": "notes.txt", "offset": 2}
            )

            self.assertEqual(
                result["content"],
                "(End of file — 1 lines total)",
            )

    def test_limits_lines_and_characters_at_the_same_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            (workspace.path / "notes.txt").write_text(
                "\n".join(
                    f"line {number}: " + ("x" * 1000)
                    for number in range(1, 100)
                ),
                encoding="utf-8",
            )

            result = _Bound(ReadFileTool(), workspace).execute(
                {
                    "path": "notes.txt",
                    "offset": 1,
                    "limit": 80,
                }
            )

            self.assertLessEqual(
                len(
                    ToolResult(
                        tool_call_id="call-1",
                        name="read_file",
                        output=result,
                    ).to_content()
                ),
                MAX_TOOL_RESULT_CHARS,
            )
            self.assertIn("character limit reached", result["content"])
            self.assertIn("Use offset=", result["content"])
            numbered_lines = [
                line
                for line in result["content"].splitlines()
                if "| " in line
            ]
            self.assertLess(len(numbered_lines), 80)

    def test_defers_a_line_that_does_not_fit_to_the_next_page(self) -> None:
        """Paging must not drop the tail of a line at a page boundary.

        A line the current page cannot hold is handed to the next call
        whole, so reading a file page by page reproduces it exactly.
        """
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            lines = [f"line {number}: " + "abcdefgh" * 12 for number in range(400)]
            (workspace.path / "notes.txt").write_text(
                "\n".join(lines), encoding="utf-8"
            )
            tool = _Bound(ReadFileTool(), workspace)

            recovered: list[str] = []
            offset = 1
            for _ in range(100):
                result = tool.execute(
                    {"path": "notes.txt", "offset": offset}
                )
                body = result["content"].splitlines()
                recovered += [
                    line.split("| ", 1)[1]
                    for line in body
                    if "| " in line and line.split("| ", 1)[0].isdigit()
                ]
                status = body[-1]
                if "End of file" in status:
                    break
                offset = int(status.split("offset=")[1].split()[0])
            else:
                self.fail("paging never reached the end of the file")

            self.assertEqual(recovered, lines)

    def test_paging_advances_past_a_line_no_page_can_hold(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            (workspace.path / "notes.txt").write_text(
                "short a\n" + ("X" * 200000) + "\nshort b",
                encoding="utf-8",
            )
            tool = _Bound(ReadFileTool(), workspace)

            statuses = []
            offset = 1
            for _ in range(20):
                result = tool.execute(
                    {"path": "notes.txt", "offset": offset}
                )
                status = result["content"].splitlines()[-1]
                statuses.append(status)
                if "End of file" in status:
                    break
                following = int(status.split("offset=")[1].split()[0])
                self.assertGreater(following, offset)
                offset = following
            else:
                self.fail("paging never reached the end of the file")

            self.assertIn("End of file — 3 lines total", statuses[-1])

    def test_truncates_an_overlong_single_line_with_notice(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            (workspace.path / "notes.txt").write_text(
                "x" * (MAX_TOOL_RESULT_CHARS * 2),
                encoding="utf-8",
            )

            result = _Bound(ReadFileTool(), workspace).execute(
                {"path": "notes.txt"}
            )

            self.assertLessEqual(
                len(
                    ToolResult(
                        tool_call_id="call-1",
                        name="read_file",
                        output=result,
                    ).to_content()
                ),
                MAX_TOOL_RESULT_CHARS,
            )
            self.assertIn("该行被截断", result["content"])
            self.assertIn("character limit reached", result["content"])

    def test_rejects_file_larger_than_size_limit_and_reports_size(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            file_path = workspace.path / "large.txt"
            file_path.write_bytes(b"x" * 11)

            with patch(
                "agent_core.tools.builtin.read_file."
                "MAX_FILE_SIZE_BYTES",
                10,
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "size is 11 bytes.*maximum of 10 bytes",
                ):
                    _Bound(ReadFileTool(), workspace).execute(
                        {"path": "large.txt"}
                    )

            self.assertEqual(MAX_READ_FILE_SIZE_BYTES, 50 * 1024 * 1024)

    def test_rejects_path_outside_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))

            with self.assertRaisesRegex(
                ValueError,
                "must stay within the workspace",
            ):
                _Bound(ReadFileTool(), workspace).execute({"path": "../outside.txt"})

    @unittest.skipUnless(
        SYMLINKS_AVAILABLE,
        "creating symlinks needs Developer Mode or administrator rights "
        "on Windows",
    )
    def test_rejects_symlink_to_file_outside_workspace(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            tempfile.TemporaryDirectory() as outside_directory,
        ):
            workspace = Workspace(Path(directory))
            outside_file = Path(outside_directory) / "outside.txt"
            outside_file.write_text("secret", encoding="utf-8")
            os.symlink(outside_file, workspace.path / "link.txt")

            with self.assertRaisesRegex(
                ValueError,
                "must stay within the workspace",
            ):
                _Bound(ReadFileTool(), workspace).execute({"path": "link.txt"})

    def test_validates_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tool = _Bound(ReadFileTool(), Workspace(Path(directory)))

            with self.assertRaisesRegex(ValueError, "non-empty string"):
                tool.execute({})
            with self.assertRaisesRegex(
                ValueError,
                "'offset' to be a positive integer",
            ):
                tool.execute({"path": "notes.txt", "offset": 0})
            with self.assertRaisesRegex(
                ValueError,
                "'offset' to be a positive integer",
            ):
                tool.execute({"path": "notes.txt", "offset": True})
            with self.assertRaisesRegex(
                ValueError,
                "'limit' to be a positive integer",
            ):
                tool.execute({"path": "notes.txt", "limit": 0})
            with self.assertRaisesRegex(ValueError, "accepts only"):
                tool.execute({"path": "notes.txt", "extra": True})


class EditFileToolTest(unittest.TestCase):
    def test_replaces_one_exact_text_occurrence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            file_path = workspace.path / "notes.txt"
            file_path.write_text("hello world\n", encoding="utf-8")

            result = _Bound(EditFileTool(), workspace).execute(
                {
                    "path": "notes.txt",
                    "old_text": "world",
                    "new_text": "Nosis",
                }
            )

            self.assertEqual(
                result,
                {
                    "path": "notes.txt",
                    "replacements": 1,
                },
            )
            self.assertEqual(
                file_path.read_text(encoding="utf-8"),
                "hello Nosis\n",
            )

    def test_rejects_missing_old_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            file_path = workspace.path / "notes.txt"
            file_path.write_text("hello\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "was not found"):
                _Bound(EditFileTool(), workspace).execute(
                    {
                        "path": "notes.txt",
                        "old_text": "missing",
                        "new_text": "replacement",
                    }
                )

            self.assertEqual(file_path.read_text(encoding="utf-8"), "hello\n")

    def test_rejects_non_unique_old_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            file_path = workspace.path / "notes.txt"
            file_path.write_text("same same", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "appears 2 times"):
                _Bound(EditFileTool(), workspace).execute(
                    {
                        "path": "notes.txt",
                        "old_text": "same",
                        "new_text": "changed",
                    }
                )

            self.assertEqual(
                file_path.read_text(encoding="utf-8"),
                "same same",
            )

    def test_allows_deleting_old_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            file_path = workspace.path / "notes.txt"
            file_path.write_text("remove me", encoding="utf-8")

            _Bound(EditFileTool(), workspace).execute(
                {
                    "path": "notes.txt",
                    "old_text": "remove",
                    "new_text": "",
                }
            )

            self.assertEqual(file_path.read_text(encoding="utf-8"), " me")

    def test_requires_exact_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tool = _Bound(EditFileTool(), Workspace(Path(directory)))

            with self.assertRaisesRegex(ValueError, "non-empty string 'path'"):
                tool.execute({})
            with self.assertRaisesRegex(ValueError, "accepts only"):
                tool.execute(
                    {
                        "path": "notes.txt",
                        "old_text": "old",
                        "new_text": "new",
                        "extra": True,
                    }
                )


class ApplyPatchToolTest(unittest.TestCase):
    def test_adds_updates_and_deletes_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            (workspace.path / "notes.txt").write_text(
                "first\nold\nlast\n", encoding="utf-8"
            )
            (workspace.path / "obsolete.txt").write_text(
                "remove", encoding="utf-8"
            )

            result = _Bound(ApplyPatchTool(), workspace).execute(
                {
                    "patch": """*** Begin Patch
*** Update File: notes.txt
@@
 first
-old
+new
 last
*** Add File: added.txt
+hello
+world
*** Delete File: obsolete.txt
*** End Patch"""
                }
            )

            self.assertEqual(
                result,
                {
                    "files": [
                        {"path": "notes.txt", "operation": "updated"},
                        {"path": "added.txt", "operation": "added"},
                        {"path": "obsolete.txt", "operation": "deleted"},
                    ]
                },
            )
            self.assertEqual(
                (workspace.path / "notes.txt").read_text(encoding="utf-8"),
                "first\nnew\nlast\n",
            )
            self.assertEqual(
                (workspace.path / "added.txt").read_text(encoding="utf-8"),
                "hello\nworld\n",
            )
            self.assertFalse((workspace.path / "obsolete.txt").exists())

    def test_validates_all_changes_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            file_path = workspace.path / "notes.txt"
            file_path.write_text("old\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "requires existing file"):
                _Bound(ApplyPatchTool(), workspace).execute(
                    {
                        "patch": """*** Begin Patch
*** Update File: notes.txt
@@
-old
+new
*** Update File: missing.txt
@@
-missing
+replacement
*** End Patch"""
                    }
                )

            self.assertEqual(file_path.read_text(encoding="utf-8"), "old\n")

    def test_rejects_ambiguous_hunk_and_workspace_escape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            (workspace.path / "notes.txt").write_text(
                "same\nother\nsame\n", encoding="utf-8"
            )
            tool = _Bound(ApplyPatchTool(), workspace)

            with self.assertRaisesRegex(ValueError, "ambiguous"):
                tool.execute(
                    {
                        "patch": """*** Begin Patch
*** Update File: notes.txt
@@
-same
+changed
*** End Patch"""
                    }
                )
            with self.assertRaisesRegex(ValueError, "within the workspace"):
                tool.execute(
                    {
                        "patch": """*** Begin Patch
*** Add File: ../outside.txt
+nope
*** End Patch"""
                    }
                )

    def test_validates_arguments_and_patch_markers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tool = _Bound(ApplyPatchTool(), Workspace(Path(directory)))

            with self.assertRaisesRegex(ValueError, "non-empty string"):
                tool.execute({"patch": ""})
            with self.assertRaisesRegex(ValueError, "accepts only"):
                tool.execute({"patch": "x", "extra": True})
            with self.assertRaisesRegex(ValueError, "must start"):
                tool.execute({"patch": "*** End Patch"})

    def test_preserves_crlf_and_non_newline_unicode_separators(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            file_path = workspace.path / "notes.txt"
            original = (
                "first\r\nold\r\nvertical\vform\ffoo\x85bar\u2028baz\r\n"
                "last\r\n"
            ).encode("utf-8")
            file_path.write_bytes(original)

            _Bound(ApplyPatchTool(), workspace).execute(
                {
                    "patch": """*** Begin Patch
*** Update File: notes.txt
@@ -2,1 +2,1 @@
-old
+new
*** End Patch"""
                }
            )

            self.assertEqual(
                file_path.read_bytes(),
                original.replace(b"old", b"new", 1),
            )

    def test_validates_numbered_hunk_header(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            file_path = workspace.path / "notes.txt"
            file_path.write_text("one\ntwo\n", encoding="utf-8")
            tool = _Bound(ApplyPatchTool(), workspace)

            with self.assertRaisesRegex(ValueError, "old line count"):
                tool.execute(
                    {
                        "patch": """*** Begin Patch
*** Update File: notes.txt
@@ -1,2 +1,1 @@
-one
+changed
*** End Patch"""
                    }
                )
            with self.assertRaisesRegex(ValueError, "invalid hunk header"):
                tool.execute(
                    {
                        "patch": """*** Begin Patch
*** Update File: notes.txt
@@ ignored text
-one
+changed
*** End Patch"""
                    }
                )
            self.assertEqual(file_path.read_text(encoding="utf-8"), "one\ntwo\n")

    def test_allows_blank_lines_between_delete_and_end_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            file_path = workspace.path / "notes.txt"
            file_path.write_text("delete me", encoding="utf-8")

            _Bound(ApplyPatchTool(), workspace).execute(
                {
                    "patch": """*** Begin Patch
*** Delete File: notes.txt

*** End Patch
"""
                }
            )

            self.assertFalse(file_path.exists())


class ListDirectoryToolTest(unittest.TestCase):
    def test_lists_immediate_entries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            (workspace.path / "z.txt").write_text("z", encoding="utf-8")
            (workspace.path / "a").mkdir()

            result = _Bound(ListDirectoryTool(), workspace).execute({"path": "."})

            self.assertEqual(result["path"], ".")
            self.assertCountEqual(
                result["entries"],
                [
                    {"name": "a", "type": "directory"},
                    {"name": "z.txt", "type": "file"},
                ],
            )
            self.assertFalse(result["has_more"])
            self.assertIsNone(result["next_offset"])

    @unittest.skipUnless(
        SYMLINKS_AVAILABLE,
        "creating symlinks needs Developer Mode or administrator rights "
        "on Windows",
    )
    def test_reports_symlink_entries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            (workspace.path / "z.txt").write_text("z", encoding="utf-8")
            os.symlink(
                workspace.path / "z.txt",
                workspace.path / "link.txt",
            )

            result = _Bound(ListDirectoryTool(), workspace).execute({"path": "."})

            self.assertCountEqual(
                result["entries"],
                [
                    {"name": "link.txt", "type": "symlink"},
                    {"name": "z.txt", "type": "file"},
                ],
            )

    def test_filters_recurses_and_paginates_without_consuming_all_entries(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            nested = workspace.path / "nested"
            nested.mkdir()
            paths = []
            for name in ("a.py", "b.txt", "c.py"):
                path = workspace.path / name
                path.write_text(name, encoding="utf-8")
                paths.append(path)
            child = nested / "child.py"
            child.write_text("child", encoding="utf-8")
            paths.append(child)
            yielded = []

            def entries(*_args):
                for path in paths:
                    yielded.append(path)
                    yield path

            with patch(
                "agent_core.tools.builtin.list_directory._iter_entries",
                side_effect=entries,
            ):
                result = _Bound(ListDirectoryTool(), workspace).execute(
                    {
                        "path": ".",
                        "glob": "**/*.py",
                        "recursive": True,
                        "offset": 1,
                        "limit": 1,
                    }
                )

            self.assertEqual(
                result,
                {
                    "path": ".",
                    "entries": [{"name": "c.py", "type": "file"}],
                    "has_more": True,
                    "next_offset": 2,
                },
            )
            self.assertEqual(yielded, paths)

    def test_glob_matches_complete_relative_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            nested = workspace.path / "nested"
            nested.mkdir()
            (workspace.path / "root.py").write_text("root", encoding="utf-8")
            (nested / "child.py").write_text("child", encoding="utf-8")
            tool = _Bound(ListDirectoryTool(), workspace)

            shallow = tool.execute(
                {"path": ".", "glob": "*.py", "recursive": True}
            )
            recursive = tool.execute(
                {"path": ".", "glob": "**/*.py", "recursive": True}
            )

            self.assertEqual(
                [entry["name"] for entry in shallow["entries"]],
                ["root.py"],
            )
            self.assertCountEqual(
                [entry["name"] for entry in recursive["entries"]],
                ["nested/child.py", "root.py"],
            )

    def test_stops_after_one_lookahead_match(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            paths = []
            for name in ("a.txt", "b.txt", "c.txt", "d.txt"):
                path = workspace.path / name
                path.write_text(name, encoding="utf-8")
                paths.append(path)
            yielded = []

            def entries(*_args):
                for path in paths:
                    yielded.append(path)
                    yield path

            with patch(
                "agent_core.tools.builtin.list_directory._iter_entries",
                side_effect=entries,
            ):
                result = _Bound(ListDirectoryTool(), workspace).execute(
                    {"path": ".", "limit": 2}
                )

            self.assertEqual(len(result["entries"]), 2)
            self.assertTrue(result["has_more"])
            self.assertEqual(result["next_offset"], 2)
            self.assertEqual(yielded, paths[:3])

    def test_large_offset_streams_without_retaining_skipped_entries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            file_path = workspace.path / "entry.txt"
            file_path.write_text("entry", encoding="utf-8")
            yielded = 0

            def entries(*_args):
                nonlocal yielded
                for _ in range(10_003):
                    yielded += 1
                    yield file_path

            with patch(
                "agent_core.tools.builtin.list_directory._iter_entries",
                side_effect=entries,
            ):
                result = _Bound(ListDirectoryTool(), workspace).execute(
                    {"path": ".", "offset": 10_001, "limit": 1}
                )

            self.assertEqual(
                result["entries"],
                [{"name": "entry.txt", "type": "file"}],
            )
            self.assertTrue(result["has_more"])
            self.assertEqual(result["next_offset"], 10_002)
            self.assertEqual(yielded, 10_003)

    def test_rejects_file_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            (workspace.path / "notes.txt").write_text(
                "notes",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "must be a directory"):
                _Bound(ListDirectoryTool(), workspace).execute({"path": "notes.txt"})

    def test_rejects_path_outside_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tool = _Bound(ListDirectoryTool(), Workspace(Path(directory)))

            with self.assertRaisesRegex(
                ValueError,
                "must stay within the workspace",
            ):
                tool.execute({"path": ".."})


class SearchFilesToolTest(unittest.TestCase):
    def test_searches_utf8_files_recursively(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            nested = workspace.path / "nested"
            nested.mkdir()
            (workspace.path / "root.txt").write_text(
                "first Nosis\nsecond\n",
                encoding="utf-8",
            )
            (nested / "child.txt").write_text(
                "Nosis child\nnosis lowercase\n",
                encoding="utf-8",
            )

            result = _Bound(SearchFilesTool(), workspace).execute(
                {"path": ".", "pattern": r"Nosis"}
            )

            self.assertEqual(
                result,
                {
                    "matches": [
                        {
                            "path": "nested/child.txt",
                            "line_number": 1,
                            "line": "Nosis child",
                        },
                        {
                            "path": "root.txt",
                            "line_number": 1,
                            "line": "first Nosis",
                        },
                    ],
                    "has_more": False,
                    "next_offset": None,
                    "scanned_files": 2,
                    "skipped_files": 0,
                },
            )

    def test_supports_regular_expressions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            (workspace.path / "notes.txt").write_text(
                "item-12\nitem-x\n",
                encoding="utf-8",
            )

            result = _Bound(SearchFilesTool(), workspace).execute(
                {"path": ".", "pattern": r"item-\d+"}
            )

            self.assertEqual(len(result["matches"]), 1)
            self.assertEqual(result["matches"][0]["line"], "item-12")

    def test_supports_glob_case_insensitive_and_fixed_strings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            nested = workspace.path / "nested"
            nested.mkdir()
            (workspace.path / "root.py").write_text(
                "value = 'NOSIS.'\n",
                encoding="utf-8",
            )
            (nested / "child.py").write_text(
                "value = 'nosis.'\n",
                encoding="utf-8",
            )
            (nested / "child.txt").write_text(
                "value = 'nosis.'\n",
                encoding="utf-8",
            )

            result = _Bound(SearchFilesTool(), workspace).execute(
                {
                    "path": ".",
                    "pattern": "nosis.",
                    "glob": "**/*.py",
                    "case_insensitive": True,
                    "fixed_strings": True,
                }
            )

            self.assertEqual(
                [match["path"] for match in result["matches"]],
                ["nested/child.py", "root.py"],
            )
            self.assertEqual(result["scanned_files"], 2)

    def test_paginates_matches_with_offset_and_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            (workspace.path / "notes.txt").write_text(
                "\n".join(f"Nosis {index}" for index in range(5)),
                encoding="utf-8",
            )
            tool = _Bound(SearchFilesTool(), workspace)

            first_page = tool.execute(
                {
                    "path": ".",
                    "pattern": "Nosis",
                    "offset": 0,
                    "limit": 2,
                }
            )
            second_page = tool.execute(
                {
                    "path": ".",
                    "pattern": "Nosis",
                    "offset": first_page["next_offset"],
                    "limit": 2,
                }
            )

            self.assertEqual(
                [match["line_number"] for match in first_page["matches"]],
                [1, 2],
            )
            self.assertTrue(first_page["has_more"])
            self.assertEqual(first_page["next_offset"], 2)
            self.assertEqual(
                [match["line_number"] for match in second_page["matches"]],
                [3, 4],
            )
            self.assertTrue(second_page["has_more"])
            self.assertEqual(second_page["next_offset"], 4)

    def test_defaults_to_at_most_200_matches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            (workspace.path / "notes.txt").write_text(
                "\n".join("Nosis" for _ in range(201)),
                encoding="utf-8",
            )

            result = _Bound(SearchFilesTool(), workspace).execute(
                {"path": ".", "pattern": "Nosis"}
            )

            self.assertEqual(len(result["matches"]), 200)
            self.assertTrue(result["has_more"])
            self.assertEqual(result["next_offset"], 200)

    def test_skips_common_dependency_and_build_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            for name in ("node_modules", ".venv", "build"):
                excluded = workspace.path / name
                excluded.mkdir()
                (excluded / "match.txt").write_text(
                    "Nosis",
                    encoding="utf-8",
                )
            (workspace.path / "match.txt").write_text(
                "Nosis",
                encoding="utf-8",
            )

            result = _Bound(SearchFilesTool(), workspace).execute(
                {"path": ".", "pattern": "Nosis"}
            )

            self.assertEqual(
                [match["path"] for match in result["matches"]],
                ["match.txt"],
            )
            self.assertEqual(result["scanned_files"], 1)

    def test_skips_files_larger_than_scan_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            (workspace.path / "large.txt").write_text(
                "Nosis",
                encoding="utf-8",
            )

            with patch(
                "agent_core.tools.builtin.search_files."
                "MAX_FILE_SIZE_BYTES",
                3,
            ):
                result = _Bound(SearchFilesTool(), workspace).execute(
                    {"path": ".", "pattern": "Nosis"}
                )

            self.assertEqual(result["matches"], [])
            self.assertEqual(result["scanned_files"], 0)
            self.assertEqual(result["skipped_files"], 1)

    def test_scans_past_the_previous_path_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            no_match = workspace.path / "no-match.txt"
            no_match.write_text("nothing", encoding="utf-8")
            match = workspace.path / "match.txt"
            match.write_text("Nosis", encoding="utf-8")

            with patch(
                "agent_core.tools.builtin.search_files._iter_files",
                return_value=iter([no_match] * 10_000 + [match]),
            ):
                result = _Bound(SearchFilesTool(), workspace).execute(
                    {"path": ".", "pattern": "Nosis"}
                )

            self.assertEqual(
                [item["path"] for item in result["matches"]],
                ["match.txt"],
            )
            self.assertFalse(result["has_more"])
            self.assertEqual(result["scanned_files"], 10_001)

    def test_returns_context_around_content_matches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            (workspace.path / "notes.txt").write_text(
                "one\ntwo\nNosis\nfour\nfive\n",
                encoding="utf-8",
            )

            result = _Bound(SearchFilesTool(), workspace).execute(
                {
                    "path": ".",
                    "pattern": "Nosis",
                    "context_before": 2,
                    "context_after": 2,
                }
            )

            self.assertEqual(
                result["matches"],
                [
                    {
                        "path": "notes.txt",
                        "line_number": 3,
                        "line": "Nosis",
                        "context_before": [
                            {"line_number": 1, "line": "one"},
                            {"line_number": 2, "line": "two"},
                        ],
                        "context_after": [
                            {"line_number": 4, "line": "four"},
                            {"line_number": 5, "line": "five"},
                        ],
                    }
                ],
            )

    def test_searches_file_paths_in_files_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            nested = workspace.path / "nested"
            nested.mkdir()
            (nested / "agent.py").write_text("binary-like: \ufffd", encoding="utf-8")
            (nested / "notes.txt").write_text("agent.py", encoding="utf-8")

            result = _Bound(SearchFilesTool(), workspace).execute(
                {
                    "path": ".",
                    "pattern": r"agent\.py$",
                    "mode": "files",
                }
            )

            self.assertEqual(result["matches"], [{"path": "nested/agent.py"}])
            self.assertEqual(result["scanned_files"], 2)

    def test_search_glob_matches_complete_relative_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            nested = workspace.path / "nested"
            nested.mkdir()
            (workspace.path / "root.py").write_text("Nosis", encoding="utf-8")
            (nested / "child.py").write_text("Nosis", encoding="utf-8")
            tool = _Bound(SearchFilesTool(), workspace)

            shallow = tool.execute(
                {"path": ".", "pattern": "Nosis", "glob": "*.py"}
            )
            recursive = tool.execute(
                {"path": ".", "pattern": "Nosis", "glob": "**/*.py"}
            )

            self.assertEqual(
                [match["path"] for match in shallow["matches"]],
                ["root.py"],
            )
            self.assertEqual(
                [match["path"] for match in recursive["matches"]],
                ["nested/child.py", "root.py"],
            )

    def test_limits_serialized_tool_result_size(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            (workspace.path / "notes.txt").write_text(
                "Nosis " + ("x" * (MAX_TOOL_RESULT_CHARS * 2)),
                encoding="utf-8",
            )

            result = _Bound(SearchFilesTool(), workspace).execute(
                {"path": ".", "pattern": "Nosis"}
            )

            content = ToolResult(
                tool_call_id="call-1",
                name="search_files",
                output=result,
            ).to_content()
            self.assertLessEqual(len(content), MAX_TOOL_RESULT_CHARS)
            self.assertTrue(result["matches"][0]["line_truncated"])

    def test_skips_non_utf8_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            (workspace.path / "binary.bin").write_bytes(b"\xffNosis")

            result = _Bound(SearchFilesTool(), workspace).execute(
                {"path": ".", "pattern": "Nosis"}
            )

            self.assertEqual(result["matches"], [])
            self.assertEqual(result["skipped_files"], 1)

    @unittest.skipUnless(
        SYMLINKS_AVAILABLE,
        "creating symlinks needs Developer Mode or administrator rights "
        "on Windows",
    )
    def test_skips_symlinks_to_files_outside_workspace(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            tempfile.TemporaryDirectory() as outside_directory,
        ):
            workspace = Workspace(Path(directory))
            outside_file = Path(outside_directory) / "outside.txt"
            outside_file.write_text("Nosis", encoding="utf-8")
            os.symlink(outside_file, workspace.path / "link.txt")

            result = _Bound(SearchFilesTool(), workspace).execute(
                {"path": ".", "pattern": "Nosis"}
            )

            self.assertEqual(result["matches"], [])
            self.assertEqual(result["skipped_files"], 1)

    @unittest.skipUnless(
        os.name == "nt",
        "directory junctions are a Windows feature",
    )
    def test_skips_junctions_to_directories_outside_workspace(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            tempfile.TemporaryDirectory() as outside_directory,
        ):
            workspace = Workspace(Path(directory))
            outside_file = Path(outside_directory) / "outside.txt"
            outside_file.write_text("Nosis", encoding="utf-8")
            subprocess.run(
                [
                    "cmd",
                    "/c",
                    "mklink",
                    "/J",
                    str(workspace.path / "link"),
                    outside_directory,
                ],
                check=True,
                capture_output=True,
            )

            result = _Bound(SearchFilesTool(), workspace).execute(
                {"path": ".", "pattern": "Nosis"}
            )

            self.assertEqual(result["matches"], [])
            self.assertEqual(result["skipped_files"], 1)

    def test_rejects_path_outside_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tool = _Bound(SearchFilesTool(), Workspace(Path(directory)))

            with self.assertRaisesRegex(
                ValueError,
                "must stay within the workspace",
            ):
                tool.execute({"path": "..", "pattern": "Nosis"})

    def test_rejects_file_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            (workspace.path / "notes.txt").write_text(
                "Nosis",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "must be a directory"):
                _Bound(SearchFilesTool(), workspace).execute(
                    {"path": "notes.txt", "pattern": "Nosis"}
                )

    def test_rejects_invalid_regular_expression(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tool = _Bound(SearchFilesTool(), Workspace(Path(directory)))

            with self.assertRaisesRegex(Exception, "unterminated"):
                tool.execute({"path": ".", "pattern": "["})

    def test_validates_optional_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tool = _Bound(SearchFilesTool(), Workspace(Path(directory)))

            invalid_arguments = (
                {"path": ".", "pattern": "x", "glob": ""},
                {"path": ".", "pattern": "x", "offset": -1},
                {"path": ".", "pattern": "x", "limit": 201},
                {
                    "path": ".",
                    "pattern": "x",
                    "case_insensitive": 1,
                },
                {"path": ".", "pattern": "x", "fixed_strings": 1},
                {"path": ".", "pattern": "x", "unknown": True},
            )

            for arguments in invalid_arguments:
                with self.subTest(arguments=arguments):
                    with self.assertRaises(ValueError):
                        tool.execute(arguments)


class ShellToolTest(unittest.TestCase):
    def test_delegates_command_to_executor(self) -> None:
        class RecordingExecutor:
            def __init__(self) -> None:
                self.commands = []

            def execute(self, command, timeout_seconds=60):
                self.commands.append((command, timeout_seconds))
                return CommandExecutionResult(
                    command=command,
                    exit_code=7,
                    stdout="output",
                    stderr="warning",
                    timeout_seconds=timeout_seconds,
                )

        executor = RecordingExecutor()
        tool = _Bound(
            ShellTool(),
            _TMP_WORKSPACE,
            execution_router=ExecutionRouter(executor, UnusedExecutor()),
        )

        result = tool.execute({"command": "example command"})

        self.assertEqual(executor.commands, [("example command", 60)])
        self.assertEqual(
            result,
            {
                "command": "example command",
                "exit_code": 7,
                "stdout": "output",
                "stderr": "warning",
                "timed_out": False,
                "timeout_seconds": 60,
            },
        )

    def test_routes_each_call_by_execution_scope(self) -> None:
        class RecordingExecutor:
            def __init__(self, name):
                self.name = name
                self.commands = []

            def execute(self, command, timeout_seconds=60):
                self.commands.append(command)
                return CommandExecutionResult(
                    command=command,
                    exit_code=0,
                    stdout=self.name,
                    stderr="",
                    timeout_seconds=timeout_seconds,
                )

        workspace_executor = RecordingExecutor("workspace")
        host_executor = RecordingExecutor("host")
        tool = _Bound(
            ShellTool(),
            _TMP_WORKSPACE,
            execution_router=ExecutionRouter(
                workspace_executor, host_executor
            ),
        )

        workspace_result = tool.execute({"command": "pwd"})
        host_result = tool.execute({"command": "pwd", "scope": "host"})

        self.assertEqual(workspace_result["stdout"], "workspace")
        self.assertEqual(host_result["stdout"], "host")
        self.assertEqual(workspace_executor.commands, ["pwd"])
        self.assertEqual(host_executor.commands, ["pwd"])

    def test_full_access_defaults_to_host_and_allows_workspace_downgrade(self) -> None:
        class RecordingExecutor:
            def __init__(self, name):
                self.name = name
                self.commands = []

            def execute(self, command, timeout_seconds=60):
                self.commands.append(command)
                return CommandExecutionResult(command, 0, self.name, "")

        workspace_executor = RecordingExecutor("workspace")
        host_executor = RecordingExecutor("host")
        context = context_for(_TMP_WORKSPACE)
        context = replace(
            context,
            execution_router=ExecutionRouter(
                workspace_executor,
                host_executor,
                authority=FULL_ACCESS_AUTHORITY,
            ),
        )
        tools = ToolCatalog((ShellTool(),)).select(("shell",), context)

        default_result = tools.execute(
            ToolCall("default", "shell", {"command": "pwd"})
        )
        downgraded_result = tools.execute(
            ToolCall(
                "workspace",
                "shell",
                {"command": "pwd", "scope": "workspace"},
            )
        )

        self.assertEqual(default_result.output["stdout"], "host")
        self.assertEqual(downgraded_result.output["stdout"], "workspace")
        self.assertEqual(host_executor.commands, ["pwd"])
        self.assertEqual(workspace_executor.commands, ["pwd"])

    def test_allows_explicit_timeout_below_default(self) -> None:
        class RecordingExecutor:
            def __init__(self) -> None:
                self.timeouts = []

            def execute(self, command, timeout_seconds=60):
                self.timeouts.append(timeout_seconds)
                return CommandExecutionResult(
                    command=command,
                    exit_code=0,
                    stdout="",
                    stderr="",
                    timeout_seconds=timeout_seconds,
                )

        executor = RecordingExecutor()
        tool = _Bound(
            ShellTool(),
            _TMP_WORKSPACE,
            execution_router=ExecutionRouter(executor, UnusedExecutor()),
        )

        tool.execute({"command": "pwd", "timeout_seconds": 10})

        self.assertEqual(executor.timeouts, [10])

    def test_allows_timeout_above_the_default(self) -> None:
        class RecordingExecutor:
            def __init__(self) -> None:
                self.timeouts = []

            def execute(self, command, timeout_seconds=60):
                self.timeouts.append(timeout_seconds)
                return CommandExecutionResult(
                    command=command,
                    exit_code=0,
                    stdout="",
                    stderr="",
                    timeout_seconds=timeout_seconds,
                )

        executor = RecordingExecutor()
        tool = _Bound(
            ShellTool(),
            _TMP_WORKSPACE,
            execution_router=ExecutionRouter(executor, UnusedExecutor()),
        )

        tool.execute({"command": "pwd", "timeout_seconds": 61})
        tool.execute({"command": "pwd", "timeout_seconds": 900})

        self.assertEqual(executor.timeouts, [61, 900])

    def test_advertises_the_shell_and_timeout_it_uses(self) -> None:
        class UnusedExecutor:
            def execute(self, command, timeout_seconds=60):
                raise AssertionError("executor should not be called")

        tool = _Bound(
            ShellTool(),
            _TMP_WORKSPACE,
            execution_router=ExecutionRouter(
                UnusedExecutor(), UnusedExecutor()
            ),
        )

        definition = tool.definition
        timeout_schema = definition.parameters["properties"][
            "timeout_seconds"
        ]

        self.assertEqual(timeout_schema["maximum"], 900)
        self.assertIn("60 seconds by default", definition.description)
        self.assertIn("maximum of 900", timeout_schema["description"])
        self.assertIn("up to 900 seconds", definition.description)
        self.assertNotIn("background", definition.description)
        self.assertNotIn("86400", definition.description)
        self.assertNotIn("background", definition.parameters["properties"])
        serialized_schema = json.dumps(definition.parameters)
        self.assertNotIn("background", serialized_schema)
        self.assertNotIn("86400", serialized_schema)
        self.assertIn(
            "Git Bash" if os.name == "nt" else "/bin/sh",
            definition.description,
        )

    def test_background_shell_advertises_the_longer_timeout(self) -> None:
        from agent_core import JobManager

        class UnusedExecutor:
            def execute(self, command, timeout_seconds=60, cancellation=None):
                raise AssertionError("executor should not be called")

        session = Session("s")
        jobs = JobManager(session)
        context = context_for(
            _TMP_WORKSPACE,
            session=session,
            execution_router=ExecutionRouter(
                UnusedExecutor(), UnusedExecutor()
            ),
            jobs=jobs,
        )

        timeout_schema = ShellTool().definition(context).parameters[
            "properties"
        ]["timeout_seconds"]

        self.assertEqual(timeout_schema["maximum"], 86400)
        definition = ShellTool().definition(context)
        self.assertIn("background", definition.parameters["properties"])
        self.assertIn("background=true", definition.description)
        self.assertIn("86400", definition.description)
        self.assertIn("background=true", timeout_schema["description"])
        self.assertIn("86400", timeout_schema["description"])
        jobs.close()

    def test_validates_arguments(self) -> None:
        class UnusedExecutor:
            def execute(self, command, timeout_seconds=60):
                raise AssertionError("executor should not be called")

        tool = _Bound(
            ShellTool(),
            _TMP_WORKSPACE,
            execution_router=ExecutionRouter(
                UnusedExecutor(), UnusedExecutor()
            ),
        )

        with self.assertRaisesRegex(ValueError, "non-empty string"):
            tool.execute({})
        with self.assertRaisesRegex(ValueError, "accepts only"):
            tool.execute({"command": "pwd", "extra": True})
        with self.assertRaisesRegex(ValueError, "execution scope"):
            tool.execute({"command": "pwd", "scope": "container"})
        with self.assertRaisesRegex(ValueError, "'background'.*boolean"):
            tool.execute(
                {
                    "command": "pwd",
                    "background": "true",
                    "timeout_seconds": 86401,
                }
            )
        for timeout_seconds in (0, 901, True, "10"):
            with self.subTest(timeout_seconds=timeout_seconds):
                with self.assertRaisesRegex(ValueError, "between 1 and 900"):
                    tool.execute(
                        {
                            "command": "pwd",
                            "timeout_seconds": timeout_seconds,
                        }
                    )

if __name__ == "__main__":
    unittest.main()
