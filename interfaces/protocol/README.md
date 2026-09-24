# Protocol

`interfaces/protocol` 提供 TUI 与 GUI 共用的 TypeScript 协议类型、状态辅助函数和 newline-delimited JSON 解码器，不包含网络连接、进程管理或 Agent 执行逻辑。

主入口是 [`src/protocol.ts`](src/protocol.ts)。Python 端消息构造与事件转换位于 [`interfaces/bridge/protocol.py`](../bridge/protocol.py)，其余部分消息由 Bridge 连接逻辑发出。

## 连接约定

连接必须以 `open_session` 开始：

```text
open_session
  ← session_ready
  ← runtime_state
后续命令与事件
```

`session_ready` 仅表示 Session 控制面可用。首个 `runtime_state` 才是同步屏障；前端可以在等待期间编辑草稿，但必须应用该快照后再发送命令。

`runtime_state.phase` 表示执行面生命周期：

- `inactive`：Session 已打开，执行面尚未启动。
- `starting`：正在装配 Runtime。
- `idle`：执行面可用且没有活动 Turn。
- `running`：Turn 正在执行。
- `waiting_approval`：等待 Tool 授权。
- `waiting_user`：等待结构化用户回答。
- `failed`：执行面启动或执行失败。

`runtimeIsActive()` 为两个前端提供统一的活动状态判断。

## 消息类型

`Outgoing` 按用途分为：

- Session 打开、历史加载和关闭
- Turn 提交、steer 与取消
- 授权和用户提问响应
- 权限、Provider 与 Workspace 修改
- 设置读取和保存
- GUI 附着、事件续传与 takeover 参数

`Incoming` 按用途分为：

- `session_ready` 和权威 `runtime_state`
- assistant / reasoning 流式内容和完整消息
- Tool 批次、调用、结果与媒体
- 上下文窗口、计划和后台 Job
- steer、授权、用户提问与 MCP 状态
- Session 历史、设置快照和状态变更确认
- Turn 完成、取消、失败和进程级 fatal

公共数据类型还包括附件、Tool Call、token usage、权限 preset、计划、Context Window、Session Item、设置快照与结构化 Runtime error。

## 事件顺序与恢复

协议消息可以携带 `event_sequence`，GUI 使用 `after_event` 从已知序号后继续接收事件。历史回放消息带 `replayed`，前端应据此避免把旧快照当作新命令确认。

同一 GUI Session 的页面所有权由 `attachment_id`、`attach_only`、`takeover` 和 `attachment_replaced` 表达。这些字段只处理连接附着，不承载 Session 或 Agent 语义。

## 错误约定

`turn_failed` 与 `fatal` 使用统一结构：

```ts
type RuntimeFailure = {
  type: string;
  message: string;
  details: Record<string, JSONValue>;
};
```

Provider 协议错误可在 `details` 中提供阶段、模型、Tool Call 身份、长度和摘要等定位信息，但不得传输完整 Tool 参数。设置校验失败使用非致命的 `settings_update_failed`。

## 维护规则

协议变更必须同时检查：

1. `src/protocol.ts` 的 TypeScript 类型。
2. `interfaces/bridge/protocol.py` 及 `bridge.py` / `__main__.py` 的发送逻辑。
3. TUI 与 GUI 的消息处理。
4. Python 与前端协议测试。

`MessageDecoder` 只负责把任意 byte chunk 边界重新组合成逐行 JSON 消息，不校验业务状态机。
