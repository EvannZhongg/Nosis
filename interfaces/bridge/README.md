# Bridge

Bridge 是 Runtime 与 TUI/GUI 之间的适配层。它通过 stdin/stdout 使用 newline-delimited JSON（每行一个 JSON 对象），把 `AgentEvent` 翻译为协议消息，并转发用户输入、Tool 授权和取消请求。

stdin 只由一个 Reader Thread 消费，再按消息类型路由：普通 turn 进入主队列，审批与用户问题响应进入对应 waiter，运行中的 `user_steer` 进入当前 `TurnControl`。steer 只会在模型调用结束、完整 Tool batch 已写回后，或最终回答 return 前的边界写入 Session。

## 进程关系

```text
nosis / nosis-gui
  └─ python -m interfaces.bridge
       └─ agent_core
```

前端不直接调用 `agent_core`，Bridge 也不包含 Agent 决策逻辑。协议消息类型定义在 [`../protocol/src/protocol.ts`](../protocol/src/protocol.ts)。

## 行为约定

- 首条消息必须是 `start`，否则返回 `fatal`。
- `permission_set` 更新 Runtime 的 Session 级权限 preset，并以 `permission_changed` 回传；`ask_for_approval` 下 shell 和配置为需确认的 MCP Tool 在执行前发送 `approval_request`，前端回复 `approval_response`，`full_access` 仅跳过该确认。
- `list_sessions` 以 `sessions_listed` 回传当前 Workspace 的 Session 摘要（`session_id` 与标题），供前端列出可切换的对话；`load_session` 以 `session_items` 回传本次 `start` 载入的对话内容，供前端回放历史。两条都由前端按需请求：GUI 从 HTTP 读取会话内容，因此不会触发。
- main Agent 的 `ask_user` Tool 发送 `user_question`，前端回复 `user_question_response`；Bridge 等待回答并将其作为 Tool Result 返回。
- `Esc`、`Ctrl+C`、GUI 的「停止」都会向当前 `turn_id` 发送 `cancel`；Bridge 负责唤醒交互 waiter 并中断正在执行的工作。取消前真实发生的 Turn、Message 与 Tool 状态都已由 Runtime 写入 Journal。未取得结果的 Tool Call 不会被删除，只会被 Provider projection 排除。
- TUI 直接拥有 Bridge 进程；GUI 服务按 Session 拥有活动 Bridge，浏览器 WebSocket 只做 attach/detach。浏览器断开不会取消正在执行的轮次，GUI 服务会暂存断线期间的事件和审批状态供重连恢复。
- Runtime 关闭或 TUI 发送 `shutdown` 时，不会执行尚未获批的命令。

Bridge 的 Python 实现位于 `bridge.py`，进程入口为 `__main__.py`；测试可直接使用内存输入输出驱动 `Bridge`，无需启动界面进程。
