"""Mobile commands and presentation over the shared frontend Session Host."""

import asyncio
import json
import logging
import secrets
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from agent_core import JsonlSessionStore, Workspace
from agent_runtime.attachments import store_attachment
from interfaces.session_host import ActiveSession, ActiveSessionRegistry

from .bindings import Bindings
from .views import Button, Card, Transport
from .streaming import TextStream

log = logging.getLogger(__name__)
HELP = (
    "先用 /workspaces 选择工作区，再用 /new 新建或 /sessions 恢复会话。\n"
    "/status 查看状态 · /cancel 停止执行 · /takeover 接管当前会话\n"
    "支持发送图片、文件和图文消息；执行期间文字可作为补充指令，附件请在任务结束后发送。\n"
    "提问期间可点选项或直接回复文字。"
)
COMMANDS = [
    ("/workspaces", "选择工作区"),
    ("/sessions", "选择会话"),
    ("/new", "新建会话"),
    ("/permissions", "会话权限"),
    ("/model", "切换模型"),
    ("/provider", "配置概览"),
    ("/refresh", "刷新运行环境"),
    ("/status", "当前状态"),
    ("/cancel", "停止执行"),
    ("/takeover", "接管会话"),
    ("/help", "使用帮助"),
]
PERMISSIONS = {
    "ask_for_approval": "逐项审批",
    "workspace_access": "工作区访问",
    "full_access": "完全访问",
}
PHASES = {
    "inactive": "待启动",
    "starting": "准备中",
    "idle": "空闲",
    "running": "执行中",
    "waiting_approval": "等待审批",
    "waiting_user": "等待回复",
    "failed": "失败",
}


def _page_actions(
    actions: list[dict],
    command: str,
    page_text: str,
) -> tuple[int, list[dict]]:
    page = int(page_text) if page_text else 1
    if page < 1:
        raise ValueError("页码必须大于 0。")
    visible = actions[(page - 1) * 8 : page * 8]
    if page * 8 < len(actions):
        visible.append(
            {
                "kind": "command",
                "value": f"{command} {page + 1}",
                "label": "下一页",
            }
        )
    return page, visible


@dataclass
class Conversation:
    key: str
    chat_id: str
    user_id: str
    workspace: str | None = None
    session_id: str | None = None
    runtime: ActiveSession | None = None
    queue: asyncio.Queue | None = None
    reader: asyncio.Task | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    actions: dict[str, tuple[dict, str | None]] = field(default_factory=dict)
    interactions: dict[str, str] = field(default_factory=dict)
    progress: str | None = None
    settings_requests: dict[str, tuple[str, str]] = field(default_factory=dict)
    streams: dict[tuple[str, int], TextStream] = field(default_factory=dict)
    attachment_id: str = field(default_factory=lambda: f"im:{uuid4()}")


