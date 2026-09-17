# Bridge

Bridge 是前端与 Agent Runtime 之间的唯一执行通道。它以独立进程运行，通过 stdin/stdout 交换 newline-delimited JSON：

```text
nosis / nosis-gui
  └─ python -m interfaces.bridge
       └─ agent_core
```

Bridge 负责：

- 读取配置并选择 Provider
- 组装 Tool、权限策略、MCP 和子 Agent Runtime
- 绑定 Workspace 与 Session
- 在 `AgentEvent` 和协议消息之间转换
- 转发用户输入、授权、提问、取消和运行状态

Agent Loop、Tool 执行和上下文管理仍属于 `agent_core`，前端与 Bridge 都不复制这些语义。

首条输入必须是 `start`。协议类型分别定义在 [`protocol.py`](protocol.py) 和 [`../protocol/src/protocol.ts`](../protocol/src/protocol.ts)，修改消息时必须同步更新两端。

进程入口为 `__main__.py`，主要装配位于 `bridge.py`。Bridge 可使用内存输入输出进行测试，无需启动 TUI 或 GUI。
