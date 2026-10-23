import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_core import (
    FilePart, JsonlSessionStore, LLMResponse, Message, Session, ToolCall,
)
from agent_core.llm import LLMProvider
from agent_core.content import text_content
from agent_core.session_paths import session_log_path
from agent_core.session_title import generate_session_title, initial_session_title
from agent_runtime.plane_manager import ExecutionPlaneManager
from interfaces.bridge.bridge import Bridge
from tests.test_agent import EchoTool, make_agent
from tests.test_bridge import emitted, open_session_message


class TitleProvider(LLMProvider):
    def __init__(self, response="会话标题", *, blocked=False):
        self.response = response
        self.requests = []
        self.started = threading.Event()
        self.release = threading.Event()
        if not blocked:
            self.release.set()

    @property
    def model_name(self):
        return "title-model"

    @property
    def max_context_tokens(self):
        return 4096

    def count_input_tokens(self, request):
        return (len(request.system_prompt) + sum(len(text_content(message.content) or "") for message in request.messages)) // 4

    def stream(self, request, on_text_delta, on_reasoning_delta=None):
        self.requests.append(request)
        self.started.set()
        if not self.release.wait(5):
            raise TimeoutError("test provider did not get released")
        if isinstance(self.response, Exception):
            raise self.response
        return LLMResponse(content=self.response)


class SessionTitleTest(unittest.TestCase):
    def test_tool_only_assistant_message_can_generate_a_title(self):
        provider = TitleProvider()
        calls = []
        title = generate_session_title(
            provider, "{{conversation}}",
            (
                Message("user", "检查加载逻辑"),
                Message("assistant", None, tool_calls=(ToolCall("c", "read_file", {"path": "app.py"}),)),
            ), lambda: None, calls.append,
        )
        conversation = json.loads(provider.requests[0].messages[0].content)
        self.assertEqual(title, "会话标题")
        self.assertEqual(conversation[1]["text"], "")
        self.assertEqual(conversation[1]["tools"], ["read_file"])
        self.assertEqual(calls[0].status, "completed")

    def test_initial_title_handles_messages_without_text(self):
        self.assertEqual(initial_session_title(Message("user", None), "s"), "s")
        self.assertEqual(initial_session_title(Message("user", (
            FilePart(path="brief.pdf", filename="brief.pdf", mime_type="application/pdf", size_bytes=24),
        )), "s"), "brief.pdf")

    def test_request_is_bounded_and_excludes_tool_output_and_reasoning(self):
        provider = TitleProvider()
        calls = []
        title = generate_session_title(
            provider, "Name {{conversation}}",
            (
                Message("user", "分析 GUI " * 2000),
                Message("assistant", "检查加载逻辑", reasoning="private reasoning"),
                Message("tool", "private tool output"),
            ), lambda: None, calls.append,
        )
        request = provider.requests[0]
        self.assertEqual(title, "会话标题")
        self.assertEqual(len(request.messages), 1)
        self.assertEqual(request.messages[0].role, "user")
        self.assertEqual(request.tools, ())
        self.assertEqual(request.max_generation_tokens, 256)
        self.assertLess(len(request.messages[0].content), 8100)
        self.assertNotIn("private", request.messages[0].content)
        self.assertNotIn("{{conversation}}", request.messages[0].content)
        self.assertEqual(calls[0].task, "session_title")
        self.assertEqual(calls[0].status, "completed")

    def test_invalid_title_is_recorded_without_retry(self):
        for response in ("", "x" * 81, RuntimeError("model failed")):
            with self.subTest(response=response):
                provider = TitleProvider(response)
                calls = []
                with self.assertRaises((ValueError, RuntimeError)):
                    generate_session_title(
                        provider, "{{conversation}}", (Message("user", "hello"),),
                        lambda: None, calls.append,
                    )
                self.assertEqual(len(provider.requests), 1)
                self.assertIn(calls[0].status, {"failed", "invalid_response"})

    def test_title_is_persisted_and_list_never_opens_the_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = JsonlSessionStore(root / "sessions")
            session = Session("s")
            store.bind_workspace("s", root)
            with store.open_journal("s", root) as sink:
                session.attach_journal_sink(sink)
                session.begin_turn("t")
                session.add_item("user", "首条消息 " * 50)
                session.add_item("user", "后续消息")
            metadata_path = session_log_path(store.directory, root, "s").parent / "session.json"
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(metadata["title_source"], "initial")
            self.assertEqual(len(metadata["title"]), 80)
            self.assertTrue(metadata["has_activity"])
            self.assertTrue(store.set_title("s", "手动名称", "manual"))
            self.assertFalse(store.set_title("s", "自动名称", "generated", expected_source="initial"))
            original_open = Path.open

            def checked_open(path, *args, **kwargs):
                if path.suffix == ".jsonl":
                    raise AssertionError("listing opened a Journal")
                return original_open(path, *args, **kwargs)

            with patch.object(Path, "open", checked_open):
                self.assertEqual(store.list_sessions()[0]["sessions"][0]["title"], "手动名称")
                self.assertEqual(store.list_workspace_sessions(root)[0]["title"], "手动名称")


