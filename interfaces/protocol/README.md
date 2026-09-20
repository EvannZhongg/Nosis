# Protocol

该目录提供 TUI 和 GUI 共用的 TypeScript 协议类型，不包含 Agent 执行逻辑。

消息结构定义在 [`src/protocol.ts`](src/protocol.ts)，并与 Python 端的 [`../bridge/protocol.py`](../bridge/protocol.py) 保持同步。修改协议时还需更新两个前端的消息处理与相关测试。

连接以 `open_session` 开始，`session_ready` 只表示 Session 控制面可用。随后首个 `runtime_state` 是前端同步屏障：前端可在等待期间编辑草稿，但应在应用该执行面快照后再发送命令。执行面的生命周期由 `runtime_state.phase` 表示；`inactive` 状态下仍可修改权限、Provider 和 Workspace，首个 `user_turn` 才激活 Agent Runtime。

`turn_failed` 与 `fatal` 使用统一的结构化错误：`type`、`message` 和
`details` 均为必填字段。Provider 协议错误可在 `details` 中携带阶段、模型、
Tool Call 身份、参数长度、分片长度与摘要；不得传输完整 Tool 参数。
