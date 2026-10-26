# Agent Core

`agent_core` 提供与应用界面无关的 Agent 执行机制。它不知道 TUI、GUI、Bridge、配置目录或进程来源，也不负责选择具体 Workspace 和 Provider。

依赖方向如下：

```text
interfaces → agent_runtime → agent_core
```

`agent_core` 不 import `agent_runtime` 或 `interfaces`。应用级配置读取、Plugin 发现和对象装配由 [`agent_runtime`](../agent_runtime/README.md) 完成。

## 执行模型

`Agent` 实现显式的 Agent Loop：构造模型请求、接收流式输出、组装 Tool Call、执行 Tool 批次、把结果回灌模型，并根据完成、错误、取消和调用上限结束当前 Turn。

`Agent.run()` 成功返回 `AgentRunResult`，包含最终回答 `content` 和最后一次普通模型调用的 `usage`。执行过程和 Provider 状态保存在 Session Journal 中。

执行过程通过结构化事件输出，包括文本与 reasoning 增量、完整消息、Tool 批次、Tool 调用与结果、媒体、上下文窗口、steer 应用和后台 Job 状态。Core 不决定这些事件在前端如何展示。

相关模块：

| 模块 | 职责 |
| --- | --- |
| `agent.py` | Agent Loop、执行事件和终止条件 |
| `tool_batch.py` | Tool 批次调度、结果回灌、Journal 记录与清理 |
| `turn_control.py` | Turn 取消和用户 steering mailbox |
| `loop_policy.py` | 调用上限、重复调用防护与 Turn 续跑策略 |
| `errors.py` | Runtime 错误的结构化表示 |
| `prompting.py` | 主 Agent 与子 Agent system prompt 渲染 |

## 基础类型与配置

与前端无关的基础类型，以及 Core 侧的配置结构定义和文件加载；配置目录的位置与来源仍由 `agent_runtime` 决定。

| 模块 | 职责 |
| --- | --- |
| `config.py` | `AgentConfig` 及上下文、记忆、Provider、Skill、子 Agent 角色等配置结构与 `load_agent_config` |
| `workspace.py` | `Workspace` 类型与路径包含校验 |
| `path_utils.py` | 路径比较与包含判断 |
| `atomic.py` | 文件原子写入 |
| `time_utils.py` | UTC 时间序列化 |

## 上下文与内容

消息内容统一表示为文本、图片和文件 part。图片可以进入支持视觉的 Provider；普通附件以 Workspace 文件引用进入上下文。

`ContextManager` 按完整语义单元投影 Session 历史，计算窗口占用，并在达到阈值时压缩较早上下文、保留近期单元和用户锚点。压缩使用当前 Agent 的模型，保留主请求的 system、工具定义和原始待归档消息前缀，在尾部追加压缩指令；近期尾部不送入压缩，格式修复指令也追加在尾部。可用输入预算为模型窗口减去 `output_reserve_tokens`；默认阈值在窗口不超过 32K 时取预算的 75%，更大窗口取 `min(预算的 80%, 256K)`，配置 `trigger_ratio` 时以它为准。用户锚点先保留最近两整轮，再按 `user_anchor_token_budget` 向前扩展连续 Turn，判断时计入已选中的全部输入，压缩不会改变保留范围。压缩结果验证后写回 Session，压缩返回的 Tool Call 不执行；超过硬上限时明确失败。

| 模块 | 职责 |
| --- | --- |
| `content.py` | 文本、图片、文件与附件数据类型 |
| `projection.py` | 从 Session Journal 投影模型上下文单元 |
| `context_manager.py` | token 预算、上下文压缩和窗口快照 |
| `media.py` | 图片格式探测、尺寸、编码与 token 估算 |
| `public_url.py` | Web 资源访问前的公网 URL 校验 |

## Session、计划与持久化

`Session` 是执行状态的事实来源。对话、Tool 执行、Job、计划和恢复所需状态以 append-only Journal Event 表示；`JsonlSessionStore` 负责 JSONL 持久化、元数据、Session 列表、跨 Session 对话全文搜索与恢复。

