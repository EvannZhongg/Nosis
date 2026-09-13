# Bridge

Bridge 是 Runtime 与 TUI/GUI 之间的适配层。它通过 stdin/stdout 使用 newline-delimited JSON（每行一个 JSON 对象），把 `AgentEvent` 翻译为协议消息，并转发用户输入、Tool 授权和取消请求。

## 进程关系

```text
nosis / nosis-gui
  └─ python -m interfaces.bridge
       └─ agent_core
```

前端不直接调用 `agent_core`，Bridge 也不包含 Agent 决策逻辑。协议消息类型定义在 [`../protocol/src/protocol.ts`](../protocol/src/protocol.ts)。

## 行为约定

- 首条消息必须是 `start`，否则返回 `fatal`。
- shell 和 MCP Tool 在执行前发送 `approval_request`，前端回复 `approval_response`。
- `Esc`、`Ctrl+C`、GUI 的「停止」都会取消当前轮次；取消的轮次同样写入已产生的 items，未取得结果的 Tool Call 会被丢弃。
- 前端关闭或发送 `shutdown` 时，不会执行尚未获批的命令。

Bridge 的 Python 实现位于 `bridge.py`，进程入口为 `__main__.py`；测试可直接使用内存输入输出驱动 `Bridge`，无需启动界面进程。
