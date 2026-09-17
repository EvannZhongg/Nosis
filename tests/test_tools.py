import json
import os
import subprocess
import threading
import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_core import (
    AskUserTool,
    CommandExecutionResult,
    EditFileTool,
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
    WebSearchTool,
    Workspace,
    builtin_catalog,
)
from agent_core.tools.builtin.search_files import MAX_OUTPUT_CHARS
from agent_core.tools.builtin.read_file import (
    MAX_FILE_SIZE_BYTES as MAX_READ_FILE_SIZE_BYTES,
    MAX_READ_CHARS,
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
        return self._tool.execute(arguments, self._context)


def context_for(workspace, **fields):
    return ToolExecutionContext(
        workspace=workspace,
        session=fields.pop("session", None) or Session(),
        **fields,
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
        return context.command_executor is not None

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
                context_for(workspace, command_executor=UnusedExecutor()),
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

    def test_defaults_to_first_2000_lines(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            (workspace.path / "notes.txt").write_text(
                "\n".join(
                    f"line {number}" for number in range(1, 2002)
                ),
                encoding="utf-8",
            )

            result = _Bound(ReadFileTool(), workspace).execute({"path": "notes.txt"})

            content_lines = result["content"].splitlines()
            self.assertEqual(content_lines[0], "1| line 1")
            self.assertEqual(content_lines[1999], "2000| line 2000")
            self.assertEqual(
                content_lines[-1],
                (
                    "(Showing lines 1-2000. "
                    "Use offset=2001 to continue.)"
                ),
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

            self.assertLessEqual(len(result["content"]), MAX_READ_CHARS)
            self.assertIn("character limit reached", result["content"])
            self.assertIn("Use offset=", result["content"])
            numbered_lines = [
                line
                for line in result["content"].splitlines()
                if "| " in line
            ]
            self.assertLess(len(numbered_lines), 80)

    def test_truncates_an_overlong_single_line_with_notice(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            (workspace.path / "notes.txt").write_text(
                "x" * (MAX_READ_CHARS * 2),
                encoding="utf-8",
            )

            result = _Bound(ReadFileTool(), workspace).execute(
                {"path": "notes.txt"}
            )

            self.assertLessEqual(len(result["content"]), MAX_READ_CHARS)
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


class ListDirectoryToolTest(unittest.TestCase):
    def test_lists_immediate_entries_in_name_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            (workspace.path / "z.txt").write_text("z", encoding="utf-8")
            (workspace.path / "a").mkdir()

            result = _Bound(ListDirectoryTool(), workspace).execute({"path": "."})

            self.assertEqual(
                result,
                {
                    "path": ".",
                    "entries": [
                        {"name": "a", "type": "directory"},
                        {"name": "z.txt", "type": "file"},
                    ],
                },
            )

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

            self.assertEqual(
                result,
                {
                    "path": ".",
                    "entries": [
                        {"name": "link.txt", "type": "symlink"},
                        {"name": "z.txt", "type": "file"},
                    ],
                },
            )

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

    def test_stops_traversal_at_path_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            for index in range(3):
                (workspace.path / f"{index}.txt").write_text(
                    "Nosis",
                    encoding="utf-8",
                )

            with patch(
                "agent_core.tools.builtin.search_files."
                "MAX_SCANNED_PATHS",
                2,
            ):
                result = _Bound(SearchFilesTool(), workspace).execute(
                    {"path": ".", "pattern": "Nosis"}
                )

            self.assertTrue(result["has_more"])
            self.assertEqual(result["next_offset"], 2)
            self.assertEqual(result["scanned_files"], 2)

    def test_limits_serialized_tool_result_size(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            (workspace.path / "notes.txt").write_text(
                "Nosis " + ("x" * (MAX_OUTPUT_CHARS * 2)),
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
            self.assertLessEqual(len(content), MAX_OUTPUT_CHARS)
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
            ShellTool(), _TMP_WORKSPACE, command_executor=executor
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
            command_executor=executor,
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
            command_executor=executor,
        )

        tool.execute({"command": "pwd", "timeout_seconds": 61})
        tool.execute({"command": "pwd", "timeout_seconds": 86400})

        self.assertEqual(executor.timeouts, [61, 86400])

    def test_advertises_the_shell_and_timeout_it_uses(self) -> None:
        class UnusedExecutor:
            def execute(self, command, timeout_seconds=60):
                raise AssertionError("executor should not be called")

        tool = _Bound(
            ShellTool(),
            _TMP_WORKSPACE,
            command_executor=UnusedExecutor(),
        )

        definition = tool.definition
        timeout_schema = definition.parameters["properties"][
            "timeout_seconds"
        ]

        self.assertEqual(timeout_schema["maximum"], 86400)
        self.assertIn("60 seconds by default", definition.description)
        self.assertIn("maximum of 86400", timeout_schema["description"])
        self.assertIn(
            "Git Bash" if os.name == "nt" else "/bin/sh",
            definition.description,
        )

    def test_validates_arguments(self) -> None:
        class UnusedExecutor:
            def execute(self, command, timeout_seconds=60):
                raise AssertionError("executor should not be called")

        tool = _Bound(
            ShellTool(), _TMP_WORKSPACE, command_executor=UnusedExecutor()
        )

        with self.assertRaisesRegex(ValueError, "non-empty string"):
            tool.execute({})
        with self.assertRaisesRegex(ValueError, "accepts only"):
            tool.execute({"command": "pwd", "extra": True})
        for timeout_seconds in (0, 86401, True, "10"):
            with self.subTest(timeout_seconds=timeout_seconds):
                with self.assertRaisesRegex(ValueError, "between 1 and 86400"):
                    tool.execute(
                        {
                            "command": "pwd",
                            "timeout_seconds": timeout_seconds,
                        }
                    )


if __name__ == "__main__":
    unittest.main()