class MobileController:
    def __init__(
        self,
        channel_id: str,
        allowed_users: tuple[str, ...],
        transport: Transport,
        host: ActiveSessionRegistry,
        store: JsonlSessionStore,
        bindings: Bindings,
        workspace: Workspace,
    ) -> None:
        self.channel_id = channel_id
        self.allowed_users = allowed_users
        self.transport = transport
        self.host = host
        self.store = store
        self.bindings = bindings
        self.workspace = str(workspace.path)
        self.conversations: dict[str, Conversation] = {}

    def _conversation(self, chat_id: str, user_id: str) -> Conversation:
        key = json.dumps([self.channel_id, chat_id, user_id])
        if key not in self.conversations:
            saved = self.bindings.get(key)
            self.conversations[key] = Conversation(
                key,
                chat_id,
                user_id,
                saved.get("workspace"),
                saved.get("session_id"),
            )
        return self.conversations[key]

    def authorized(self, user_id: str) -> bool:
        if user_id in self.allowed_users:
            return True
        log.warning("Rejected Feishu identity: channel=%s open_id=%s", self.channel_id, user_id)
        return False

    async def receive_message(
        self, chat_id: str, user_id: str, text: str, message_id: str,
        attachments: list[dict] | tuple = (),
    ) -> None:
        if not self.authorized(user_id):
            return
        conversation = self._conversation(chat_id, user_id)
        async with conversation.lock:
            if not self.bindings.accept(self.channel_id, message_id):
                return
            try:
                if attachments:
                    await self._input(conversation, text.strip(), attachments, message_id)
                else:
                    await self._text(conversation, text.strip())
            except (ValueError, OSError, RuntimeError) as error:
                await self._show(conversation, "无法完成操作", str(error))

    async def receive_action(self, chat_id: str, user_id: str, message_id: str, token: str) -> None:
        if not self.authorized(user_id):
            return
        conversation = self._conversation(chat_id, user_id)
        async with conversation.lock:
            entry = conversation.actions.get(token)
            if entry is None or entry[1] != message_id:
                await self._show(conversation, "操作已失效", "请使用命令重新打开，或查看当前提问。")
                return
            action = entry[0]
            try:
                await self._action(conversation, action)
            except (ValueError, OSError, RuntimeError) as error:
                await self._show(conversation, "无法完成操作", str(error))
                return
            conversation.actions = {
                key: value for key, value in conversation.actions.items() if value[1] != message_id
            }
            await self.transport.update(
                message_id, Card("已处理", action.get("label", "操作已提交"))
            )

    async def _show(
        self, c: Conversation, title: str, text: str, actions: list[dict] | None = None
    ) -> str:
        buttons = []
        tokens = []
        for action in actions or []:
            token = secrets.token_urlsafe(24)
            tokens.append(token)
            c.actions[token] = (action, None)
            buttons.append(Button(action["label"], token))
        message_id = await self.transport.send(c.chat_id, Card(title, text, buttons))
        for token in tokens:
            c.actions[token] = (c.actions[token][0], message_id)
        return message_id

    def _save(self, c: Conversation) -> None:
        self.bindings.save(c.key, c.workspace, c.session_id)

    def _assert_idle(self, c: Conversation) -> None:
        if c.runtime is not None and c.runtime.running:
            raise ValueError("当前会话正在执行，请先 /cancel 或等待完成。")

    async def _text(self, c: Conversation, text: str) -> None:
        if not text:
            return
        command, _, argument = text.partition(" ")
        if text == "/":
            c.actions = {
                key: value
                for key, value in c.actions.items()
                if value[0]["kind"] in {"approval", "question"}
            }
            await self._show(
                c,
                "可用命令",
                "点击执行命令，也可以直接发送命令文字。",
                [
                    {"kind": "command", "value": value, "label": f"{value} · {label}"}
                    for value, label in COMMANDS
                ],
            )
        elif command in {"/help", "/start"}:
            await self._show(
                c,
                "Nosis",
                HELP + "\n\n" + "\n".join(f"{value}：{label}" for value, label in COMMANDS),
            )
        elif command == "/workspaces":
            workspaces = list(
                dict.fromkeys(
                    [
                        self.workspace,
                        *[str(group["workspace"]) for group in self.store.list_sessions()],
                    ]
                )
            )
            await self._menu(c, "选择工作区", workspaces, "workspace", argument)
        elif command == "/sessions":
            if c.workspace is None:
                await self._text(c, "/workspaces")
                return
            sessions = self.store.list_workspace_sessions(c.workspace)
            await self._menu(c, "选择会话", sessions, "session", argument)
        elif command == "/new":
            self._assert_idle(c)
            if c.workspace is None:
                await self._text(c, "/workspaces")
                return
            await self._open(c, str(uuid4()))
            await self._show(c, "新会话已就绪", self._status(c))
        elif command == "/permissions":
            runtime = await self._attached(c)
            await self._show(
                c,
                "当前会话权限",
                f"当前：{PERMISSIONS[runtime.state['permission_preset']]}\n完全访问允许在宿主能力范围内执行操作。",
                [
                    {
                        "kind": "permission",
                        "value": value,
                        "label": label,
                        "session_id": c.session_id,
                    }
                    for value, label in PERMISSIONS.items()
                ],
            )
        elif command in {"/model", "/provider"}:
            if command == "/model":
                self._assert_idle(c)
            runtime = await self._attached(c)
            request_id = str(uuid4())
            c.settings_requests[request_id] = (command, argument)
            runtime.send({"type": "settings_get", "request_id": request_id})
        elif command == "/refresh":
            self._assert_idle(c)
            runtime = await self._attached(c)
            runtime.send({"type": "runtime_refresh"})
            await self._show(c, "正在刷新运行环境", "完成后会通知你。")
        elif command == "/status":
            await self._show(c, "当前会话", self._status(c))
        elif command == "/takeover":
            if c.session_id is None:
                raise ValueError("请先选择会话。")
            await self._open(c, c.session_id, takeover=True)
            await self._show(c, "已接管会话", self._status(c))
        elif command == "/cancel":
            runtime = await self._attached(c)
            if runtime.turn_id is None:
                await self._show(c, "当前会话", "没有正在执行的任务。")
            else:
                runtime.send({"type": "cancel", "turn_id": runtime.turn_id})
        elif command.startswith("/"):
            await self._text(c, "/")
        else:
            await self._input(c, text)

    async def _input(
        self, c: Conversation, text: str,
        attachments: list[dict] | tuple = (), message_id: str = "",
    ) -> None:
        if c.session_id is None:
            await self._show(c, "先选择会话", HELP)
            await self._text(c, "/workspaces" if c.workspace is None else "/sessions")
            return
        runtime = await self._attached(c)
        if attachments and runtime.running:
            raise ValueError("当前会话正在执行，附件请在任务结束后重新发送；提问请回复文字或点击选项。")
        question = runtime.state["question"]
        if question is not None:
            if not question["allow_free_text"]:
                raise ValueError("请点击当前提问卡片中的选项。")
            runtime.send({
                "type": "user_question_response",
                "request_id": question["request_id"], "text": text,
            })
            await self._resolve(c, question["request_id"], text)
        elif runtime.state["approval"] is not None:
            raise ValueError("请使用审批卡片中的允许或拒绝按钮。")
        elif runtime.running:
            runtime.send({
                "type": "user_steer", "turn_id": runtime.turn_id,
                "steer_id": str(uuid4()), "text": text,
            })
        else:
            workspace = Workspace(Path(runtime.state["workspace"]))
            stored = []
            try:
                if attachments:
                    with tempfile.TemporaryDirectory(prefix="nosis-feishu-") as directory:
                        for index, attachment in enumerate(attachments):
                            path = Path(directory) / str(index)
                            await self.transport.download_attachment(message_id, attachment, path)
                            with path.open("rb") as source:
                                stored.append(store_attachment(
                                    source, workspace, attachment["filename"],
                                ))
                if not runtime.owns_attachment(c.attachment_id):
                    raise ValueError("会话已由其他端接管。可使用 /takeover 接管。")
                c.progress = await self._show(c, "Nosis 正在处理", self._status(c))
                if not runtime.owns_attachment(c.attachment_id):
                    raise ValueError("会话已由其他端接管。可使用 /takeover 接管。")
                runtime.send({
                    "type": "user_turn", "turn_id": str(uuid4()), "text": text,
                    **({"attachments": stored} if stored else {}),
                })
            except BaseException:
                for attachment in stored:
                    workspace.resolve_path(attachment["path"]).unlink(missing_ok=True)
                raise

    async def _menu(
        self, c: Conversation, title: str, entries: list, kind: str, page_text: str
    ) -> None:
        c.actions = {
            key: value
            for key, value in c.actions.items()
            if value[0]["kind"] in {"approval", "question"}
        }
        actions = []
        for entry in entries:
            label = entry if isinstance(entry, str) else entry["title"]
            value = entry if isinstance(entry, str) else entry["session_id"]
            actions.append({"kind": kind, "value": value, "label": label, "workspace": c.workspace})
        command = "/workspaces" if kind == "workspace" else "/sessions"
        page, actions = _page_actions(actions, command, page_text)
        if kind == "session":
            actions.append({"kind": "command", "value": "/new", "label": "新建会话"})
        await self._show(c, title, f"第 {page} 页 · {len(entries)} 项", actions)

    async def _action(self, c: Conversation, action: dict) -> None:
        kind = action["kind"]
        if kind == "command":
            await self._text(c, action["value"])
        elif kind == "workspace":
            self._assert_idle(c)
            await self._detach(c)
            c.workspace, c.session_id = action["value"], None
            c.actions.clear()
            self._save(c)
            await self._text(c, "/sessions")
        elif kind == "session":
            self._assert_idle(c)
            if c.workspace != action["workspace"]:
                raise ValueError("工作区已切换，请重新选择会话。")
            await self._open(c, action["value"])
            await self._show(c, "会话已就绪", self._status(c))
        elif kind in {"permission", "model"}:
            runtime = await self._attached(c)
            if action["session_id"] != c.session_id:
                raise ValueError("会话已切换，请重新打开命令菜单。")
            if kind == "permission":
                runtime.send({"type": "permission_set", "preset": action["value"]})
            else:
                self._assert_idle(c)
                runtime.send({"type": "provider_set", "provider": action["value"]})
        elif kind in {"approval", "question"}:
            runtime = await self._attached(c)
            field_name = "approval" if kind == "approval" else "question"
            request = runtime.state[field_name]
            if (
                request is None
                or request["request_id"] != action["request_id"]
                or c.session_id != action["session_id"]
            ):
                raise ValueError("该请求已结束，请查看当前状态。")
            runtime.send(
                {
                    "type": "approval_response" if kind == "approval" else "user_question_response",
                    "request_id": request["request_id"],
                    **action["response"],
                }
            )

    async def _attached(self, c: Conversation) -> ActiveSession:
        if c.runtime is None or c.runtime.done or (c.reader is not None and c.reader.done()):
            if c.session_id is None:
                raise ValueError("请先选择会话。")
            await self._open(c, c.session_id)
        if not c.runtime.owns_attachment(c.attachment_id):
            raise ValueError("该会话已由其他端控制。使用 /takeover 显式接管。")
        return c.runtime

    async def _open(self, c: Conversation, session_id: str, *, takeover: bool = False) -> None:
        bound = self.store.workspace_for(session_id)
        workspace = Workspace(Path(bound or c.workspace))
        runtime, created = await self.host.open_or_attach(
            {
                "type": "open_session",
                "session_id": session_id,
                "workspace": str(workspace.path),
                "provider": None,
            },
            workspace,
            attach_only=False,
        )
        # Detach our old subscription before reattaching to the same Session.
        await self._detach(c)
        attachment = await runtime.attach(0, c.attachment_id, takeover=takeover)
        if attachment is None:
            c.workspace, c.session_id = str(workspace.path), session_id
            self._save(c)
            raise ValueError("会话由其他端控制。可使用 /takeover 接管。")
        queue, events, state = attachment
        c.workspace, c.session_id = str(workspace.path), session_id
        c.runtime, c.queue = runtime, queue
        c.actions.clear()
        self._save(c)
        failures = [event for event in events if event["type"] in {"session_open_failed", "fatal"}]
        if failures:
            runtime.mark_failure_delivered()
            await self._detach(c)
            raise RuntimeError(failures[-1]["error"]["message"])
        c.reader = asyncio.create_task(self._read(c, runtime, queue))
        for field_name in ("approval", "question"):
            request = state.get(field_name)
            if request is not None:
                await self._interaction(c, request)

    async def _detach(self, c: Conversation) -> None:
        if c.runtime is not None and c.queue is not None:
            await c.runtime.detach(c.queue, c.attachment_id)
        if c.reader is not None and c.reader is not asyncio.current_task():
            c.reader.cancel()
            await asyncio.gather(c.reader, return_exceptions=True)
        c.interactions.clear()
        c.actions.clear()
        c.settings_requests.clear()
        c.runtime = c.queue = c.reader = None

    def _status(self, c: Conversation) -> str:
        state = c.runtime.state if c.runtime is not None else {}
        return (
            f"工作区：{c.workspace or '未选择'}\n会话：{c.session_id or '未选择'}\n"
            f"状态：{PHASES[state.get('phase', 'inactive')]}\n模型：{state.get('provider') or '默认'}\n"
            f"权限：{PERMISSIONS.get(state.get('permission_preset'), '随会话恢复')}"
        )

    async def _interaction(self, c: Conversation, request: dict) -> None:
        request_id = request["request_id"]
        if request_id in c.interactions:
            return
        approval = request["type"] == "approval_request"
        options = (
            [
                {"label": "允许本次", "response": {"approved": True}},
                {"label": "拒绝", "response": {"approved": False}},
            ]
            if approval
            else [
                {"label": option["label"], "response": {"option_id": option["id"]}}
                for option in request["options"]
            ]
        )
        actions = [
            {
                "kind": "approval" if approval else "question",
                "session_id": c.session_id,
                "request_id": request_id,
                **option,
            }
            for option in options
        ]
        text = request["command"] if approval else request["question"]
        if not approval:
            descriptions = [
                f"{option['label']}：{option['description']}"
                for option in request["options"]
                if option.get("description")
            ]
            text += "\n" + "\n".join(descriptions)
            if request["allow_free_text"]:
                text += "\n也可以直接发送文字回复。"
        c.interactions[request_id] = await self._show(
            c, "需要执行许可" if approval else "请做选择", text, actions
        )

    async def _resolve(self, c: Conversation, request_id: str, text: str) -> None:
        message_id = c.interactions.pop(request_id, None)
        if message_id is not None:
            c.actions = {key: value for key, value in c.actions.items() if value[1] != message_id}
            await self.transport.update(message_id, Card("请求已结束", text))

    async def _settings(self, c: Conversation, event: dict) -> None:
        request = c.settings_requests.pop(event.get("request_id"), None)
        if request is None or not c.runtime.owns_attachment(c.attachment_id):
            return
        if event["type"] == "settings_update_failed":
            await self._show(c, "读取配置失败", event["error"]["message"])
            return
        command, argument = request
        settings = event["settings"]
        providers = settings["providers"]
        if command == "/model":
            actions = [
                {
                    "kind": "model",
                    "session_id": c.session_id,
                    "value": item["id"],
                    "label": f"{item['id']} · {item['model']}",
                }
                for item in providers
            ]
            page, actions = _page_actions(actions, command, argument)
            await self._show(
                c, "选择会话模型", f"当前：{c.runtime.state['provider']} · 第 {page} 页", actions
            )
        else:
            text = "\n".join(f"{item['id']}：{item['model']}" for item in providers)
            for name, field_name in (
                ("Skills", "skills"),
                ("Plugins", "plugins"),
                ("MCP", "mcp_servers"),
            ):
                items = settings.get(field_name, [])
                text += f"\n\n{name}：" + (
                    "、".join(str(item.get("name") or item.get("id")) for item in items) or "无"
                )
            await self._show(c, "配置概览", text[:10000])

    async def _read(self, c: Conversation, runtime: ActiveSession, queue: asyncio.Queue) -> None:
        try:
            while (event := await queue.get()) is not None:
                kind = event["type"]
                if kind in {"settings_snapshot", "settings_update_failed"}:
                    await self._settings(c, event)
                elif kind in {"permission_changed", "provider_changed", "runtime_refreshed"}:
                    await self._show(c, "运行设置已更新", self._status(c))
                elif kind == "runtime_refresh_failed":
                    await self._show(c, "刷新失败", event["error"]["message"])
                elif kind in {"approval_request", "user_question"}:
                    await self._interaction(c, event)
                elif kind in {"assistant_delta", "assistant_message"}:
                    key = (event["turn_id"], event["model_call_index"])
                    stream = c.streams.get(key)
                    if stream is None:
                        stream = c.streams[key] = TextStream(self.transport, c.chat_id)
                    if kind == "assistant_delta":
                        stream.append(event["text"])
                    else:
                        await stream.finish(event["content"])
                elif kind == "tool_media":
                    workspace = Workspace(Path(runtime.state["workspace"]))
                    for attachment in event["attachments"]:
                        if not runtime.owns_attachment(c.attachment_id):
                            break
                        try:
                            path = workspace.resolve_path(attachment["path"])
                            await self.transport.send_attachment(c.chat_id, attachment, path)
                        except (ValueError, OSError, RuntimeError) as error:
                            await self._show(c, "附件发送失败", f"{attachment['filename']}：{error}")
                elif kind in {
                    "turn_completed",
                    "turn_cancelled",
                    "turn_failed",
                    "fatal",
                    "session_open_failed",
                }:
                    for stream in c.streams.values():
                        await stream.finish()
                    c.streams.clear()
                    for request_id in list(c.interactions):
                        await self._resolve(c, request_id, "请求已结束。")
                    title = {"turn_completed": "处理完成", "turn_cancelled": "已停止"}.get(
                        kind, "执行失败"
                    )
                    text = event["error"]["message"] if event.get("error") else self._status(c)
                    if c.progress is not None:
                        await self.transport.update(c.progress, Card(title, text))
                        c.progress = None
                    else:
                        await self._show(c, title, text)
                elif kind == "runtime_state":
                    pending = [
                        value["request_id"]
                        for value in (event["approval"], event["question"])
                        if value is not None
                    ]
                    for request_id in list(c.interactions):
                        if request_id not in pending:
                            await self._resolve(c, request_id, "请求已结束。")
                elif kind == "user_steer_rejected":
                    await self._show(c, "补充指令未接收", "当前执行已经结束，请重新发送。")
                elif kind == "attachment_replaced":
                    for stream in c.streams.values():
                        await stream.abort()
                    c.streams.clear()
                    for request_id in list(c.interactions):
                        await self._resolve(c, request_id, "会话已由其他端接管。")
                    await self._show(c, "会话已被接管", "使用 /takeover 可重新接管。")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Mobile event delivery failed: %s", c.key)
        finally:
            for stream in c.streams.values():
                await stream.abort()
            c.streams.clear()
            await runtime.detach(queue, c.attachment_id)

    async def close(self) -> None:
        for conversation in self.conversations.values():
            await self._detach(conversation)
