# Bridge

`interfaces.bridge` 是前端与 [`agent_runtime`](../../agent_runtime/README.md) 之间的协议适配层，也是前端驱动 Agent、执行 Tool、授权、提问、steer 和取消的唯一通道。

```text
TUI ── 本地 Bridge 协议 ── nosis-server ◀── GUI / IM
                              └─ python -m interfaces.bridge
                                   ├─ Session A ── RuntimeHost A ── agent_core
                                   └─ Session B ── RuntimeHost B ── agent_core
```

Bridge 作为独立 Python 进程运行，通过 stdin/stdout 交换每行一个 JSON 对象的 newline-delimited JSON。它不装配 Provider、Tool、MCP 或 Agent，也不包含 Agent Loop。

## 进程入口

安装 `nosis-agent` 后，第三方客户端使用 `nosis-bridge` 启动同一个 stdio
Bridge，无需了解内部模块路径。入口在保留协议 stdout 后调用 Runtime 的首次
配置初始化，再进入协议循环。公开契约见 [Bridge Public Client API](../../docs/bridge-protocol.md)，
现状审查与边界设计见 [设计文档](../../docs/bridge-api-design.md)。

`__main__.py` 在 import Provider 相关模块前保留专用 stdout 协议通道，并把普通 `print()` 和第三方库输出重定向到 stderr，防止日志破坏 JSON 消息流。它也统一处理 UTF-8 stdin、启动期中断和无法进入协议循环时的 `fatal` 错误。

`nosis-server` 管理同一配置目录的 Bridge 和 Scheduler，供 GUI、IM 和本地 TUI 共同使用。启动 TUI 时若常驻宿主尚未运行，TUI 可独立持有 Bridge。配置目录的执行锁限制一个 Bridge 取得 Runtime 执行权。

## 连接与路由

进程预热默认 Provider SDK、tokenizer 与 TLS 信任上下文后发送 `bridge_ready`，不解析凭据或调用模型。每条会话命令以 `channel_command` 携带 `channel_id` 与 `command`，事件以 `channel_event` 携带同一通道 ID 与 `event`。每个通道的第一条命令必须是 `open_session`。Bridge 调用 `RuntimeHost.open_session()` 建立轻量 Session 控制面，然后依次发送：

1. `session_ready`：Session、Workspace、Provider 和权限已恢复。
2. `runtime_state`：当前执行面的权威快照，也是前端发送后续命令前的同步屏障。

Journal 写入权被占用、工作区或配置无效、文件系统错误导致打开失败时，Bridge 返回 `session_open_failed`，保持协议连接并允许 `list_workspaces` 或重新发送 `open_session`；存在工作区浏览上下文时也允许 `list_sessions`。前端在收到 `session_ready` 前不得请求历史记录；在初始 `runtime_state` 前不得提交执行命令。同一应用宿主内打开已有会话会附着原 Bridge，模型或工作区变更通过显式协议消息执行，不另建 Session 写者。

Provider、MCP、Tool 和 Agent 由 Runtime 在首个交互或定时 Turn 到来时延迟初始化。

每个 Session 由一个 RuntimeHost 持有，交互和定时 Turn 在该 Runtime 排队。Scheduler 先打开的 Session 可以附着，并复用它已经装配的执行面。不同 Session 并发执行。通道绑定单个 Session，切换时使用目标 Session 的通道。

GUI、TUI 和 SDK 都使用这一常驻服务端。TUI 为访问过的会话保留独立通道，切回时发送 `session_snapshot`，由 Bridge 返回该 Runtime 的 ready/snapshot 对，不释放 Agent、Jobs、Journal 或 MCP。SDK 的发送与取消接口必须显式携带通道 ID。

附着失败通过 `session_open_failed` 返回。已经打开的其他通道仍可使用。

`list_workspaces` 复用 Core 的历史会话分组，返回已有会话的工作区路径。选择工作区后发送该路径和 `session_id: null`，即可新建会话并在同步后直接对话；`list_sessions` 继续只列出当前工作区的历史会话。两种入口都通过 `open_session` 完成绑定，不使用会迁移当前会话的 `workspace_set`。

Bridge 路由的输入包括：

- `user_turn`、`user_steer` 和 `cancel`
- `approval_response` 与 `user_question_response`
- `permission_set`、`provider_set` 和 `workspace_set`
- Session 列表与历史加载
- 设置读取及 Provider、Agent、路由更新
- 进程级 `close_channel` 是关闭通道，`shutdown` 是结束连接。常驻宿主继续管理已附着过的 Runtime。

Bridge 把 Core 的 `AgentEvent`、Runtime phase、计划、MCP 状态和结束结果转换为协议消息。普通模型调用结束时立即推送 `model_completed`（本次用量与会话累计缓存快照），每次压缩尝试结束时推送 `compression_completed`（会话累计缓存快照），不等待 Tool 执行、压缩重试或 Turn 结束。实时 Tool 事件只包含执行状态；会话检查点包含 Core 已规范化的对话内容与 Artifact 引用，大输出仍由 Core 落盘。

## 执行状态与交互

Bridge 维护的是协议侧投影，而不是另一套 Runtime 状态。它负责：

- 发布包含 phase、turn、授权、提问、Job、计划、上下文窗口、会话累计缓存用量、Workspace 路径和 Runtime warning 的 `runtime_state`
- 为事件分配序号，并发送与会话内容绑定的 transcript 检查点及续传游标
- 将同步的授权和用户提问等待转换为协议 request / response
- 把运行中的新输入转发为 steer
- 把异常转换成结构化 `turn_failed` 或 `fatal`

## Workspace 辅助

`managed_workspaces.py` 提供 Nosis 管理的 scratch Workspace 创建、识别与安全删除规则。它由启动器和 GUI 工作区接口复用，不参与 Agent 的 Workspace 文件访问语义。

## 模块索引

| 模块 | 职责 |
| --- | --- |
| `__main__.py` | 独立进程入口和协议 stdout 隔离 |
| `server.py` | 进程协议、多通道分发与工作线程生命周期 |
| `bridge.py` | 单通道消息路由、Runtime 回调、交互等待和状态投影 |
| `protocol.py` | Python 端编解码与 Core event → 协议消息转换 |
| `event_stream.py` | 事件序号、会话检查点与续传游标 |
| `managed_workspaces.py` | scratch Workspace 生命周期辅助 |

TypeScript 消息类型位于 [`interfaces/protocol`](../protocol/README.md)。修改消息结构时必须同步 Python 定义、TypeScript 类型、前端处理和相关测试。