class SessionTitleRuntimeTest(unittest.TestCase):
    def run_title_turn(self, *, enabled=True, manual=False, cancel=False, failure=False):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        opening = open_session_message(root, agent_config={
            "max_same_tool_calls": 5, "output_reserve_tokens": 100,
            "workspace_instruction_files": [], "main_agent": {"tools": {}},
            "memory": {"enabled": False}, "mcp": {"enabled": False},
            "generate_session_title": enabled,
        }, provider_config={
            "main_agent": {"provider": "main"}, "fast_model": {"provider": "fast"},
            "providers": {"main": {"model": "openai/main"}, "fast": {"model": "openai/fast"}},
        })
        output = io.StringIO()
        with patch("interfaces.bridge.bridge.default_config_directory", return_value=root):
            bridge = Bridge(io.StringIO(), output, start_scheduler=False)
        self.addCleanup(bridge.close)
        bridge.open_session(opening)
        fast = TitleProvider(RuntimeError("unavailable") if failure else "GUI 加载优化", blocked=True)
        second_started = threading.Event()
        finish_main = threading.Event()

        class MainProvider(TitleProvider):
            def stream(self, request, on_text_delta, on_reasoning_delta=None):
                self.requests.append(request)
                if len(self.requests) == 1:
                    return LLMResponse(content=None, tool_calls=(ToolCall("c", "echo", {"text": "ok"}),))
                second_started.set()
                if not finish_main.wait(5):
                    raise TimeoutError("main provider did not get released")
                return LLMResponse(content="完成")

        main = MainProvider()
        with patch.object(ExecutionPlaneManager, "_provider_for", side_effect=lambda config, *_:
                          None if config is None else fast if config.model == "openai/fast" else main):
            plane = bridge.host.planes.ensure(bridge.host.sessions)
        self.assertIs(plane.fast_provider, fast)
        self.assertIsNone(plane.memory)
        session = bridge.host.sessions.session
        plane.agent = make_agent(provider=main, session=session, tools=(EchoTool(),))
        # The plane still executes through RuntimeHost.publish_event.
        worker = threading.Thread(target=lambda: bridge.run_turn({"turn_id": "t", "text": "分析 GUI 加载"}))
        worker.start()
        try:
            self.assertTrue(second_started.wait(3))
            if enabled:
                self.assertTrue(fast.started.wait(3))
                self.assertTrue(worker.is_alive())
                self.assertEqual(session.items[-1].role, "tool")
                if manual:
                    bridge.host.sessions.store.set_title(session.session_id, "我的名称", "manual")
                if cancel:
                    plane.title_task._control.cancel()
                if failure:
                    with self.assertLogs("agent_runtime.session_title", level="ERROR"):
                        fast.release.set()
                        plane.title_task._thread.join(3)
                else:
                    fast.release.set()
                    plane.title_task._thread.join(3)
                self.assertFalse(plane.title_task._thread.is_alive())
                metadata_path = session_log_path(root / "sessions", root, session.session_id).parent / "session.json"
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                expected_source = "manual" if manual else "initial" if cancel or failure else "generated"
                self.assertEqual(metadata["title_source"], expected_source)
                updates = [message for message in emitted(output) if message["type"] == "session_title_updated"]
                self.assertEqual(len(updates), int(not (manual or cancel or failure)))
                self.assertFalse(any(message["type"] == "turn_completed" for message in emitted(output)))
                self.assertEqual(len(fast.requests), 1)
            else:
                self.assertIsNone(plane.title_task)
                self.assertFalse(fast.requests)
        finally:
            fast.release.set()
            finish_main.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(emitted(output)[-1]["type"], "turn_completed")
        self.assertEqual(len([event for event in session.journal if event.event_type == "session_title_completed"]), int(enabled))
        if enabled:
            call = next(event for event in session.journal if event.event_type == "session_title_completed")
            self.assertEqual(call.turn_id, "t")
        self.assertEqual(len(fast.requests), int(enabled))

    def test_first_unit_names_session_while_the_turn_continues_with_memory_disabled(self):
        self.run_title_turn()

    def test_generation_can_be_disabled(self):
        self.run_title_turn(enabled=False)

    def test_manual_title_wins_over_pending_generation(self):
        self.run_title_turn(manual=True)

    def test_cancelled_naming_preserves_initial_title(self):
        self.run_title_turn(cancel=True)

    def test_failed_naming_does_not_fail_or_retry_the_main_turn(self):
        self.run_title_turn(failure=True)