Runtime 通过 `JsonlSessionStore.open_journal()` 持有当前 Session 的写入句柄，Session 关闭或切换时释放；子 Agent 在执行期间持有自己的句柄。Windows 句柄仅共享读取，阻止同用户子进程删除、重命名或覆盖打开的 Journal，Runtime 继续通过原句柄追加并 fsync。保护不覆盖已关闭的 Session、其他 Session 文件、POSIX 删除语义或能够终止 Runtime 的进程，不等同于独立权限的审计存储。

`PlanManager` 从最新 `plan_updated` 事件恢复当前计划。计划可跨 Turn 延续，每个步骤记录状态以及不超过 500 字符的结果或阻塞原因。

较大的 Tool 输出由 `ToolResultNormalizer` 保存为 Session Artifact，只把可控大小的预览和 Artifact 引用送回模型。Artifact 是按行读取的文本阅读视图：结构化结果按字段路径展开，字符串保留正文换行，超长行分段写入；Shell spool 也经过同一落盘处理。该视图不作为原始 JSON 解析。

| 模块 | 职责 |
| --- | --- |
| `session.py` | Message、Turn、Tool / Job Execution 和 Journal Event |
| `session_store.py` | append-only JSONL、Session 元数据、列表与全文搜索、恢复 |
| `session_paths.py` | Session 与 Workspace 存储路径规则 |
| `plan.py` | 计划状态、更新与序列化 |
| `tool_result.py` | Tool 输出预算、预览和 Artifact 落盘 |

## Tool 系统

所有 Tool 使用统一的 `Tool`、`ToolDefinition`、`ToolCall`、`ToolResult` 和 `ToolExecutionContext` 接口。`ToolCatalog` 保存完整能力集合，`ToolSet` 按 Agent 角色选择实际可见 Tool，`ToolPolicy` 在执行前处理授权。参数不是合法的 JSON object 时，`ToolCall` 保留 Provider 的原文并直接得到 `invalid_tool_arguments` 错误结果，模型可据此重新调用，Turn 不中断。

内置 Tool 覆盖：

- Workspace 文件读取、搜索、列举、写入、编辑和 patch
- Shell 命令执行
- Web Search 与 Web Fetch
- 图片读取、分析与生成
- 桌面截图与输入
- Skill 读取和子 Agent 派发
- 后台 Shell 与子 Agent Job 的查询和取消
- 用户提问、计划更新与长期记忆写入
- 定时任务创建、修改、查询和删除

`ask_user`、`update_plan`、`remember`、`job_control` 等 Tool 依赖注入的 Runtime 能力，不把应用决策复制到 Tool 内。图片生成与桌面自动化只面向主 Agent；`job_control` 只能查看和取消父 Session 的 Job，子 Agent 角色不可见；角色可见性由装配阶段选择。

