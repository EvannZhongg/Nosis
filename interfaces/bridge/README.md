# Bridge

`interfaces.bridge` 是前端与 [`agent_runtime`](../../agent_runtime/README.md) 之间的协议适配层，也是前端驱动 Agent、执行 Tool、授权、提问、steer 和取消的唯一通道。

```text
TUI / GUI host
  └─ python -m interfaces.bridge
       └─ RuntimeHost ── agent_core
```

Bridge 作为独立 Python 进程运行，通过 stdin/stdout 交换每行一个 JSON 对象的 newline-delimited JSON。它不装配 Provider、Tool、MCP 或 Agent，也不包含 Agent Loop。

## 进程入口

`__main__.py` 在 import Provider 相关模块前保留专用 stdout 协议通道，并把普通 `print()` 和第三方库输出重定向到 stderr，防止日志破坏 JSON 消息流。它也统一处理 UTF-8 stdin、启动期中断和无法进入协议循环时的 `fatal` 错误。

TUI 和 GUI 各自负责启动与附着 Bridge 子进程。`process.py` 提供启动期或失联时的进程组取消辅助，确保 Bridge 的子进程一并退出。

## 连接与路由

每个连接的第一条消息必须是 `open_session`。Bridge 调用 `RuntimeHost.open_session()` 建立轻量 Session 控制面，然后依次发送：

1. `session_ready`：Session、Workspace、Provider 和权限已恢复。
2. `runtime_state`：当前执行面的权威快照，也是前端发送后续命令前的同步屏障。

Provider、MCP、Tool 和 Agent 仍由 Runtime 在首个 `user_turn` 到来时延迟初始化。

Bridge 路由的输入包括：

- `user_turn`、`user_steer` 和 `cancel`
- `approval_response` 与 `user_question_response`
- `permission_set`、`provider_set` 和 `workspace_set`
- Session 列表与历史加载
- 设置读取及 Provider、Agent、路由更新
- `shutdown`

Bridge 把 Core 的 `AgentEvent`、Runtime phase、计划、MCP 状态和结束结果转换为协议消息。Tool 的完整大输出不会经协议发送；前端只收到执行状态，模型所需结果和 Artifact 由 Core 处理。

## 执行状态与交互

Bridge 维护的是协议侧投影，而不是另一套 Runtime 状态。它负责：

- 发布包含 phase、turn、授权、提问、Job、计划和上下文窗口的 `runtime_state`
- 将同步的授权和用户提问等待转换为协议 request / response
- 把运行中的新输入转发为 steer
- 把异常转换成结构化 `turn_failed` 或 `fatal`

## Workspace 辅助

`managed_workspaces.py` 提供 Nosis 管理的 scratch Workspace 创建、识别与安全删除规则。它由启动器和 GUI 工作区接口复用，不参与 Agent 的 Workspace 文件访问语义。

## 模块索引

| 模块 | 职责 |
| --- | --- |
| `__main__.py` | 独立进程入口和协议 stdout 隔离 |
| `bridge.py` | 消息路由、Runtime 回调、交互等待和状态投影 |
| `protocol.py` | Python 端编解码与 Core event → 协议消息转换 |
| `process.py` | Bridge 进程组取消 |
| `managed_workspaces.py` | scratch Workspace 生命周期辅助 |

TypeScript 消息类型位于 [`interfaces/protocol`](../protocol/README.md)。修改消息结构时必须同步 Python 定义、TypeScript 类型、前端处理和相关测试。
