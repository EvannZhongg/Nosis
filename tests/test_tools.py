import json
import os
import subprocess
import threading
import unittest
import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.filesystem_fixtures import symlinks_available

import httpx

from agent_core import (
    ApplyPatchTool,
    AskUserTool,
    CommandExecutionResult,
    EditFileTool,
    ExecutionRouter,
    ExecutionScope,
    FilesystemAccess,
    FULL_ACCESS_AUTHORITY,
    JsonlSessionStore,
    ListDirectoryTool,
    NetworkAccess,
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
    WriteFileTool,
    builtin_catalog,
)
from agent_core.tools.config import ToolSettings, WebSearchConfig
from agent_core.permissions import PermissionPreset
from agent_core.turn_control import AgentCancelled, TurnControl
from agent_core.scheduler import CronTrigger, IntervalTrigger, SchedulerService
from agent_core.tools.builtin.schedule import ScheduledTaskTool
from agent_core.tools.budget import MAX_TOOL_RESULT_CHARS
from agent_core.tools.builtin.read_file import (
    MAX_FILE_SIZE_BYTES as MAX_READ_FILE_SIZE_BYTES,
)


SYMLINKS_AVAILABLE = symlinks_available()

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
    call = ToolCall("test-call", "scheduled_task", arguments)
    return ScheduledTaskTool().execute(
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
    def test_catalog_requires_scheduler_and_execution_router(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context = scheduled_context_for(
                Workspace(root), scheduler=SchedulerService(root / "schedule.jsonl")
            )
            catalog = builtin_catalog()
            self.assertEqual(
                [name for name in catalog.names if "scheduled" in name],
                ["scheduled_task"],
            )
            self.assertEqual(
                len(catalog.select(("scheduled_task",), context).definitions), 1
            )
            for unavailable in (
                replace(context, scheduler=None),
                replace(context, execution_router=None),
            ):
                self.assertEqual(
                    catalog.select(("scheduled_task",), unavailable).definitions, ()
                )

    def test_unified_tool_exposes_all_actions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scheduler = SchedulerService(root / "schedule.jsonl")
            context = scheduled_context_for(
                Workspace(root),
                sessions_directory=root / "sessions",
                scheduler=scheduler,
            )
            tool = ScheduledTaskTool()
            created = execute_scheduled(
                {
                    "action": "create",
                    "schedule": {
                        "prompt": "p",
                        "trigger": {"type": "interval", "seconds": 60},
                    },
                },
                context,
            )
            schedule_id = created["schedule_id"]
            updated = tool.execute(
                {
                    "action": "update",
                    "schedule_id": schedule_id,
                    "changes": {"enabled": False},
                },
                context,
            )
            self.assertFalse(updated["enabled"])
            listed = tool.execute({"action": "list"}, context)
            self.assertEqual(listed[0]["schedule_id"], schedule_id)
            deleted = tool.execute(
                {"action": "delete", "schedule_id": schedule_id}, context
            )
            self.assertEqual(deleted, {"schedule_id": schedule_id, "deleted": True})
            self.assertEqual(scheduler.schedules, [])

    def test_action_rejects_fields_belonging_to_another_action(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context = scheduled_context_for(
                Workspace(root),
                sessions_directory=root / "sessions",
                scheduler=SchedulerService(root / "schedule.jsonl"),
            )
            tool = ScheduledTaskTool()
            invalid = (
                {},
                {"action": "unknown"},
                {"action": None},
                {"action": []},
                {"action": "create"},
                {"action": "create", "schedule": None},
                {"action": "list", "schedule_id": "x"},
                {"action": "delete"},
                {"action": "update", "schedule_id": "x"},
                {"action": "update", "schedule_id": "x", "changes": {}},
                {"action": "update", "schedule_id": "x", "changes": None},
                {"action": "update", "schedule_id": "x", "changes": []},
                {"action": "create", "schedule": {"prompt": "p"}},
                {
                    "action": "create",
                    "schedule": {
                        "prompt": "p",
                        "trigger": {"type": "interval", "seconds": 1},
                        "enabled": True,
                    },
                },
                {
                    "action": "delete",
                    "schedule_id": "x",
                    "changes": {"enabled": False},
                },
            )
            for arguments in invalid:
                with self.subTest(arguments=arguments):
                    with self.assertRaises(ValueError):
                        tool.execute(arguments, context)
                    self.assertEqual(list(root.iterdir()), [])

    def test_create_schema_declares_trigger_properties_without_one_of(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            definition = ScheduledTaskTool().definition(
                scheduled_context_for(Workspace(Path(directory)))
            )

        properties = definition.parameters["properties"]
        self.assertEqual(definition.parameters["required"], ["action"])
        self.assertEqual(
            properties["action"]["enum"], ["create", "update", "list", "delete"]
        )
        self.assertEqual(properties["changes"]["minProperties"], 1)
        trigger_schema = properties["schedule"]["properties"]["trigger"]
        self.assertEqual(properties["changes"]["properties"]["trigger"], trigger_schema)
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
            definition.parameters["properties"]["schedule"]["properties"]["execution_scope"]["enum"],
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
            tool = ScheduledTaskTool()

            self.assertEqual(
                tool.definition(context).parameters["properties"]["schedule"]
                ["properties"]["execution_scope"]["enum"],
                ["workspace", "host"],
            )
            result = execute_scheduled(
                {
                    "action": "create",
                    "schedule": {
                        "prompt": "host scheduled prompt",
                        "trigger": {
                            "type": "once",
                            "at": "2099-01-01T00:00:00+00:00",
                        },
                        "execution_scope": "host",
                    },
                },
                context,
            )

            self.assertEqual(result["execution_scope"], "host")
            self.assertIs(
                scheduler.schedules[0].execution_scope,
                ExecutionScope.HOST,
            )
            self.assertEqual(
                store.load(result["schedule_session_id"]).permission_preset,
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
                        "action": "create",
                        "schedule": {
                            "prompt": "host scheduled prompt",
                            "trigger": {
                                "type": "once",
                                "at": "2099-01-01T00:00:00+00:00",
                            },
                            "execution_scope": "host",
                        },
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
                ScheduledTaskTool().definition(context).parameters
                ["properties"]["schedule"]["properties"]["execution_scope"]["enum"],
                ["workspace"],
            )
            with self.assertRaisesRegex(
                PermissionError,
                "host scheduled tasks require Full Access",
            ):
                execute_scheduled(
                    {
                        "action": "create",
                        "schedule": {
                            "prompt": "host scheduled prompt",
                            "trigger": {
                                "type": "once",
                                "at": "2099-01-01T00:00:00+00:00",
                            },
                            "execution_scope": "host",
                        },
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
                    "action": "create",
                    "schedule": {
                        "prompt": "scheduled prompt",
                        "trigger": {"type": "once", "at": "2099-01-01T00:00:00+00:00"},
                    },
                },
                context,
            )
            toolset = builtin_catalog().select(("scheduled_task",), context)
            result = toolset.execute(
                ToolCall(
                    id="delete-1",
                    name="scheduled_task",
                    arguments={"action": "delete", "schedule_id": created["schedule_id"]},
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
                    "action": "create",
                    "schedule": {
                        "prompt": "scheduled prompt",
                        "trigger": {"type": "interval", "seconds": 90},
                    },
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
                    "action": "create",
                    "schedule": {
                        "prompt": "scheduled prompt",
                        "trigger": {"type": "interval", "seconds": 1200},
                    },
                },
                context,
            )

            result = ScheduledTaskTool().execute(
                {
                    "action": "update",
                    "schedule_id": created["schedule_id"],
                    "changes": {
                        "trigger": {
                            "type": "cron",
                            "expression": "0 10 * * *",
                            "timezone": "Asia/Shanghai",
                        },
                        "end_at": "2099-12-31T23:59:00+08:00",
                    },
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

    def test_schedule_tool_rejects_coercion_and_unknown_arguments(self) -> None:
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
                    "action": "create",
                    "schedule": {
                        "prompt": "scheduled prompt",
                        "trigger": {"type": "interval", "seconds": 60},
                    },
                },
                context,
            )

            with self.assertRaisesRegex(ValueError, "prompt must be"):
                ScheduledTaskTool().execute(
                    {
                        "action": "update",
                        "schedule_id": created["schedule_id"],
                        "changes": {"prompt": " "},
                    },
                    context,
                )
            with self.assertRaisesRegex(ValueError, "enabled must be"):
                ScheduledTaskTool().execute(
                    {
                        "action": "update",
                        "schedule_id": created["schedule_id"],
                        "changes": {"enabled": 1},
                    },
                    context,
                )
            with self.assertRaisesRegex(
                ValueError, "unsupported schedule update field"
            ):
                ScheduledTaskTool().execute(
                    {
                        "action": "update",
                        "schedule_id": created["schedule_id"],
                        "changes": {"unknown": True},
                    },
                    context,
                )
            with self.assertRaisesRegex(ValueError, "schedule_id must be"):
                ScheduledTaskTool().execute(
                    {
                        "action": "update",
                        "schedule_id": 123,
                        "changes": {"prompt": "updated"},
                    },
                    context,
                )
            with self.assertRaisesRegex(ValueError, "schedule_id must be"):
                ScheduledTaskTool().execute({"action": "delete", "schedule_id": 123}, context)

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
                    "action": "create",
                    "schedule": {
                        "prompt": "scheduled prompt",
                        "trigger": {"type": "interval", "seconds": 1200},
                        "end_at": "2099-12-31T23:59:00+00:00",
                    },
                },
                context,
            )

            item = ScheduledTaskTool().execute({"action": "list"}, context)[0]

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
            with self.assertRaisesRegex(ValueError, "once trigger requires 'at'"):
                execute_scheduled(
                    {"action": "create", "schedule": {"prompt": "p", "trigger": {"type": "once"}}},
                    context,
                )
            with self.assertRaisesRegex(ValueError, "cron trigger requires 'expression'"):
                execute_scheduled(
                    {"action": "create", "schedule": {"prompt": "p", "trigger": {"type": "cron"}}},
                    context,
                )
            with self.assertRaisesRegex(ValueError, "Retry with seconds"):
                execute_scheduled(
                    {"action": "create", "schedule": {"prompt": "p", "trigger": {"type": "interval"}}},
                    context,
                )
            with self.assertRaisesRegex(ValueError, "start_at must be"):
                execute_scheduled(
                    {
                        "action": "create",
                        "schedule": {
                            "prompt": "p",
                            "trigger": {
                                "type": "interval",
                                "seconds": 60,
                                "start_at": 123,
                            },
                        },
                    },
                    context,
                )
            with self.assertRaisesRegex(ValueError, "end_at must be"):
                execute_scheduled(
                    {
                        "action": "create",
                        "schedule": {
                            "prompt": "p",
                            "trigger": {"type": "interval", "seconds": 60},
                            "end_at": 123,
                        },
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
                ScheduledTaskTool().execute({"action": "delete", "schedule_id": "missing"}, context)
            with self.assertRaisesRegex(ValueError, "scheduled task not found"):
                ScheduledTaskTool().execute(
                    {
                        "action": "update",
                        "schedule_id": "missing",
                        "changes": {"enabled": False},
                    },
                    context,
                )

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
                    "action": "create",
                    "schedule": {
                        "prompt": "scheduled prompt",
                        "trigger": {
                            "type": "once",
                            "at": "2099-01-01T00:00:00+00:00",
                        },
                    },
                },
                context,
            )

            scheduled_session = result["schedule_session_id"]
            self.assertEqual(
                store.load(scheduled_session).permission_preset,
                PermissionPreset.WORKSPACE_ACCESS,
            )
            self.assertEqual(
                store.provider_for(scheduled_session),
                "configured",
            )

            session.permission_preset = PermissionPreset.FULL_ACCESS
            second = execute_scheduled(
                {
                    "action": "create",
                    "schedule": {
                        "prompt": "another scheduled prompt",
                        "trigger": {
                            "type": "once",
                            "at": "2099-01-02T00:00:00+00:00",
                        },
                    },
                },
                context,
            )
            self.assertEqual(
                store.load(second["schedule_session_id"]).permission_preset,
                PermissionPreset.WORKSPACE_ACCESS,
            )


class ToolSetTest(unittest.TestCase):
    def test_invalid_arguments_skip_routing_approval_and_execution(self):
        router, policy = Mock(), Mock()
        tool = FailingTool()
        tool.execute = Mock()
        tools = ToolCatalog((tool,)).select(
            (tool.name,), context_for(_TMP_WORKSPACE, execution_router=router), policy=policy,
        )

        for raw in ('{"value" "broken"}', '[]', 'null', '"text"'):
            with self.subTest(raw=raw):
                call = ToolCall.from_raw_arguments("bad-call", tool.name, raw)
                result = tools.execute(call)
                self.assertEqual(result.tool_call_id, call.id)
                self.assertEqual(result.name, call.name)
                self.assertEqual(result.error, call.argument_error)
                self.assertEqual(json.loads(result.to_content())["error"]["type"], "invalid_tool_arguments")
        router.resolve.assert_not_called()
        policy.authorize.assert_not_called()
        tool.execute.assert_not_called()

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
        self.assertEqual(anchor.item_index, len(session.items) - 1)
        self.assertEqual(session.items[-1].origin, "user_interaction")
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
        self.assertEqual(anchor.item_index, len(session.items) - 1)
        self.assertEqual(session.items[-1].origin, "user_interaction")
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


class WebSearchToolTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tool = _Bound(WebSearchTool(), _TMP_WORKSPACE, tool_settings=ToolSettings(
            web_search=WebSearchConfig(provider="exa", api_key="exa-key"),
        ))
        self.request = Mock(return_value=httpx.Response(200, json={"results": []}))
        client_type = httpx.Client
        self.client = self.enterContext(patch(
            "agent_core.tools.builtin.web_search.httpx.Client",
            side_effect=lambda **options: client_type(
                transport=httpx.MockTransport(self.request), **options,
            ),
        ))

    def test_returns_ranked_results_with_highlights(self) -> None:
        self.request.return_value = httpx.Response(200, json={"results": [
            {"title": "Exa API", "url": "https://docs.exa.ai/reference/search",
             "publishedDate": "2025-01-02", "highlights": ["Search the web"]},
            {"title": "Exa", "url": "https://exa.ai"},
        ]})
        result = self.tool.execute({"query": "exa search api"})
        self.client.assert_called_once_with(timeout=20.0)
        request = self.request.call_args.args[0]
        self.assertEqual(str(request.url), "https://api.exa.ai/search")
        self.assertEqual(request.headers["x-api-key"], "exa-key")
        self.assertEqual(json.loads(request.content), {
            "query": "exa search api", "type": "auto", "numResults": 5,
            "contents": {"highlights": {"maxCharacters": 1000}},
        })
        self.assertEqual(result, {
            "query": "exa search api", "results": [
                {"title": "Exa API", "url": "https://docs.exa.ai/reference/search",
                 "published_date": "2025-01-02", "highlights": ["Search the web"]},
                {"title": "Exa", "url": "https://exa.ai",
                 "published_date": None, "highlights": None},
            ],
        })

    def test_passes_result_limit_and_domain_filters(self) -> None:
        self.tool.execute({
            "query": "pytest fixtures", "num_results": 3,
            "include_domains": ["docs.pytest.org"], "exclude_domains": ["medium.com"],
        })
        self.assertEqual(json.loads(self.request.call_args.args[0].content), {
            "query": "pytest fixtures", "type": "auto", "numResults": 3,
            "includeDomains": ["docs.pytest.org"], "excludeDomains": ["medium.com"],
            "contents": {"highlights": {"maxCharacters": 1000}},
        })

    def test_rejects_invalid_arguments_before_request(self) -> None:
        for arguments in ({}, {"query": ""}, *(
            {"query": "exa", "num_results": count} for count in (0, 11, True, "3")
        ), {"query": "exa", "include_domains": []},
           {"query": "exa", "exclude_domains": [""]},
           {"query": "exa", "extra": True}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                self.tool.execute(arguments)
        self.request.assert_not_called()

    def test_key_failures_fall_back_once_for_both_providers(self) -> None:
        with patch("ddgs.DDGS") as ddgs:
            ddgs.return_value.text.return_value = [{"title": "Python", "href": "https://python.org"}]
            for provider in ("exa", "deepseek"):
                for key, status, reason in (
                    (None, None, "missing_api_key"),
                    ("invalid-key", 401, "authentication_failed"),
                ):
                    with self.subTest(provider=provider, key=key, status=status):
                        self.request.reset_mock()
                        ddgs.reset_mock()
                        if status:
                            self.request.return_value = httpx.Response(status, json={"error": "invalid-key"})
                        tool = _Bound(WebSearchTool(), _TMP_WORKSPACE, tool_settings=ToolSettings(
                            web_search=WebSearchConfig(provider=provider, api_key=key),
                        ))
                        result = tool.execute({"query": "python", "num_results": 2})
                        self.assertEqual(self.request.call_count, int(status is not None))
                        ddgs.return_value.text.assert_called_once_with(
                            "python", max_results=2, backend="duckduckgo",
                        )
                        self.assertEqual(result["provider"], "duckduckgo")
                        self.assertEqual(result["fallback"], {"from": provider, "reason": reason})
                        self.assertEqual(result["results"][0]["url"], "https://python.org")
                        self.assertNotIn("invalid-key", json.dumps(result))

    def test_fallback_carries_exa_domain_constraints(self) -> None:
        for status in (None, 401):
            with self.subTest(status=status), patch("ddgs.DDGS") as ddgs:
                ddgs.return_value.text.return_value = [
                    {"href": "https://docs.python.org/3/"},
                    {"href": "https://medium.com/python"},
                    {"href": "https://example.com/python"},
                ]
                self.request.return_value = httpx.Response(401)
                tool = _Bound(WebSearchTool(), _TMP_WORKSPACE, tool_settings=ToolSettings(
                    web_search=WebSearchConfig(provider="exa", api_key="bad" if status else None),
                ))
                result = tool.execute({
                    "query": "python", "include_domains": ["docs.python.org", "python.org"],
                    "exclude_domains": ["medium.com"],
                })
                ddgs.return_value.text.assert_called_once_with(
                    "python", max_results=5, backend="duckduckgo",
                )
                self.assertEqual(result["query"], "python")
                self.assertEqual([item["url"] for item in result["results"]], [
                    "https://docs.python.org/3/",
                ])

    def test_fallback_preserves_existing_query_operators(self) -> None:
        self.request.return_value = httpx.Response(401)
        for query in (
            "python site:docs.python.org",
            "python site:outside.example OR site:docs.python.org -site:medium.com",
            'python "site:example.com"',
        ):
            with self.subTest(query=query), patch("ddgs.DDGS") as ddgs:
                ddgs.return_value.text.return_value = [{"href": "https://docs.python.org/3/"}]
                result = self.tool.execute({
                    "query": query, "include_domains": ["docs.python.org"],
                    "exclude_domains": ["medium.com"],
                })
                ddgs.return_value.text.assert_called_once_with(
                    query, max_results=5, backend="duckduckgo",
                )
                self.assertEqual(result["query"], query)
                self.assertEqual(len(result["results"]), 1)

    def test_fallback_filters_domains_at_host_boundaries(self) -> None:
        self.request.return_value = httpx.Response(401)
        with patch("ddgs.DDGS") as ddgs:
            ddgs.return_value.text.return_value = [
                {"href": f"https://{host}/"} for host in (
                    "python.org", "www.python.org", "docs.python.org",
                    "sub.docs.python.org", "evilpython.org", "python.org.evil.example",
                )
            ]
            result = self.tool.execute({
                "query": "python", "num_results": 10,
                "include_domains": ["PYTHON.ORG."], "exclude_domains": ["docs.python.org"],
            })
            self.assertEqual([item["url"] for item in result["results"]], [
                "https://python.org/", "https://www.python.org/",
            ])

    def test_fallback_returns_empty_when_query_results_conflict_with_filter(self) -> None:
        self.request.return_value = httpx.Response(401)
        with patch("ddgs.DDGS") as ddgs:
            ddgs.return_value.text.return_value = [{"href": "https://outside.example/"}]
            result = self.tool.execute({
                "query": "python site:outside.example", "include_domains": ["python.org"],
            })
            self.assertEqual(result["results"], [])
            ddgs.return_value.text.assert_called_once_with(
                "python site:outside.example", max_results=5, backend="duckduckgo",
            )

    def test_fallback_honors_excluded_domains_without_an_include_filter(self) -> None:
        self.request.return_value = httpx.Response(401)
        with patch("ddgs.DDGS") as ddgs:
            ddgs.return_value.text.return_value = [
                {"href": "https://medium.com/python"},
                {"href": "https://blog.medium.com/python"},
                {"href": "https://python.org/"},
            ]
            result = self.tool.execute({"query": "python", "exclude_domains": ["medium.com"]})
            self.assertEqual([item["url"] for item in result["results"]], ["https://python.org/"])

    def test_other_errors_propagate_without_retry_or_fallback(self) -> None:
        with patch("ddgs.DDGS") as ddgs:
            for status in (400, 402, 403, 429, 500, 504):
                with self.subTest(status=status):
                    self.request.reset_mock()
                    self.request.return_value = httpx.Response(status)
                    with self.assertRaises(httpx.HTTPStatusError):
                        self.tool.execute({"query": "python"})
                    self.request.assert_called_once()
            self.request.side_effect = httpx.ReadTimeout("search timed out")
            with self.assertRaises(httpx.ReadTimeout):
                self.tool.execute({"query": "python"})
            ddgs.assert_not_called()

    def test_failed_fallback_propagates_without_looping(self) -> None:
        self.request.return_value = httpx.Response(401)
        with patch("ddgs.DDGS") as ddgs:
            ddgs.return_value.text.side_effect = TimeoutError("fallback timed out")
            with self.assertRaisesRegex(TimeoutError, "fallback timed out"):
                self.tool.execute({"query": "python"})
            ddgs.return_value.text.assert_called_once()
            self.request.assert_called_once()


class DuckDuckGoSearchToolTest(unittest.TestCase):
    def test_default_provider_returns_shared_result_format_without_api_key(self) -> None:
        tool = _Bound(WebSearchTool(), _TMP_WORKSPACE)
        with patch.dict("os.environ"), patch("ddgs.DDGS") as ddgs:
            os.environ.pop("EXA_API_KEY", None)
            ddgs.return_value.text.return_value = [
                {"title": "Python", "href": "https://python.org", "body": "x" * 1200},
                {"title": "Docs", "href": "https://docs.python.org"},
            ]
            result = tool.execute({"query": "python"})

        ddgs.assert_called_once_with(timeout=10)
        ddgs.return_value.text.assert_called_once_with(
            "python", max_results=5, backend="duckduckgo",
        )
        self.assertEqual(result, {
            "query": "python",
            "results": [
                {
                    "title": "Python", "url": "https://python.org",
                    "published_date": None, "highlights": ["x" * 1000],
                },
                {
                    "title": "Docs", "url": "https://docs.python.org",
                    "published_date": None, "highlights": None,
                },
            ],
        })

    def test_schema_follows_context_for_shared_tool(self) -> None:
        shared = WebSearchTool()
        for provider in ("duckduckgo", "exa", "deepseek"):
            with self.subTest(provider=provider):
                tool = _Bound(shared, _TMP_WORKSPACE, tool_settings=ToolSettings(
                    web_search=WebSearchConfig(provider=provider),
                ))
                definition = tool.definition
                properties = definition.parameters["properties"]
                self.assertEqual(set(properties), {"query", "num_results"} | (
                    {"include_domains", "exclude_domains"} if provider == "exa" else set()
                ))
                self.assertEqual(definition.parameters["required"], ["query"])
                self.assertFalse(definition.parameters["additionalProperties"])
                if provider == "deepseek":
                    self.assertEqual(properties["query"]["maxLength"], 128)
                    for syntax in ("site:", "-site:", "lang:", "after:", "before:", "OR"):
                        self.assertIn(syntax, properties["query"]["description"])
        self.assertIn("site:", _Bound(shared, _TMP_WORKSPACE).definition.parameters["properties"]["query"]["description"])

    def test_passes_site_query_and_result_limit_without_filtering_results(self) -> None:
        tool = _Bound(WebSearchTool(), _TMP_WORKSPACE)
        query = "python site:docs.python.org"
        with patch("ddgs.DDGS") as ddgs:
            ddgs.return_value.text.return_value = [
                {"href": "https://docs.python.org/3/"},
                {"href": "https://python.org/"},
                {"href": "https://example.com/"},
            ]
            result = tool.execute({
                "query": query, "num_results": 2,
            })

        ddgs.return_value.text.assert_called_once_with(
            query, max_results=2, backend="duckduckgo",
        )
        self.assertEqual(result["query"], query)
        self.assertEqual([item["url"] for item in result["results"]], [
            "https://docs.python.org/3/", "https://python.org/",
        ])

    def test_rejects_domain_parameters_before_calling_provider(self) -> None:
        tool = _Bound(WebSearchTool(), _TMP_WORKSPACE)
        with patch("ddgs.DDGS") as ddgs:
            for argument in ("include_domains", "exclude_domains", "extra"):
                for value in (["python.org"], [], None):
                    with self.subTest(argument=argument, value=value):
                        with self.assertRaisesRegex(
                            ValueError, "accepts only 'query', 'num_results'",
                        ):
                            tool.execute({"query": "python", argument: value})
        ddgs.assert_not_called()

    def test_empty_results(self) -> None:
        tool = _Bound(WebSearchTool(), _TMP_WORKSPACE)
        with patch("ddgs.DDGS") as ddgs:
            ddgs.return_value.text.return_value = []
            self.assertEqual(
                tool.execute({"query": "no results"}),
                {"query": "no results", "results": []},
            )

    def test_search_failure_propagates_without_switching_provider(self) -> None:
        tool = _Bound(WebSearchTool(), _TMP_WORKSPACE)
        with patch("ddgs.DDGS") as ddgs:
            ddgs.return_value.text.side_effect = TimeoutError("search timed out")
            with self.assertRaisesRegex(TimeoutError, "search timed out"):
                tool.execute({"query": "python"})

    def test_rejects_unknown_provider(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unsupported web search provider"):
            WebSearchConfig(provider="unknown")


class DeepSeekSearchToolTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tool = _Bound(WebSearchTool(), _TMP_WORKSPACE, tool_settings=ToolSettings(
            web_search=WebSearchConfig(provider="deepseek", api_key="test-key"),
        ))
        self.request = Mock(return_value=httpx.Response(200, json={"results": []}))
        client_type = httpx.Client
        self.client = self.enterContext(patch(
            "agent_core.tools.builtin.web_search.httpx.Client",
            side_effect=lambda **options: client_type(
                transport=httpx.MockTransport(self.request), **options,
            ),
        ))

    def test_posts_authenticated_query_and_returns_shared_result_format(self) -> None:
        query = "Python site:docs.python.org lang:zh after:2025"
        self.request.return_value = httpx.Response(200, json={"results": [
            {
                "title": "Python Docs", "url": "https://docs.python.org/3/",
                "date": "2026-10-17T00:00:00Z", "summary": "正文摘要" * 300,
                "snippet": "short excerpt", "content": "",
                "rerank_score": 2.4, "authority": 3,
                "site_name": "Python", "site_icon": None,
            },
            {
                "title": "Python", "url": "https://python.org/",
                "date": None, "summary": "", "snippet": "", "content": "",
                "rerank_score": 1.5, "authority": -1,
                "site_name": None, "site_icon": None,
            },
        ]})

        result = self.tool.execute({"query": query})

        self.client.assert_called_once_with(timeout=20.0)
        self.request.assert_called_once()
        request = self.request.call_args.args[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(str(request.url), "https://api.deepseek.com/v1/tools/search")
        self.assertEqual(request.headers["Authorization"], "Bearer test-key")
        self.assertEqual(request.headers["Content-Type"], "application/json")
        self.assertEqual(json.loads(request.content), {
            "query": query, "count": 5, "need_content": False,
        })
        self.assertEqual(result, {
            "query": query,
            "results": [
                {
                    "title": "Python Docs", "url": "https://docs.python.org/3/",
                    "published_date": "2026-10-17T00:00:00Z",
                    "highlights": [("正文摘要" * 300)[:1000]],
                },
                {
                    "title": "Python", "url": "https://python.org/",
                    "published_date": None, "highlights": None,
                },
            ],
        })

    def test_passes_count_and_bounds_returned_results(self) -> None:
        self.request.return_value = httpx.Response(200, json={"results": [
            {"title": "First", "url": "https://example.com/1", "date": None,
             "summary": "First summary"},
            {"title": "Second", "url": "https://example.com/2", "date": None,
             "summary": "Second summary"},
        ]})
        result = self.tool.execute({"query": "python", "num_results": 1})
        self.assertEqual(json.loads(self.request.call_args.args[0].content)["count"], 1)
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["results"][0]["title"], "First")

    def test_empty_results(self) -> None:
        self.assertEqual(self.tool.execute({"query": "no results"}), {
            "query": "no results", "results": [],
        })

    def test_accepts_unicode_query_boundary_and_maximum_result_count(self) -> None:
        query = "搜" * 128
        self.tool.execute({"query": query, "num_results": 10})
        self.assertEqual(json.loads(self.request.call_args.args[0].content), {
            "query": query, "count": 10, "need_content": False,
        })

    def test_rejects_invalid_queries_before_network_request(self) -> None:
        for query in (None, True, 12, "", " \t\n", "\u3000", "搜" * 129):
            with self.subTest(query=query), self.assertRaisesRegex(ValueError, "query"):
                self.tool.execute({"query": query})
        self.client.assert_not_called()

    def test_rejects_invalid_counts_and_unsupported_parameters(self) -> None:
        for count in (0, 11, True, "3", 1.5, None):
            with self.subTest(count=count), self.assertRaisesRegex(ValueError, "num_results"):
                self.tool.execute({"query": "python", "num_results": count})
        for argument in ("include_domains", "exclude_domains", "need_content", "extra"):
            with self.subTest(argument=argument), self.assertRaisesRegex(ValueError, "accepts only"):
                self.tool.execute({"query": "python", argument: True})
        self.client.assert_not_called()

    def test_http_errors_propagate_without_retry_or_provider_switch(self) -> None:
        with patch("ddgs.DDGS") as ddgs:
            for status in (400, 402, 403, 429, 500, 504):
                with self.subTest(status=status):
                    self.request.reset_mock()
                    self.request.return_value = httpx.Response(
                        status, json={"error": {"message": "search failed"}},
                    )
                    with self.assertRaises(httpx.HTTPStatusError):
                        self.tool.execute({"query": "python"})
                    self.request.assert_called_once()
        ddgs.assert_not_called()

    def test_network_timeout_propagates(self) -> None:
        self.request.side_effect = httpx.ReadTimeout("search timed out")
        with self.assertRaisesRegex(httpx.ReadTimeout, "search timed out"):
            self.tool.execute({"query": "python"})
        self.request.assert_called_once()


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


class WriteFileToolTest(unittest.TestCase):
    def test_creates_missing_parent_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))

            result = _Bound(WriteFileTool(), workspace).execute(
                {
                    "path": "src/foo/bar.py",
                    "content": "print('hello')\n",
                }
            )

            self.assertEqual(
                result,
                {
                    "path": "src/foo/bar.py",
                    "created": True,
                    "created_parent_dirs": ["src", "src/foo"],
                    "bytes_written": 15,
                },
            )
            self.assertEqual(
                (workspace.path / "src/foo/bar.py").read_text(encoding="utf-8"),
                "print('hello')\n",
            )

    def test_reports_only_parent_directories_created_by_the_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            (workspace.path / "src").mkdir()

            result = _Bound(WriteFileTool(), workspace).execute(
                {"path": "src/foo/bar.py", "content": "content"}
            )

            self.assertEqual(result["created_parent_dirs"], ["src/foo"])

    def test_reports_overwrite_as_not_created(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            file_path = workspace.path / "notes.txt"
            file_path.write_text("old", encoding="utf-8")

            result = _Bound(WriteFileTool(), workspace).execute(
                {
                    "path": "notes.txt",
                    "content": "new",
                    "overwrite": True,
                }
            )

            self.assertEqual(
                result,
                {
                    "path": "notes.txt",
                    "created": False,
                    "created_parent_dirs": [],
                    "bytes_written": 3,
                },
            )
            self.assertEqual(file_path.read_text(encoding="utf-8"), "new")

    def test_rejects_file_in_parent_chain_before_creating_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            (workspace.path / "a").mkdir()
            blocking_file = workspace.path / "a/b"
            blocking_file.write_text("file", encoding="utf-8")

            with self.assertRaises(NotADirectoryError):
                _Bound(WriteFileTool(), workspace).execute(
                    {"path": "a/b/c/d.txt", "content": "content"}
                )

            self.assertEqual(blocking_file.read_text(encoding="utf-8"), "file")
            self.assertFalse((workspace.path / "a/b/c").exists())

    def test_rejects_workspace_escape_without_creating_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            outside = workspace.path.parent / f"{workspace.path.name}-outside"

            with self.assertRaisesRegex(ValueError, "within the workspace"):
                _Bound(WriteFileTool(), workspace).execute(
                    {"path": f"new/../../{outside.name}/file.txt", "content": "x"}
                )

            self.assertFalse(workspace.path.joinpath("new").exists())
            self.assertFalse(outside.exists())

    @unittest.skipUnless(
        SYMLINKS_AVAILABLE,
        "creating symlinks needs Developer Mode or administrator rights "
        "on Windows",
    )
    def test_rejects_symlink_parent_outside_workspace_without_creating(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            tempfile.TemporaryDirectory() as outside_directory,
        ):
            workspace = Workspace(Path(directory))
            outside = Path(outside_directory)
            os.symlink(outside, workspace.path / "link")

            with self.assertRaisesRegex(ValueError, "within the workspace"):
                _Bound(WriteFileTool(), workspace).execute(
                    {"path": "link/missing/file.txt", "content": "x"}
                )

            self.assertFalse((outside / "missing").exists())


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
    def test_rejects_cancelled_call_before_accessing_workspace(self) -> None:
        control = TurnControl()
        control.cancel()
        tool = _Bound(ListDirectoryTool(), _TMP_WORKSPACE, cancellation=control)
        with patch.object(Workspace, "resolve_path") as resolve:
            with self.assertRaises(AgentCancelled):
                tool.execute({"path": "."})
        resolve.assert_not_called()

    def test_cancels_during_enumeration_without_matching_entries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            (workspace.path / "a.txt").write_text("a", encoding="utf-8")
            control = TurnControl()
            tool = _Bound(ListDirectoryTool(), workspace, cancellation=control)
            with os.scandir(workspace.path) as iterator:
                entries = list(iterator)

            def cancelling_entries():
                control.cancel()
                yield from entries

            with patch("agent_core.tools.builtin.list_directory.os.scandir") as scandir:
                scandir.return_value.__enter__.return_value = cancelling_entries()
                with self.assertRaises(AgentCancelled):
                    tool.execute({"path": ".", "glob": "*.py", "recursive": True})
                scandir.return_value.__exit__.assert_called_once()

    def test_times_out_during_enumeration_without_matching_entries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            (workspace.path / "a.txt").write_text("a", encoding="utf-8")
            tool = _Bound(ListDirectoryTool(), workspace)
            schema = tool.definition.parameters["properties"]["timeout_seconds"]
            self.assertEqual(schema["default"], 60)
            self.assertEqual(schema["minimum"], 1)
            with os.scandir(workspace.path) as iterator:
                entries = list(iterator)
            for timeout in (None, 2):
                with self.subTest(timeout=timeout):
                    clock = SimpleNamespace(now=0.0)

                    def slow_entries():
                        clock.now = 60.0 if timeout is None else float(timeout)
                        yield from entries

                    arguments = {"path": ".", "glob": "*.py", "recursive": True}
                    if timeout is not None:
                        arguments["timeout_seconds"] = timeout
                    with (
                        patch("agent_core.tools.builtin.list_directory.os.scandir") as scandir,
                        patch(
                            "agent_core.tools.builtin.list_directory.time.monotonic",
                            side_effect=lambda: clock.now,
                        ),
                    ):
                        scandir.return_value.__enter__.return_value = slow_entries()
                        with self.assertRaisesRegex(TimeoutError, f"after {timeout or 60} seconds"):
                            tool.execute(arguments)

    def test_checks_budget_after_empty_enumeration_and_result_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            tool = _Bound(ListDirectoryTool(), workspace)
            clock = SimpleNamespace(now=0.0)

            def slow_empty_entries():
                clock.now = 60.0
                return iter(())

            with (
                patch("agent_core.tools.builtin.list_directory.os.scandir") as scandir,
                patch(
                    "agent_core.tools.builtin.list_directory.time.monotonic",
                    side_effect=lambda: clock.now,
                ),
            ):
                scandir.return_value.__enter__.side_effect = slow_empty_entries
                with self.assertRaises(TimeoutError):
                    tool.execute({"path": "."})

            (workspace.path / "a.txt").write_text("a", encoding="utf-8")
            clock.now = 0.0

            def slow_type(_entry):
                clock.now = 60.0
                return "file"

            with (
                patch("agent_core.tools.builtin.list_directory._entry_type", side_effect=slow_type),
                patch(
                    "agent_core.tools.builtin.list_directory.time.monotonic",
                    side_effect=lambda: clock.now,
                ),
            ):
                with self.assertRaises(TimeoutError):
                    tool.execute({"path": "."})

    def test_rejects_invalid_timeouts(self) -> None:
        tool = _Bound(ListDirectoryTool(), _TMP_WORKSPACE)
        for timeout in (0, -1, True, 1.5, "1", None):
            with self.subTest(timeout=timeout):
                with self.assertRaisesRegex(ValueError, "timeout_seconds"):
                    tool.execute({"path": ".", "timeout_seconds": timeout})

    @unittest.skipUnless(os.name == "nt", "directory junctions are a Windows feature")
    def test_lists_junctions_without_traversing_cycles_or_external_targets(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            tempfile.TemporaryDirectory() as outside,
        ):
            workspace = Workspace(Path(directory))
            nested = workspace.path / "nested"
            nested.mkdir()
            (nested / "a.txt").write_text("a", encoding="utf-8")
            (Path(outside) / "outside.txt").write_text("outside", encoding="utf-8")
            links = [workspace.path / "alias", nested / "ancestor", workspace.path / "external"]
            created = []
            try:
                for link, target in zip(links, (nested, workspace.path, Path(outside))):
                    subprocess.run(
                        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                        check=True, capture_output=True,
                    )
                    created.append(link)
                result = _Bound(ListDirectoryTool(), workspace).execute(
                    {"path": ".", "glob": "**/*", "recursive": True, "timeout_seconds": 2}
                )
                self.assertEqual(
                    [entry["name"] for entry in result["entries"]],
                    ["alias", "external", "nested", "nested/a.txt", "nested/ancestor"],
                )
                self.assertFalse(result["has_more"])
            finally:
                for link in created:
                    link.rmdir()

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
            self.assertIsNone(result["next_cursor"])

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
            with patch(
                "agent_core.tools.builtin.list_directory._iter_entries",
                return_value=iter(paths),
            ):
                first = _Bound(ListDirectoryTool(), workspace).execute(
                    {
                        "path": ".",
                        "glob": "**/*.py",
                        "recursive": True,
                        "limit": 1,
                    }
                )
            with patch(
                "agent_core.tools.builtin.list_directory._iter_entries",
                return_value=iter(paths),
            ):
                result = _Bound(ListDirectoryTool(), workspace).execute(
                    {
                        "path": ".",
                        "glob": "**/*.py",
                        "recursive": True,
                        "cursor": first["next_cursor"],
                        "limit": 1,
                    }
                )

            self.assertEqual(first["entries"], [{"name": "a.py", "type": "file"}])
            self.assertEqual(
                result,
                {
                    "path": ".",
                    "entries": [{"name": "c.py", "type": "file"}],
                    "has_more": True,
                    "next_cursor": "c.py",
                },
            )

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

    def test_returns_stable_path_order_with_bounded_page_size(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            paths = []
            for name in ("a.txt", "b.txt", "c.txt", "d.txt"):
                path = workspace.path / name
                path.write_text(name, encoding="utf-8")
                paths.append(path)
            with patch(
                "agent_core.tools.builtin.list_directory._iter_entries",
                return_value=iter(reversed(paths)),
            ):
                result = _Bound(ListDirectoryTool(), workspace).execute(
                    {"path": ".", "limit": 2}
                )

            self.assertEqual(len(result["entries"]), 2)
            self.assertEqual(
                [entry["name"] for entry in result["entries"]],
                ["a.txt", "b.txt"],
            )
            self.assertTrue(result["has_more"])
            self.assertEqual(result["next_cursor"], "b.txt")

    def test_cursor_pagination_is_stable_when_earlier_entries_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            for name in ("b.txt", "c.txt", "d.txt"):
                (workspace.path / name).write_text(name, encoding="utf-8")
            tool = _Bound(ListDirectoryTool(), workspace)
            first = tool.execute({"path": ".", "limit": 2})

            (workspace.path / "a.txt").write_text("a", encoding="utf-8")
            (workspace.path / "b.txt").unlink()
            second = tool.execute(
                {
                    "path": ".",
                    "cursor": first["next_cursor"],
                    "limit": 2,
                }
            )

            self.assertEqual(
                [entry["name"] for entry in first["entries"]],
                ["b.txt", "c.txt"],
            )
            self.assertEqual(
                [entry["name"] for entry in second["entries"]],
                ["d.txt"],
            )
            self.assertFalse(second["has_more"])
            self.assertIsNone(second["next_cursor"])

    def test_rejects_offset_and_invalid_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tool = _Bound(ListDirectoryTool(), Workspace(Path(directory)))

            with self.assertRaisesRegex(ValueError, "accepts only"):
                tool.execute({"path": ".", "offset": 1})
            for cursor in ("", 1, True):
                with self.subTest(cursor=cursor):
                    with self.assertRaisesRegex(ValueError, "cursor"):
                        tool.execute({"path": ".", "cursor": cursor})

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
            for name in (
                "node_modules", ".venv", "build", ".pnpm-store", ".npm",
                ".cache", ".yarn", "pip-cache", "uv-cache",
            ):
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

    def test_can_search_an_explicit_cache_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            cache = workspace.path / ".pnpm-store"
            cache.mkdir()
            (cache / "match.txt").write_text("Nosis", encoding="utf-8")

            result = _Bound(SearchFilesTool(), workspace).execute(
                {"path": ".pnpm-store", "pattern": "Nosis"}
            )

            self.assertEqual(result["matches"][0]["path"], ".pnpm-store/match.txt")

    def test_resolves_only_the_search_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            nested = workspace.path / "nested"
            nested.mkdir()
            (nested / "match.txt").write_text("Nosis", encoding="utf-8")
            tool = _Bound(SearchFilesTool(), workspace)
            resolved = []
            original_resolve = Path.resolve

            def resolve(path, *args, **kwargs):
                resolved.append(path)
                return original_resolve(path, *args, **kwargs)

            with patch.object(Path, "resolve", resolve):
                result = tool.execute({"path": ".", "pattern": "Nosis"})

            self.assertEqual(result["scanned_files"], 1)
            self.assertEqual(resolved, [workspace.path])

    def test_times_out_during_directory_enumeration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            (workspace.path / "empty").mkdir()
            tool = _Bound(SearchFilesTool(), workspace)
            clock = SimpleNamespace(now=0.0)
            with os.scandir(workspace.path) as iterator:
                entries = list(iterator)

            def slow_entries():
                clock.now = 1.0
                yield from entries

            with (
                patch("agent_core.tools.builtin.search_files.os.scandir") as scandir,
                patch(
                    "agent_core.tools.builtin.search_files.time.monotonic",
                    side_effect=lambda: clock.now,
                ),
            ):
                scandir.return_value.__enter__.return_value = slow_entries()
                with self.assertRaisesRegex(TimeoutError, "after 1 seconds"):
                    tool.execute({"path": ".", "pattern": "x", "timeout_seconds": 1})

    def test_times_out_while_scanning_lines_without_matches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            (workspace.path / "notes.txt").write_text("one\ntwo", encoding="utf-8")
            tool = _Bound(SearchFilesTool(), workspace)
            clock = SimpleNamespace(now=0.0)

            def slow_match(_value):
                clock.now = 2.0
                return False

            matcher = Mock(side_effect=slow_match)
            with (
                patch(
                    "agent_core.tools.builtin.search_files._create_matcher",
                    return_value=matcher,
                ),
                patch(
                    "agent_core.tools.builtin.search_files.time.monotonic",
                    side_effect=lambda: clock.now,
                ),
            ):
                with self.assertRaises(TimeoutError):
                    tool.execute({"path": ".", "pattern": "x", "timeout_seconds": 2})

            self.assertEqual(matcher.call_count, 1)

    def test_checks_deadline_after_last_file_in_files_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            (workspace.path / "notes.txt").write_text("", encoding="utf-8")
            tool = _Bound(SearchFilesTool(), workspace)
            clock = SimpleNamespace(now=0.0)

            def slow_match(_value):
                clock.now = 60.0
                return False

            schema = tool.definition.parameters["properties"]["timeout_seconds"]
            self.assertEqual(schema["default"], 60)
            with (
                patch(
                    "agent_core.tools.builtin.search_files._create_matcher",
                    return_value=slow_match,
                ),
                patch(
                    "agent_core.tools.builtin.search_files.time.monotonic",
                    side_effect=lambda: clock.now,
                ),
            ):
                with self.assertRaisesRegex(TimeoutError, "after 60 seconds"):
                    tool.execute({"path": ".", "pattern": "x", "mode": "files"})

    def test_honors_cancellation_during_search(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            (workspace.path / "notes.txt").write_text("one\ntwo", encoding="utf-8")
            control = TurnControl()
            tool = _Bound(SearchFilesTool(), workspace, cancellation=control)

            def cancel(_value):
                control.cancel()
                return False

            matcher = Mock(side_effect=cancel)
            with patch(
                "agent_core.tools.builtin.search_files._create_matcher",
                return_value=matcher,
            ):
                with self.assertRaises(AgentCancelled):
                    tool.execute({"path": ".", "pattern": "x"})

            self.assertEqual(matcher.call_count, 1)

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

    @unittest.skipUnless(os.name == "nt", "directory junctions are a Windows feature")
    def test_skips_junctions_to_descendants_and_ancestors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            nested = workspace.path / "nested"
            nested.mkdir()
            (nested / "match.txt").write_text("Nosis", encoding="utf-8")
            links = [workspace.path / "alias", nested / "ancestor"]
            for link, target in zip(links, (nested, workspace.path)):
                subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                    check=True,
                    capture_output=True,
                )
            try:
                tool = _Bound(SearchFilesTool(), workspace)
                for mode in ("files", "content"):
                    with self.subTest(mode=mode):
                        result = tool.execute(
                            {"path": ".", "pattern": "match" if mode == "files" else "Nosis", "mode": mode}
                        )
                        self.assertEqual(
                            [match["path"] for match in result["matches"]],
                            ["nested/match.txt"],
                        )
                        self.assertEqual(result["scanned_files"], 1)
                        self.assertEqual(result["skipped_files"], 2)
            finally:
                for link in links:
                    link.rmdir()

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
                {"path": ".", "pattern": "x", "timeout_seconds": 0},
                {"path": ".", "pattern": "x", "timeout_seconds": -1},
                {"path": ".", "pattern": "x", "timeout_seconds": True},
                {"path": ".", "pattern": "x", "timeout_seconds": 1.5},
                {"path": ".", "pattern": "x", "timeout_seconds": "1"},
                {"path": ".", "pattern": "x", "unknown": True},
            )

            for arguments in invalid_arguments:
                with self.subTest(arguments=arguments):
                    with self.assertRaises(ValueError):
                        tool.execute(arguments)


class ShellToolTest(unittest.TestCase):
    def test_description_uses_the_policy_for_write_and_network_restrictions(self) -> None:
        for access, restricted in (
            (FilesystemAccess.WRITE_RESTRICTED, True),
            (FilesystemAccess.DENIED, False),
        ):
            for network, available in (
                (NetworkAccess.ALLOW, True),
                (NetworkAccess.DENY, False),
            ):
                with self.subTest(host_filesystem=access, network=network):
                    executor = SimpleNamespace(
                        policy=SimpleNamespace(
                            host_filesystem=access, network=network,
                        ),
                    )
                    tool = _Bound(
                        ShellTool(), _TMP_WORKSPACE,
                        execution_router=ExecutionRouter(executor, UnusedExecutor()),
                    )
                    description = tool.definition.description
                    self.assertEqual(
                        "best-effort write boundary" in description, restricted
                    )
                    self.assertEqual("writable by Everyone" in description, restricted)
                    self.assertEqual(
                        "Network access remains available" in description, available
                    )
                    self.assertEqual(
                        "Network access is disabled" in description, not available
                    )

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

    def test_passes_explicit_timeouts_below_and_above_default(self) -> None:
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

        for timeout in (10, 61, 900):
            with self.subTest(timeout=timeout):
                tool.execute({"command": "pwd", "timeout_seconds": timeout})

        self.assertEqual(executor.timeouts, [10, 61, 900])

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
            "PowerShell 7" if os.name == "nt" else "/bin/sh",
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