`web_search` 从 `ToolExecutionContext.tool_settings` 获取 Runtime 注入的 provider 与密钥，Core 不读取配置文件或环境变量。Runtime 在 `~/.nosis/tool_config.json` 中选择 `duckduckgo`（`ddgs` 库，无需密钥）、`exa` 或 `deepseek`，详见 [Runtime 配置](../agent_runtime/README.md#配置与默认资源)。

`web_fetch` 同样使用该上下文中的设置，可选择 `direct` 或 `deepseek`。`direct` 提取网页文本并逐次检查重定向；DeepSeek [Fetch API](https://api-docs.deepseek.com/zh-cn/api/fetch/) 返回标题与 Markdown 正文，保留 Markdown 格式并按工具预算截断。两条路径都校验公开 URL 并限制响应字节数。DeepSeek 密钥缺失或返回 401 时降级到直接抓取；403 是安全策略拒绝，和其他错误一样直接返回，不重试。

后端同时决定 Tool schema 暴露的参数，例如 Exa 的 `include_domains`。DeepSeek 使用 [Search API](https://api-docs.deepseek.com/zh-cn/api/search/)（`POST https://api.deepseek.com/v1/tools/search`），将 `num_results` 映射为 `count`，沿用工具的 1～10 条结果上限；查询限制为 1～128 个 Unicode 字符且不能全为空白，支持 `site:`、`-site:`、引号、`lang:`、`after:`、`before:` 等语法。结果保持统一的 `title`、`url`、`published_date`、`highlights` 格式，其中 `summary` 用作摘要，不请求网页正文。Exa 和 DeepSeek 使用 HTTP 状态码识别鉴权错误；密钥缺失或返回 401 时仅该次调用降级到 DuckDuckGo，并返回结构化的降级原因；其他错误通过现有 Tool 错误路径返回。

`search_files` 与 `list_directory` 默认 60 秒超时并响应取消。`search_files` 跳过符号链接、junction 以及常见依赖、缓存和构建目录；`list_directory` 可传 `timeout_seconds`，递归只进入真实目录，符号链接与 junction 只列出、不展开目标。

`tools/` 内的主要分工：

| 模块 | 职责 |
| --- | --- |
| `base.py` | Tool 抽象和结构化输入输出 |
| `catalog.py` | Catalog 与按名称选取的 ToolSet |
| `config.py` | Tool 开关配置与 Runtime 注入的参数结构 |
| `context.py` | 每次调用所需的 Workspace、Session、执行器等依赖 |
| `paths.py` | 读取路径解析与 Workspace 包含校验 |
| `globs.py` | 文件路径 glob 匹配 |
| `policy.py` | Shell、文件写入、桌面自动化与 MCP 的授权策略组合 |
| `approval.py` | 文件写入审批摘要与审批历史文本 |
| `budget.py` | Tool 结果大小预算 |
| `builtin/` | 内置 Tool 实现 |

## 命令执行与权限

Shell 通过 `ExecutionRouter` 根据 `ExecutionScope` 和当前 `ExecutionAuthority` 选择宿主执行器或 Workspace 沙箱执行器。`permissions.py` 的 `PermissionController` 把 Session 的权限 preset 转换为默认 scope、允许范围和人工确认规则。

| preset | 默认 scope | 授权行为 |
| --- | --- | --- |
| `Ask for approval` | `workspace` | 两种 scope 都请求确认 |
| `Workspace Access` | `workspace` | 仅 `host` 请求确认 |
| `Full Access` | `host` | 不请求确认 |

显式传入的 scope 始终生效，但仍受 Runtime 提供的 authority 上限约束。`apply_patch`、`edit_file`、`write_file` 解析为 Workspace scope，因此同样按 preset 决定是否确认：`Ask for approval` 请求确认，`Workspace Access` 与 `Full Access` 直接执行。文件 Tool 在任何 preset 下都限制在 Workspace 内，不使用宿主执行器。

审批提示与审批历史只写入有界摘要：路径、字节与行数、每个文本字段的前 3 行预览，整体不超过 2048 字符，patch 最多列出 8 个文件操作并说明省略数量。完整参数保留在原始 Tool Call 中，不重复写入用户交互记录。

`execution/` 负责进程超时、取消、输出 spool、平台 shell 和沙箱路由：

- macOS 使用 Seatbelt。
- Linux 使用 bubblewrap；缺失时报错，namespace 初始化失败时保留原始诊断，两者都不回退宿主执行。
- Windows 使用 Restricted Token（`windows_sandbox.py`），Shell 方言为 PowerShell 7。
- Windows 的 host/workspace Shell 统一启用 `ErrorActionPreference=Stop`、严格模式与原生命令错误处理；清理脚本须使用任务专用变量并先验证目标路径边界，不得吞掉初始化或删除错误。

沙箱策略使用结构化的文件系统、网络、临时目录和进程隔离能力描述；平台 backend 负责把策略翻译成具体 OS 约束。

## Provider 与模型协议

`LLMProvider` 定义模型调用接口、能力信息、流式响应和 token usage。`OpenAIProvider` 使用原生 SDK 实现 Responses 与 Chat Completions 协议，官方服务和中转使用同一实现；其他服务商路由由 `LiteLLMProvider` 承担。

`usage.py` 统一定义 `TokenUsage` 和序列化：`input_tokens` 包含缓存部分，`cache_read_tokens` 与 `cache_write_tokens` 记录其读取、写入明细，未报告时为 `None`，明确报告无缓存活动时为 `0`。普通模型、压缩尝试和记忆调用分别以 `model_completed`、`compression_completed`、`memory_completed` 事件写入所属 Session 的同一份 Journal；辅助调用记录实际模型、scope、尝试次数、状态、耗时、用量和错误，不进入对话上下文。Session 在应用普通模型和压缩事件时累计 `CacheUsage`，只计入同时报告输入和缓存读取 token 的调用，包括格式校验失败的压缩尝试；记忆调用单独记录，不计入该口径。会话缓存命中率为 `Σcache_read_tokens / Σinput_tokens`，Journal 重载时恢复，跨 Turn、模型与工作区切换保持累计。每次调用落盘后，`ModelCompletedEvent` 经 Bridge 推送本次用量和会话累计快照，`CompressionCompletedEvent` 只推送会话累计缓存快照，两个前端立即更新，无需等待工具执行、压缩重试或 Turn 结束。`runtime_state.cache_usage` 与 GUI 历史读取提供相同累计快照，事件重放不会重复计数。Bridge 的 `turn_completed.usage` 表示最后一次普通模型调用的用量，不是整个 Turn 的累计用量。

`ModelProvider` 共享媒体序列化、模型信息缓存和可取消的流读取。原生 OpenAI 模型的输入上限、输出上限和视觉能力来自 `providers/openai_models.py` 的本地资料，仅维护经官方模型页核验的 18 个规范模型名，不维护日期快照、chat 别名或预览条目。资料使用官方公布的总上下文窗口，输出预留由 Runtime 扣除；token 估算使用 `tiktoken` 与随包分发的词表，不联网下载或导入 LiteLLM。工具 schema 按完整 JSON 计入，图片沿用共享尺寸估算，加密推理沿用服务端 usage。清单外的模型（含日期快照和别名）必须显式配置 `max_context_tokens`，按文本模型处理且不提供自动输出上限；未知别名和上游尚未公布 tokenizer 映射的 GPT-6 使用 `cl100k_base` 做本地估算，不能视为服务端精确计数。资料表与词表随软件发布维护。

原生文本计数器只缓存编码对象，每次对请求文本与完整 JSON 工具定义重新分词。ContextManager 使用最近一次成功普通模型调用的输入 usage，加上当前请求与锚点请求的本地估算差量；压缩判断和上下文展示共享这个值。锚点随 `model_completed` 写入 Journal，可恢复；压缩事件直接清除锚点，不将压缩状态重复写入指纹；端点、模型、协议、推理参数、系统提示、工具定义、计数策略或压缩状态变化时失效。缓存 token 已包含在输入 usage 中，不再相加；压缩、记忆和标题等辅助调用不替换普通请求锚点。首次请求及失效后的请求仍为本地估算，增量误差仍可能累积。DeepSeek 原生 Chat 在无工具时忽略历史 reasoning；Responses 只回传原生 reasoning 项，展示摘要不计入输入，加密 reasoning 按原生回传状态和 usage 计入。

DeepSeek Flash 与 V4 Pro 共用原生 OpenAI SDK 的 Chat Completions 传输，本地模型资料与文本计数不依赖 LiteLLM；其他服务商的具名前缀（如 `anthropic/`）继续使用 LiteLLM 元数据与计数，即使显式选择原生 OpenAI 兼容协议也保持这一路径。图片生成仍使用 LiteLLM。只有装配的主模型、辅助模型、视觉模型和子 Agent 模型都走原生 OpenAI 或 DeepSeek 模型路径，且未启用图片生成时，首个模型请求才完全不需要导入 LiteLLM。

图片在共享请求序列化阶段按 `providers/image_limits.py` 的内部策略处理，覆盖用户附件、Tool 图片及历史重发。DeepSeek Flash单边最多 8192 像素，请求包含至少 15 张图片时降为 4096；Claude 单边最多 8000 像素，超过 20 张时降为 2000。策略识别官方端点；未指定端点时使用模型路由，未知端点和未覆盖的服务保持原有发送行为。OpenAI、Gemini 等服务端的推理缩放尺寸不作为客户端上传限制。超限图片仅在内存中等比缩小，发送时附带原图尺寸和坐标换算；原始附件与 Journal 不变。编码缓存包含目标尺寸上限，token 估算使用发送尺寸且不解码图片。

所有视觉 Provider 在 token 估算和发送阶段都使用相同的格式探测和 5 MiB 字节上限检查。缺失图片以文本提示替代，不计图片 token；存在但为空、格式不可识别或超出字节上限的图片直接报错，不按 0 token 忽略，也不发送给上游。路径越界等调用错误同样直接报错。未知服务仅不应用尺寸缩放规则，仍受统一图片校验约束。

Responses 工具参数从 `response.completed` 的完整 output 读取，推理及加密状态通过消息的 `provider_data` 写入 Session Journal，后续请求原样回传；加密推理的 token 数使用服务端 usage 纳入上下文估算。Bridge 的呈现消息和 GUI 的会话读取响应不携带该状态。原生 SDK 流中断或协议异常会报错，不通过强制工具选择重新请求。

`ToolCallStreamAssembler` 负责把流式 Tool Call 分片组装为结构化参数，并在协议异常时提供不包含完整敏感参数的定位信息。

| 模块 | 职责 |
| --- | --- |
| `llm.py` | Provider 抽象、请求、响应与能力类型 |
| `providers/base.py` | 模型信息缓存、媒体与取消机制 |
| `providers/openai_models.py` | 原生 OpenAI 模型的本地上限与视觉能力 |
| `providers/deepseek_models.py` | DeepSeek 当前模型的本地上限与视觉能力 |
| `providers/openai_tokens.py` | 使用随包词表的本地请求 token 估算 |
| `providers/openai_provider.py` | 原生 Responses 与 Chat Completions 协议 |
| `providers/litellm_provider.py` | LiteLLM 文本和视觉适配 |
| `providers/image_limits.py` | 已知服务的图片上传尺寸限制 |
| `providers/reasoning.py` | `reasoning_effort` 与自定义推理参数的校验和路由映射 |
| `providers/tool_call_stream.py` | 流式 Tool Call 组装与校验 |
| `image_generation.py` | 图片生成抽象 |
| `providers/image_generation.py` | LiteLLM 图片生成实现 |
| `auxiliary.py` | 辅助模型调用的耗时、结果与用量统计 |

## Skill、MCP 与子 Agent

Core 只定义扩展机制，不决定扩展从哪个目录加载：

- `SkillSource` 描述可枚举的 Skill 来源，`SkillLoader` 与 `SkillRegistry` 负责元数据加载、注册及按需读取正文。
- `McpClientManager` 管理 stdio 与 streamable HTTP Server 生命周期，将远程 Tool 适配为统一 `McpTool`。
- `SubagentRoleRegistry` 保存角色；`SubagentRuntime` 为每次调用创建独立 Session，并复用同一套 Agent、Tool 和上下文机制。

相关模块为 `skills.py`、`mcp/` 和 `subagent.py`。

## Memory、Job 与 Scheduler

长期记忆分为全局和 Workspace 两层。`MemoryManager` 在 Turn 开始时提供记忆上下文，收集候选更新，并在成功结束后通过 `MemoryReconciler` 合并；失败或取消时丢弃待提交更新。

`JobManager` 管理可取消的后台任务和状态事件，供子 Agent 等长任务使用。Job 快照（原始详情、耗时、progress、错误与结果摘要）随 Journal 持久化，Turn 恢复后仍可由 `job_control` 查询；已恢复的中断 Job 不能再取消。

`SchedulerService` 定义 one-shot、interval、cron trigger，以及任务、运行记录、错过执行的处理和 JSONL 持久化。它通过注入的 runner 发起 Agent Turn，但不负责组装应用 Runtime。

相关模块为 `memory.py`、`jobs.py` 和 `scheduler.py`。

## Workspace Instructions

`workspace_instructions.py` 只定义 instruction 条目、合并结果和 fingerprint 数据结构。具体读取哪些文件、来源优先级以及何时触发 Runtime 重建，由 `agent_runtime` 决定。

## Hooks

`hooks.py` 定义与前端无关的执行检查点、不可变 Hook 声明和 command Hook 机制。Agent、ToolSet、ToolBatchExecutor 和 TurnContinuationPolicy 显式应用决策；内部命令 Tool 复用权限、执行路由、沙箱和取消。Plugin 来源解析与装配属于 Runtime。完整契约见 [Plugin Hooks](../docs/plugin-hooks.md)。
