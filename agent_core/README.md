# Agent Core

`agent_core` 提供与应用界面无关的 Agent 执行机制。它不知道 TUI、GUI、Bridge、配置目录或进程来源，也不负责选择具体 Workspace 和 Provider。

依赖方向如下：

```text
interfaces → agent_runtime → agent_core
```

`agent_core` 不 import `agent_runtime` 或 `interfaces`。应用级配置读取、Plugin 发现和对象装配由 [`agent_runtime`](../agent_runtime/README.md) 完成。

## 执行模型

`Agent` 实现显式的 Agent Loop：构造模型请求、接收流式输出、组装 Tool Call、执行 Tool 批次、把结果回灌模型，并根据完成、错误、取消和调用上限结束当前 Turn。

执行过程通过结构化事件输出，包括文本与 reasoning 增量、完整消息、Tool 批次、Tool 调用与结果、媒体、上下文窗口、steer 应用和后台 Job 状态。Core 不决定这些事件在前端如何展示。

相关模块：

| 模块 | 职责 |
| --- | --- |
| `agent.py` | Agent Loop、执行事件和终止条件 |
| `tool_batch.py` | Tool 批次调度、结果回灌、Journal 记录与清理 |
| `turn_control.py` | Turn 取消和用户 steering mailbox |
| `errors.py` | Runtime 错误的结构化表示 |
| `prompting.py` | 主 Agent 与子 Agent system prompt 渲染 |

## 上下文与内容

消息内容统一表示为文本、图片和文件 part。图片可以进入支持视觉的 Provider；普通附件以 Workspace 文件引用进入上下文。

`ContextManager` 按完整语义单元投影 Session 历史，计算窗口占用，并在达到阈值时压缩较早上下文、保留近期单元和用户锚点。压缩记录写回 Session；超过硬上限时明确失败。

| 模块 | 职责 |
| --- | --- |
| `content.py` | 文本、图片、文件与附件数据类型 |
| `projection.py` | 从 Session Journal 投影模型上下文单元 |
| `context_manager.py` | token 预算、上下文压缩和窗口快照 |
| `media.py` | 图片格式探测、尺寸、编码与 token 估算 |
| `public_url.py` | Web 资源访问前的公网 URL 校验 |

## Session、计划与持久化

`Session` 是执行状态的事实来源。对话、Tool 执行、Job、计划和恢复所需状态以 append-only Journal Event 表示；`JsonlSessionStore` 负责 JSONL 持久化、元数据、Session 列表与恢复。

`PlanManager` 从最新 `plan_updated` 事件恢复当前计划。计划可跨 Turn 延续，每个步骤记录状态以及不超过 500 字符的结果或阻塞原因。

较大的 Tool 输出由 `ToolResultNormalizer` 保存为 Session Artifact，只把可控大小的预览和 Artifact 引用送回模型。

| 模块 | 职责 |
| --- | --- |
| `session.py` | Message、Turn、Tool / Job Execution 和 Journal Event |
| `session_store.py` | append-only JSONL、Session 元数据、查询与恢复 |
| `session_paths.py` | Session 与 Workspace 存储路径规则 |
| `plan.py` | 计划状态、更新与序列化 |
| `tool_result.py` | Tool 输出预算、预览和 Artifact 落盘 |

## Tool 系统

所有 Tool 使用统一的 `Tool`、`ToolDefinition`、`ToolCall`、`ToolResult` 和 `ToolExecutionContext` 接口。`ToolCatalog` 保存完整能力集合，`ToolSet` 按 Agent 角色选择实际可见 Tool，`ToolPolicy` 在执行前处理授权。

内置 Tool 覆盖：

- Workspace 文件读取、搜索、列举、写入和 patch
- Shell 命令执行
- Web Search 与 Web Fetch
- 图片读取、分析与生成
- Skill 读取和子 Agent 派发
- 用户提问、计划更新与长期记忆写入
- 定时任务创建、修改、查询和删除

`ask_user`、`update_plan`、`remember` 等 Tool 依赖注入的 Runtime 能力，不把应用决策复制到 Tool 内。图片生成只面向主 Agent；角色可见性由装配阶段选择。

