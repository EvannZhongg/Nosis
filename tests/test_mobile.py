import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4
from unittest.mock import patch

from agent_core import JsonlSessionStore, Workspace
from interfaces.bridge.event_stream import EventStream
from interfaces.bridge.protocol import runtime_state_message
from interfaces.mobile.bindings import Bindings
from interfaces.mobile.config import load_channels
from interfaces.mobile.controller import MobileController
from interfaces.mobile.feishu import render_card
from interfaces.mobile.views import Button, Card
from interfaces.session_host import ActiveSessionRegistry


class Transport:
    def __init__(self):
        self.messages = {}
        self.updates = []
        self.texts = {}
        self.text_updates = []

    async def send(self, chat_id, card):
        message_id = f"m{len(self.messages)}"
        self.messages[message_id] = (chat_id, card)
        return message_id

    async def update(self, message_id, card):
        self.updates.append((message_id, card))
        self.messages[message_id] = (self.messages[message_id][0], card)

    async def send_text(self, chat_id, text):
        message_id = f"text{len(self.texts)}"
        self.texts[message_id] = (chat_id, text)
        return message_id

    async def update_text(self, message_id, text):
        self.text_updates.append((message_id, text))
        self.texts[message_id] = (self.texts[message_id][0], text)


class Channel:
    def __init__(self):
        self.sent = []
        self.queue = asyncio.Queue()
        self.stream = EventStream()
        self.state = runtime_state_message(
            phase="idle",
            approval=None,
            question=None,
            provider="model",
            permission_preset="ask_for_approval",
            context_window=None,
            cache_usage=None,
            jobs=[],
        )

    def emit(self, message):
        if message["type"] == "runtime_state":
            self.state.update(message)
            message = dict(self.state)
        self.queue.put_nowait(self.stream.publish(message))

    def send(self, message):
        self.sent.append(message)
        if message["type"] == "open_session":
            self.emit(dict(self.state, workspace=message["workspace"]))
        elif message["type"] == "user_turn":
            self.emit(dict(self.state, phase="running", turn_id=message["turn_id"]))

    async def read(self):
        return await self.queue.get()

    async def close(self):
        self.queue.put_nowait(None)


class Process:
    available = True

    def __init__(self):
        self.channels = []

    def open_channel(self, key):
        channel = Channel()
        self.channels.append(channel)
        return channel

    async def close(self):
        for channel in self.channels:
            await channel.close()


class MobileTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = Workspace(self.root)
        self.store = JsonlSessionStore(self.root / "sessions")
        self.host = ActiveSessionRegistry(self.store, self.workspace, self.root)
        self.process = Process()
        self.host._bridge = self.process
        self.transport = Transport()
        self.bindings = Bindings(self.root / "im_bindings.json")
        self.controller = MobileController(
            "feishu",
            ("ou_owner",),
            self.transport,
            self.host,
            self.store,
            self.bindings,
            self.workspace,
        )
        self.addAsyncCleanup(self.host.close_all)
        self.addAsyncCleanup(self.controller.close)

    async def text(self, text, user="ou_owner", chat="chat"):
        await self.controller.receive_text(chat, user, text, str(uuid4()))

    def conversation(self):
        return self.controller._conversation("chat", "ou_owner")

    async def click(self, button=0, message_id=None, user="ou_owner", chat="chat"):
        if message_id is None:
            message_id = next(reversed(self.transport.messages))
        card = self.transport.messages[message_id][1]
        await self.controller.receive_action(chat, user, message_id, card.buttons[button].token)

    async def open(self):
        await self.text("/workspaces")
        await self.click()
        await self.click()  # New Session button, since this workspace has no journals.
        return self.conversation(), self.process.channels[-1]

    async def settle(self):
        for _ in range(10):
            await asyncio.sleep(0)

    async def question(self, c, channel, *, free_text=True):
        request = {
            "type": "user_question",
            "request_id": "turn:q1",
            "turn_id": "turn",
            "question": "选择方案",
            "options": [{"id": "a", "label": "方案 A"}],
            "allow_free_text": free_text,
        }
        channel.emit(
            dict(channel.state, type="runtime_state", phase="waiting_user", question=request)
        )
        channel.emit(request)
        await self.settle()
        return request

    async def test_first_message_does_not_create_an_implicit_session(self):
        await self.text("你好")
        self.assertIsNone(self.conversation().session_id)
        self.assertEqual(self.process.channels, [])
        self.assertTrue(next(reversed(self.transport.messages.values()))[1].buttons)

    async def test_slash_shows_commands_and_button_opens_workspace_picker(self):
        await self.text("/")
        card = next(reversed(self.transport.messages.values()))[1]
        self.assertEqual(card.title, "可用命令")
        self.assertTrue(any(button.label.startswith("/cancel") for button in card.buttons))
        for command in ("/permissions", "/model", "/provider", "/refresh"):
            self.assertTrue(any(button.label.startswith(command) for button in card.buttons))
        await self.click(0)
        self.assertEqual(next(reversed(self.transport.messages.values()))[1].title, "选择工作区")

    async def test_permissions_model_and_refresh_reuse_existing_bridge_commands(self):
        c, channel = await self.open()
        await self.text("/permissions")
        await self.click(1)
        self.assertEqual(channel.sent[-1], {"type": "permission_set", "preset": "workspace_access"})
        await self.text("/model")
        request_id = channel.sent[-1]["request_id"]
        self.assertEqual(channel.sent[-1]["type"], "settings_get")
        channel.emit(
            {
                "type": "settings_snapshot",
                "request_id": request_id,
                "settings": {"providers": [{"id": "second", "model": "model-b"}]},
            }
        )
        await self.settle()
        await self.click()
        self.assertEqual(channel.sent[-1], {"type": "provider_set", "provider": "second"})
        await self.text("/refresh")
        self.assertEqual(channel.sent[-1], {"type": "runtime_refresh"})

    async def test_workspace_and_model_next_page_buttons(self):
        groups = [{"workspace": f"workspace-{index}"} for index in range(9)]
        with patch.object(self.store, "list_sessions", return_value=groups):
            await self.text("/workspaces")
            first_page = next(reversed(self.transport.messages.values()))[1]
            self.assertEqual(len(first_page.buttons), 9)
            self.assertEqual(first_page.buttons[-1].label, "下一页")
            await self.click(8)
        last_page = next(reversed(self.transport.messages.values()))[1]
        self.assertIn("第 2 页", last_page.text)
        self.assertEqual(
            [button.label for button in last_page.buttons], ["workspace-7", "workspace-8"]
        )

        c, channel = await self.open()
        providers = [{"id": f"p{index}", "model": "model"} for index in range(9)]
        await self.text("/model")
        for page in (1, 2):
            channel.emit(
                {
                    "type": "settings_snapshot",
                    "request_id": channel.sent[-1]["request_id"],
                    "settings": {"providers": providers},
                }
            )
            await self.settle()
            card = next(reversed(self.transport.messages.values()))[1]
            self.assertIn(f"第 {page} 页", card.text)
            if page == 1:
                self.assertEqual(len(card.buttons), 9)
                self.assertEqual(card.buttons[-1].label, "下一页")
                await self.click(8)
            else:
                self.assertEqual([button.label for button in card.buttons], ["p8 · model"])
                await self.click()
                self.assertEqual(channel.sent[-1], {"type": "provider_set", "provider": "p8"})

    async def test_pending_turn_can_be_cancelled_before_first_runtime_event(self):
        c, channel = await self.open()
        await self.text("开始")
        turn_id = channel.sent[-1]["turn_id"]
        await self.text("/cancel")
        self.assertEqual(channel.sent[-1], {"type": "cancel", "turn_id": turn_id})

    async def test_message_redelivery_is_deduplicated_across_restart(self):
        await self.controller.receive_text("chat", "ou_owner", "/help", "delivery-1")
        count = len(self.transport.messages)
        self.controller.bindings = Bindings(self.bindings.path)
        await self.controller.receive_text("chat", "ou_owner", "/help", "delivery-1")
        self.assertEqual(len(self.transport.messages), count)

    async def test_workspace_new_and_binding_survive_restart(self):
        c, channel = await self.open()
        self.assertEqual(channel.sent[0]["type"], "open_session")
        restored = Bindings(self.bindings.path).get(c.key)
        self.assertEqual(restored["session_id"], c.session_id)
        self.assertEqual(restored["workspace"], str(self.root.resolve()))

    async def test_unauthorized_and_forwarded_buttons_do_nothing(self):
        await self.text("/workspaces", user="ou_stranger")
        self.assertEqual(self.transport.messages, {})
        await self.text("/workspaces")
        card_id = next(reversed(self.transport.messages))
        token = self.transport.messages[card_id][1].buttons[0].token
        await self.controller.receive_action("other-chat", "ou_owner", card_id, token)
        self.assertIsNone(self.controller._conversation("other-chat", "ou_owner").workspace)
        self.assertEqual(self.process.channels, [])

    async def test_old_menu_cannot_change_selection(self):
        await self.text("/workspaces")
        card_id = next(reversed(self.transport.messages))
        token = self.transport.messages[card_id][1].buttons[0].token
        await self.text("/workspaces")
        await self.controller.receive_action("chat", "ou_owner", card_id, token)
        self.assertIsNone(self.conversation().workspace)

    async def test_question_button_uses_bridge_and_is_single_use(self):
        c, channel = await self.open()
        await self.question(c, channel)
        card_id = next(reversed(self.transport.messages))
        token = self.transport.messages[card_id][1].buttons[0].token
        await self.controller.receive_action("chat", "ou_owner", card_id, token)
        await self.controller.receive_action("chat", "ou_owner", card_id, token)
        responses = [
            message for message in channel.sent if message["type"] == "user_question_response"
        ]
        self.assertEqual(
            responses,
            [{"type": "user_question_response", "request_id": "turn:q1", "option_id": "a"}],
        )
        self.assertFalse(self.transport.messages[card_id][1].buttons)

    async def test_free_text_question_and_disallowed_free_text(self):
        c, channel = await self.open()
        await self.question(c, channel)
        await self.text("我自己的方案")
        self.assertEqual(channel.sent[-1]["text"], "我自己的方案")
        self.assertEqual(channel.sent[-1]["type"], "user_question_response")
        channel.emit(dict(channel.state, type="runtime_state", question=None, phase="running"))
        await self.settle()
        await self.question(c, channel, free_text=False)
        count = len(channel.sent)
        await self.text("不合法的自由文本")
        self.assertEqual(len(channel.sent), count)

    async def test_approval_cannot_be_granted_with_plain_text(self):
        c, channel = await self.open()
        request = {
            "type": "approval_request",
            "request_id": "turn:a1",
            "turn_id": "turn",
            "command": "write file",
            "kind": "file",
        }
        channel.emit(
            dict(channel.state, type="runtime_state", approval=request, phase="waiting_approval")
        )
        channel.emit(request)
        await self.settle()
        card_id = next(reversed(self.transport.messages))
        await self.text("允许")
        self.assertFalse(any(message["type"] == "approval_response" for message in channel.sent))
        await self.click(1, card_id)
        self.assertEqual(
            channel.sent[-1],
            {"type": "approval_response", "request_id": "turn:a1", "approved": False},
        )

    async def test_expired_request_cannot_answer_a_new_question(self):
        c, channel = await self.open()
        await self.question(c, channel)
        card_id = next(reversed(self.transport.messages))
        token = self.transport.messages[card_id][1].buttons[0].token
        channel.emit(dict(channel.state, type="runtime_state", question=None, phase="running"))
        await self.settle()
        await self.controller.receive_action("chat", "ou_owner", card_id, token)
        self.assertFalse(
            any(message["type"] == "user_question_response" for message in channel.sent)
        )

    async def test_running_text_is_steer_and_cancel_keeps_turn_id(self):
        c, channel = await self.open()
        await self.text("开始工作")
        await self.settle()
        turn_id = channel.sent[-1]["turn_id"]
        await self.text("补充要求")
        self.assertEqual(channel.sent[-1]["type"], "user_steer")
        self.assertEqual(channel.sent[-1]["turn_id"], turn_id)
        await self.text("/cancel")
        self.assertEqual(channel.sent[-1], {"type": "cancel", "turn_id": turn_id})

    async def test_running_session_cannot_be_replaced(self):
        c, channel = await self.open()
        await self.text("开始工作")
        session_id = c.session_id
        await self.text("/new")
        self.assertEqual(c.session_id, session_id)
        self.assertEqual(len(self.process.channels), 1)

    async def test_gui_takeover_revokes_mobile_and_explicit_takeover_restores_it(self):
        c, channel = await self.open()
        runtime = c.runtime
        attachment = await asyncio.wait_for(runtime.attach(0, "gui", takeover=True), 2)
        queue = attachment[0]
        await self.settle()
        count = len(channel.sent)
        await self.text("不应执行")
        self.assertEqual(len(channel.sent), count)

        async def gui_detach():
            while (await queue.get()) is not None:
                pass
            await runtime.detach(queue, "gui")

        task = asyncio.create_task(gui_detach())
        await asyncio.wait_for(self.text("/takeover"), 2)
        await task
        self.assertTrue(runtime.owns_attachment(c.attachment_id))

    async def test_reconnect_renders_pending_question_from_runtime_snapshot(self):
        c, channel = await self.open()
        await self.question(c, channel)
        await self.controller._detach(c)
        await self.controller._open(c, c.session_id)
        self.assertEqual(next(reversed(self.transport.messages.values()))[1].title, "请做选择")

    async def test_terminal_event_updates_progress_and_delivers_assistant_output(self):
        c, channel = await self.open()
        await self.text("工作")
        progress = c.progress
        channel.emit(
            {
                "type": "assistant_message",
                "content": "完成内容",
                "turn_id": "turn",
                "model_call_index": 0,
            }
        )
        channel.emit({"type": "turn_completed"})
        await self.settle()
        self.assertTrue(any(text == "完成内容" for _, text in self.transport.texts.values()))
        self.assertFalse(
            any(card.text == "完成内容" for _, card in self.transport.messages.values())
        )
        self.assertEqual(self.transport.messages[progress][1].title, "处理完成")

    async def test_assistant_deltas_stream_as_text_without_reasoning_or_duplicate_final_message(
        self,
    ):
        c, channel = await self.open()
        channel.emit(
            {
                "type": "reasoning_delta",
                "text": "private reasoning",
                "turn_id": "turn",
                "model_call_index": 0,
            }
        )
        channel.emit(
            {"type": "assistant_delta", "text": "第一段", "turn_id": "turn", "model_call_index": 0}
        )
        await self.settle()
        self.assertEqual(list(self.transport.texts.values()), [("chat", "第一段")])
        channel.emit(
            {"type": "assistant_delta", "text": "第二段", "turn_id": "turn", "model_call_index": 0}
        )
        channel.emit(
            {
                "type": "assistant_message",
                "content": "第一段第二段",
                "turn_id": "turn",
                "model_call_index": 0,
            }
        )
        await self.settle()
        self.assertEqual(list(self.transport.texts.values()), [("chat", "第一段第二段")])
        self.assertEqual(len(self.transport.texts), 1)
        self.assertNotIn("private reasoning", str(self.transport.texts))


