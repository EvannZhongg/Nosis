import json
import os
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timezone
from pathlib import Path

from agent_core import (
    JsonlSessionStore,
    LLMRequest,
    LLMResponse,
    Message,
    TokenUsage,
    ToolCall,
)
from agent_core.session_paths import (
    default_sessions_directory,
    session_log_path,
    workspace_key,
)


class JsonlSessionStoreTest(unittest.TestCase):
    def test_default_sessions_directory_survives_empty_environment(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(default_sessions_directory().is_absolute())

    def test_lists_sessions_once_in_recent_turn_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            sessions_directory = workspace / "sessions"
            store = JsonlSessionStore(sessions_directory)

            self.assertEqual(store.list_sessions(), [])

            turns = (
                ("first", "第一轮"),
                ("second", "另一个会话"),
                ("first", "后续问题"),
            )
            for index, (session_id, content) in enumerate(turns, start=1):
                store.bind_workspace(session_id, workspace)
                store.append_turn(
                    session_id,
                    LLMRequest("prompt", ()),
                    LLMResponse("answer"),
                    (
                        Message("user", content),
                        Message("assistant", "answer"),
                    ),
                    workspace=workspace,
                )
                os.utime(
                    session_log_path(sessions_directory, workspace, session_id),
                    (index, index),
                )

            # One workspace group, each session once, titled by its first
            # user message.
            self.assertEqual(
                store.list_sessions(),
                [
                    {
                        "workspace": str(workspace.resolve()),
                        "sessions": [
                            {"session_id": "first", "title": "第一轮"},
                            {"session_id": "second", "title": "另一个会话"},
                        ],
                    }
                ],
            )

    def test_lists_session_id_when_no_user_message_was_stored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = JsonlSessionStore(workspace / "sessions")
            store.bind_workspace("no-title", workspace)
            store.append_turn(
                "no-title",
                LLMRequest("prompt", ()),
                LLMResponse("answer"),
                (Message("assistant", "answer"),),
                workspace=workspace,
            )

            self.assertEqual(
                store.list_sessions(),
                [
                    {
                        "workspace": str(workspace.resolve()),
                        "sessions": [
                            {"session_id": "no-title", "title": "no-title"}
                        ],
                    }
                ],
            )

    def test_ignores_session_directories_without_a_log(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sessions_directory = Path(directory) / "sessions"
            # A normalized tool result creates the directory before the
            # first turn is appended.
            (sessions_directory / "artifacts-only").mkdir(parents=True)

            self.assertEqual(
                JsonlSessionStore(sessions_directory).list_sessions(),
                [],
            )

    def test_appends_complete_turn_items(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            sessions_directory = workspace / "sessions"
            store = JsonlSessionStore(sessions_directory)
            request_time = datetime(2026, 9, 9, 8, 0, tzinfo=timezone.utc)
            response_time = datetime(2026, 9, 9, 8, 1, tzinfo=timezone.utc)
            request = LLMRequest(
                system_prompt="Be helpful.",
                messages=(Message(role="user", content="你好"),),
                max_generation_tokens=100,
            )
            response = LLMResponse(
                content="你好！",
                usage=TokenUsage(
                    input_tokens=20,
                    output_tokens=8,
                    total_tokens=28,
                ),
            )
            items = (
                Message(
                    role="user",
                    content="你好",
                    timestamp_utc=request_time,
                ),
                Message(
                    role="assistant",
                    content="你好！",
                    timestamp_utc=response_time,
                ),
            )

            store.append_turn(
                "session-1",
                request,
                response,
                items,
                workspace=workspace,
            )

            path = session_log_path(sessions_directory, workspace, "session-1")
            record = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(record["session_id"], "session-1")
            self.assertEqual(
                record["items"],
                [
                    {
                        "role": "user",
                        "content": "你好",
                        "timestamp_utc": "2026-09-09T08:00:00Z",
                    },
                    {
                        "role": "assistant",
                        "content": "你好！",
                        "timestamp_utc": "2026-09-09T08:01:00Z",
                    },
                ],
            )
            self.assertEqual(
                record["request"],
                {
                    "system_prompt": "Be helpful.",
                    "messages": [
                        {"role": "user", "content": "你好"},
                    ],
                    "max_generation_tokens": 100,
                },
            )
            self.assertEqual(
                record["response"],
                {
                    "content": "你好！",
                    "usage": {
                        "input_tokens": 20,
                        "output_tokens": 8,
                        "total_tokens": 28,
                    },
                },
            )

    def test_appends_items_from_a_turn_without_a_response(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            sessions_directory = workspace / "sessions"
            store = JsonlSessionStore(sessions_directory)
            store.bind_workspace("session-1", workspace)

            store.append_items(
                "session-1",
                (
                    Message(
                        role="user",
                        content="interrupted",
                        timestamp_utc=datetime(
                            2026, 9, 9, 8, 0, tzinfo=timezone.utc
                        ),
                    ),
                    Message(role="assistant", content="partial answer"),
                ),
                workspace=workspace,
            )

            record = json.loads(
                session_log_path(
                    sessions_directory, workspace, "session-1"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(
                [item["content"] for item in record["items"]],
                ["interrupted", "partial answer"],
            )
            self.assertNotIn("request", record)
            self.assertNotIn("response", record)

            session = store.load("session-1")
            self.assertEqual(
                [message.content for message in session.items],
                ["interrupted", "partial answer"],
            )

    def test_loads_an_unwritten_session_as_empty(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            sessions_directory = workspace / "sessions"
            store = JsonlSessionStore(sessions_directory)
            store.bind_workspace("session-1", workspace)

            session = store.load("session-1")

            self.assertEqual(session.session_id, "session-1")
            self.assertEqual(session.items, [])
            self.assertFalse(store.has_transcript("session-1"))
            self.assertFalse(
                session_log_path(sessions_directory, workspace, "session-1").exists()
            )

    def test_moves_a_session_to_the_group_of_its_new_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            sessions_directory = root / "sessions"
            store = JsonlSessionStore(sessions_directory)
            store.bind_workspace("session-1", first)
            store.append_turn(
                "session-1",
                LLMRequest("prompt", ()),
                LLMResponse("answer"),
                (Message("user", "hello"), Message("assistant", "answer")),
                workspace=first,
            )

            store.bind_workspace("session-1", second)

            self.assertEqual(store.workspace_for("session-1"), str(second.resolve()))
            self.assertEqual(
                [message.content for message in store.load("session-1").items],
                ["hello", "answer"],
            )
            self.assertTrue(
                session_log_path(sessions_directory, second, "session-1").is_file()
            )
            self.assertFalse(
                session_log_path(sessions_directory, first, "session-1").exists()
            )

    def test_loads_session_with_tool_call_items(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            sessions_directory = workspace / "sessions"
            store = JsonlSessionStore(sessions_directory)
            tool_call = ToolCall(
                id="call-1",
                name="read_file",
                arguments={"path": "README.md"},
            )
            first_items = (
                Message(
                    role="user",
                    content="read README.md",
                    timestamp_utc=datetime(
                        2026, 9, 9, 8, 0, tzinfo=timezone.utc
                    ),
                ),
                Message(
                    role="assistant",
                    content=None,
                    timestamp_utc=datetime(
                        2026, 9, 9, 8, 0, 1, tzinfo=timezone.utc
                    ),
                    tool_calls=(tool_call,),
                ),
                Message(
                    role="tool",
                    content='{"ok": true}',
                    timestamp_utc=datetime(
                        2026, 9, 9, 8, 0, 2, tzinfo=timezone.utc
                    ),
                    tool_call_id="call-1",
                ),
                Message(
                    role="assistant",
                    content="README.md was read.",
                    timestamp_utc=datetime(
                        2026, 9, 9, 8, 1, tzinfo=timezone.utc
                    ),
                ),
            )
            second_items = (
                Message(
                    role="user",
                    content="thanks",
                    timestamp_utc=datetime(
                        2026, 9, 9, 8, 2, tzinfo=timezone.utc
                    ),
                ),
                Message(
                    role="assistant",
                    content="You're welcome.",
                    timestamp_utc=datetime(
                        2026, 9, 9, 8, 3, tzinfo=timezone.utc
                    ),
                ),
            )

            store.bind_workspace("session-1", workspace)
            store.append_turn(
                "session-1",
                LLMRequest(
                    system_prompt="Be helpful.",
                    messages=first_items[:-1],
                ),
                LLMResponse(content="README.md was read."),
                first_items,
                workspace=workspace,
            )
            store.bind_workspace("session-2", workspace)
            store.append_turn(
                "session-2",
                LLMRequest(
                    system_prompt="Be helpful.",
                    messages=(Message(role="user", content="other"),),
                ),
                LLMResponse(content="other answer"),
                (
                    Message(role="user", content="other"),
                    Message(role="assistant", content="other answer"),
                ),
                workspace=workspace,
            )
            store.append_turn(
                "session-1",
                LLMRequest(
                    system_prompt="Be helpful.",
                    messages=(*first_items, second_items[0]),
                ),
                LLMResponse(content="You're welcome."),
                second_items,
                workspace=workspace,
            )

            session = store.load("session-1")

            self.assertEqual(
                session.items,
                [*first_items, *second_items],
            )
            group = workspace_key(workspace)
            self.assertEqual(
                sorted(
                    path.relative_to(sessions_directory).as_posix()
                    for path in sessions_directory.rglob("*.jsonl")
                ),
                [
                    f"{group}/session-1/session-1.jsonl",
                    f"{group}/session-2/session-2.jsonl",
                ],
            )

    def test_loads_missing_session_without_creating_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sessions_directory = Path(directory) / "sessions"
            store = JsonlSessionStore(sessions_directory)

            session = store.load("session-1")

            self.assertEqual(session.session_id, "session-1")
            self.assertEqual(session.items, [])
            self.assertFalse(sessions_directory.exists())

    def test_rejects_session_id_that_can_escape_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = JsonlSessionStore(Path(directory) / "sessions")

            with self.assertRaises(ValueError):
                store.load("../session-1")


if __name__ == "__main__":
    unittest.main()
