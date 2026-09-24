# Agent Runtime

`agent_runtime` 把 [`agent_core`](../agent_core/README.md) 拼装成可运行的 Runtime，供 Bridge 与定时任务共用。它只负责装配与运行时生命周期，不含 Agent 决策逻辑。

`RuntimeHost` 是唯一入口，负责：

- 初始化并读取配置、Prompt 模板、Workspace Instructions、standalone Skill 和 Plugin descriptor，选择 Provider
- 组装 Tool、权限策略、MCP 和子 Agent Runtime
- 绑定 Workspace 与 Session
- 管理 ExecutionPlane、turn、取消、steer 和定时任务

一次具体装配由 `ExecutionPlaneManager` 原子持有，包括 Workspace、Provider、已解析 Agent 配置、Workspace Instructions、Agent、MCP、后台 Jobs 和 context window；Provider、Workspace、配置或 Instructions 变化时旧 plane 整体关闭，下一次使用时重新装配。`SessionRuntimeController` 负责 Session、Workspace、Provider 与权限状态，`TurnRunner` 负责 turn、取消和 steer，`ScheduledTurnRunner` 以无传输的方式执行定时任务。

Agent Loop、Tool 执行和上下文管理属于 `agent_core`，前端与 Bridge 都不复制这些语义。前端只通过 Bridge 协议驱动它。

配置目录、默认配置与内置 Skill/Plugin 安装源在 [`defaults/`](defaults)，字段与结构见 [`agent_core`](../agent_core/README.md)。