class ConfigurationTest(unittest.TestCase):
    def test_dotenv_overrides_process_environment_without_interpolation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "im_channels.json").write_text(
                json.dumps(
                    {
                        "channels": [
                            {
                                "type": "feishu",
                                "id": "personal",
                                "app_id": "${APP_ID}",
                                "app_secret": "${SECRET}",
                                "allowed_users": ["ou_owner"],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (root / ".env").write_text(
                "APP_ID=dotenv-app\nSECRET=${OTHER}\nOTHER=expanded-secret\n",
                encoding="utf-8",
            )
            with patch.dict("os.environ", {"APP_ID": "process-app", "SECRET": "process-secret"}):
                config = load_channels(root)[0]
            self.assertEqual(config.app_id, "dotenv-app")
            self.assertEqual(config.app_secret, "${OTHER}")

            (root / ".env").write_text("SECRET=\n", encoding="utf-8")
            with patch.dict("os.environ", {"APP_ID": "process-app", "SECRET": "process-secret"}):
                with self.assertRaisesRegex(ValueError, "app_secret must be a non-empty string"):
                    load_channels(root)

    def test_credentials_support_env_and_never_appear_in_repr(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "im_channels.json").write_text(
                json.dumps(
                    {
                        "channels": [
                            {
                                "type": "feishu",
                                "id": "personal",
                                "app_id": "cli_test",
                                "app_secret": "${FEISHU_SECRET}",
                                "allowed_users": ["ou_owner"],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict("os.environ", {"FEISHU_SECRET": "secret-value"}):
                configs = load_channels(root)
            self.assertEqual(configs[0].app_secret, "secret-value")
            self.assertNotIn("secret-value", repr(configs[0]))
            self.assertEqual(configs[0].allowed_users, ("ou_owner",))

    def test_missing_config_is_optional(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(load_channels(Path(directory)), [])

    def test_card_uses_opaque_callback_values(self):
        card = render_card(Card("Question", "Text", [Button("Answer", "opaque-token")]))
        self.assertEqual(card["elements"][1]["actions"][0]["value"], {"token": "opaque-token"})
        self.assertTrue(card["config"]["update_multi"])