`tools/` 内的主要分工：

| 模块 | 职责 |
| --- | --- |
| `base.py` | Tool 抽象和结构化输入输出 |
| `catalog.py` | Catalog 与按名称选取的 ToolSet |
| `config.py` | Tool 开关配置结构 |
| `context.py` | 每次调用所需的 Workspace、Session、执行器等依赖 |
| `policy.py` | Shell 与 MCP 的授权策略组合 |
| `budget.py` | Tool 结果大小预算 |
| `builtin/` | 内置 Tool 实现 |

## 命令执行与权限

Shell 通过 `ExecutionRouter` 根据 `ExecutionScope` 和当前 `ExecutionAuthority` 选择宿主执行器或 Workspace 沙箱执行器。`PermissionController` 把 Session 的权限 preset 转换为默认 scope、允许范围和人工确认规则。

| preset | 默认 scope | 授权行为 |
| --- | --- | --- |
| `Ask for approval` | `workspace` | 两种 scope 都请求确认 |
| `Workspace Access` | `workspace` | 仅 `host` 请求确认 |
| `Full Access` | `host` | 不请求确认 |

显式传入的 scope 始终生效，但仍受 Runtime 提供的 authority 上限约束。文件 Tool 不经过 Shell scope，始终限制在 Workspace。

`execution/` 负责进程超时、取消、输出 spool、平台 shell 和沙箱路由：

- macOS 使用 Seatbelt。
- Linux 使用 bubblewrap；缺失或 namespace 初始化失败时明确报错，不回退宿主执行。
- Windows 使用 Restricted Token，Shell 方言为 PowerShell 7。

沙箱策略使用结构化的文件系统、网络、临时目录和进程隔离能力描述；平台 backend 负责把策略翻译成具体 OS 约束。

## Provider 与模型协议

`LLMProvider` 定义模型调用接口、能力信息、流式响应和 token usage。当前实现通过 LiteLLM 接入文本与视觉模型，并有独立的图片生成 Provider 实现。

`ToolCallStreamAssembler` 负责把流式 Tool Call 分片组装为结构化参数，并在协议异常时提供不包含完整敏感参数的定位信息。

| 模块 | 职责 |
| --- | --- |
| `llm.py` | Provider 抽象、请求、响应与能力类型 |
| `providers/litellm_provider.py` | LiteLLM 文本和视觉适配 |
| `providers/tool_call_stream.py` | 流式 Tool Call 组装与校验 |
| `image_generation.py` | 图片生成抽象 |
| `providers/image_generation.py` | LiteLLM 图片生成实现 |

## Skill、MCP 与子 Agent

Core 只定义扩展机制，不决定扩展从哪个目录加载：

- `SkillSource` 描述可枚举的 Skill 来源，`SkillLoader` 与 `SkillRegistry` 负责元数据加载、注册及按需读取正文。
- `McpClientManager` 管理 stdio 与 streamable HTTP Server 生命周期，将远程 Tool 适配为统一 `McpTool`。
- `SubagentRoleRegistry` 保存角色；`SubagentRuntime` 为每次调用创建独立 Session，并复用同一套 Agent、Tool 和上下文机制。

相关模块为 `skills.py`、`mcp/` 和 `subagent.py`。

## Memory、Job 与 Scheduler

长期记忆分为全局和 Workspace 两层。`MemoryManager` 在 Turn 开始时提供记忆上下文，收集候选更新，并在成功结束后通过 `MemoryReconciler` 合并；失败或取消时丢弃待提交更新。

`JobManager` 管理可取消的后台任务和状态事件，供子 Agent 等长任务使用。

`SchedulerService` 定义 one-shot、interval、cron trigger，以及任务、运行记录、错过执行的处理和 JSONL 持久化。它通过注入的 runner 发起 Agent Turn，但不负责组装应用 Runtime。

相关模块为 `memory.py`、`jobs.py` 和 `scheduler.py`。

## Workspace Instructions

`workspace_instructions.py` 只定义 instruction 条目、合并结果和 fingerprint 数据结构。具体读取哪些文件、来源优先级以及何时触发 Runtime 重建，由 `agent_runtime` 决定。
