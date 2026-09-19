# Bridge

Bridge 是前端与 Agent Runtime 之间的唯一执行通道。它以独立进程运行，通过 stdin/stdout 交换 newline-delimited JSON：

```text
nosis / nosis-gui
  └─ python -m interfaces.bridge
       └─ agent_core
```

Bridge 负责：

- 初始化并读取配置、Prompt 模板、Workspace Instructions 和内置 Skill，选择 Provider
- 组装 Tool、权限策略、MCP 和子 Agent Runtime
- 绑定 Workspace 与 Session
- 在 `AgentEvent` 和协议消息之间转换
- 转发用户输入、授权、提问、取消和运行状态

一次具体装配由 `ExecutionPlane` 原子持有，包括 Workspace、Provider、已解析 Agent 配置、Workspace Instructions、Agent、MCP、后台 Jobs 和 context window。Bridge 只保留一个当前 plane；Provider、Workspace、配置或 Instructions 改变时，旧 plane 整体关闭，下一次装配成功后再以单一对象挂载。

Agent Loop、Tool 执行和上下文管理仍属于 `agent_core`，前端与 Bridge 都不复制这些语义。

首条输入必须是 `open_session`。Bridge 先完成轻量 Session 初始化，依次发送 `session_ready` 与初始 `runtime_state`；后者是前端可以开始发送命令的同步屏障。Provider、MCP、Tool 与 Agent 只在首个 `user_turn` 到达时初始化。协议类型分别定义在 [`protocol.py`](protocol.py) 和 [`../protocol/src/protocol.ts`](../protocol/src/protocol.ts)，修改消息时必须同步更新两端。

进程入口为 `__main__.py`，主要装配位于 `bridge.py`，执行生命周期定义在 `execution_plane.py`。Bridge 可使用内存输入输出进行测试，无需启动 TUI 或 GUI。
