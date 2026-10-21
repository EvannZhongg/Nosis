# Agent Runtime

`agent_runtime` 是 Nosis 唯一的应用 Runtime 装配层。它读取应用配置和 Workspace 状态，把 [`agent_core`](../agent_core/README.md) 的机制组装成 Bridge 与定时任务可调用的运行实例，但不实现 Agent 决策逻辑。

## Runtime 入口

`RuntimeHost` 是共享入口，组合三个主要控制器：

```text
RuntimeHost
├── SessionRuntimeController   Session、Workspace、Provider、权限与计划
├── ExecutionPlaneManager     Agent、Tool、MCP 连接、Memory、Job 等原子装配
└── TurnRunner                turn、steer、cancel 与结束结果
```

交互式 Bridge 和无人值守的 Scheduled Runner 都通过这套装配进入 Core。前端不自行创建 Agent、执行 Tool 或维护另一套授权状态。

## Execution Plane

`ExecutionPlane` 持有一次完整执行所需的对象：

- Workspace 与选定的 Provider 名称
- 已解析的 Agent 配置及配置 fingerprint
- Workspace Instructions 及其 fingerprint
- 已装配的 Agent（含其 Tool）与执行路由
- Memory、MCP Client Manager、桌面能力、后台 Job 和上下文窗口

`ExecutionPlaneManager.ensure()` 在首个 `user_turn` 到来时延迟装配。复用条件是 Workspace、Provider 名称、解析后的 Agent 配置、配置 fingerprint、Instructions fingerprint 与有效 MCP 配置全部一致；任一变化时旧 plane 整体关闭并在下一次使用时重新创建。关闭时由执行面关闭其 MCP 连接及 stdio 子进程。

空闲时可调用 `RuntimeHost.refresh_runtime()` 显式强制重新装配，即使上述输入未变化也会重建执行面，重新读取 Prompt、Skill、Plugin Agent，并重连 MCP、重新发现工具。该操作保留当前 Session、Journal、权限和 Plan，不调用模型、不创建 Turn；结果通过 `starting → idle/failed` 反馈。装配失败不会发布半成品，可修正后再次刷新；旧执行面关闭后的失败不恢复旧资源。正常 Turn 仍沿用原有变更检测。

装配阶段负责：

- 创建主 Provider、`fast_model` 对应的辅助模型 Provider、视觉 Provider 和图片生成 Provider
- 建立宿主与平台沙箱执行器，并施加 Runtime authority 上限
- 装配桌面截图与输入能力
- 从内置 Catalog 按主 Agent 和子 Agent 角色选择 Tool
- 注入 Session、Plan、Memory、Scheduler、权限、图片和用户交互能力
- 加载 standalone Skill、Plugin Skill、Plugin Agent 与 MCP Server
- 构造子 Agent 角色注册表和后台 Job Manager
- 启动 MCP Server 并汇总非致命 Runtime warning

## MCP 生命周期

每个 Execution Plane 创建自己的 Core `McpClientManager`，直接连接 stdio 或 streamable HTTP Server。Bridge 与定时任务各自通过 Runtime 装配 MCP；不同 Runtime 的连接与 stdio 子进程相互独立。

Manager 在后台线程中并发连接 Server，装配其他依赖后通过 `wait_ready()` 获取经过 allowlist 过滤的 `McpTool`。启动超时或失败仅将对应 Server 标记为 unavailable；工具名冲突仅拒绝冲突 Server 的整组工具，不留下部分注册结果。

主 Agent 与子 Agent 通过同一执行面的 Tool Catalog 和上下文调用 MCP，审批与取消仍走既有 ToolPolicy 和 Turn 控制。相同装配输入下切换 Session 可复用执行面；Provider、Workspace、配置、Instructions 或有效 MCP 配置变化时，旧执行面连同 MCP 连接一起关闭，随后重新启动和发现工具。

Server 初始化返回的 `instructions` 由 Manager 在成功注册时保存为执行面内的固定快照。主 Agent 与子 Agent 按实际选出的 MCP ToolSet 取得相关 Server 的完整说明，在创建 Agent 时追加到 system prompt 的 Workspace Instructions 与 Skill 目录之后、Memory 和压缩摘要之前。说明保留 Server 身份及原文，不另行列举工具名，不覆盖执行、授权或任务约束；无可见工具、无说明或注册失败的 Server 不注入。说明顺序不受连接完成顺序影响，不随 Turn、计划或 Jobs 更新，也不由历史压缩摘要替代。

stdio 的相对 cwd 基于 Workspace 解析，未配置时使用当前 Workspace；Plugin Server 的 cwd 由 PluginLoader 解析到 Plugin 根目录。streamable HTTP 直接使用当前 Runtime 的环境与配置 headers。

### 子 Agent 的 MCP 选择

`agent_config.json` 的 `subagent_roles.<role>.tools` 声明内置工具，独立的 `mcp` 字段声明 MCP 引用，默认 `{}`，不授予任何 MCP 工具：

```json
{
  "description": "Search documentation.",
  "tools": {"read_file": true},
  "mcp": {
    "analyzer": true,
    "docs": {"search": true},
    "review-kit:writer": {"write": false}
  }
}
```

服务器键使用完整身份（Plugin Server 为 `plugin:server`），工具键使用服务器返回的原始名，不使用模型可见的 `mcp__...` 限定名。服务器值为 `true` 时允许当前全部可用工具；`false` 或未声明时不允许。工具映射全部为 `true` 时是白名单，只允许列出的工具；全部为 `false` 时是黑名单，允许除列出工具外的其他工具。单个服务器的空映射、混用 `true`/`false` 或非 boolean 值均为配置错误。

配置解析只检查上述结构；Runtime 在 MCP 发现、上游 `tools.enabled` 过滤和完整 Catalog 装配后统一验证引用并展开。未知服务器、不可用的显式工具引用（含黑名单项）会报告对应角色的配置错误。配置中已知但禁用或不可用的服务器以 `true` 选择时得到空工具集；任何选择都不能绕过服务器启用状态和上游 allowlist。

展开结果按内置工具固定顺序、MCP 限定名字典序保存为执行面快照，再交给 Core 生成 ToolSet。白名单不会自动授予新增工具；黑名单和 `true` 会在下一次执行面重建时纳入服务器新增且上游允许的工具。复用同一执行面时，切换 Session 或执行 Turn 不会重新展开。

## Session 与 Turn 生命周期

`SessionRuntimeController` 在 `open_session` 时完成轻量控制面初始化：

- 创建或恢复 Session，并绑定 Workspace
- 选择显式、Session 已保存或默认的 Provider
- 连接 append-only Journal sink
- 恢复计划和权限 preset

此时不会创建 Provider、MCP、Tool 或 Agent。真正的执行面由首个 Turn 延迟创建。

`TurnRunner` 保证一个 Host 同时只有一个活动 Turn，并统一处理：

- 尚在排队或已经运行的 steer
- 排队取消、活动取消和 Host shutdown
- Runtime phase 回调
- 成功、取消、失败以及启动阶段失败的结构化结果
- 成功 Turn 后的长期记忆 reconcile

`RuntimeCallbacks` 是 Runtime 对宿主的窄接口，用于发布事件、phase、计划与 MCP 状态，以及请求授权或向用户提问。

## 配置与默认资源

`config.py` 负责初始化和读取 `~/.nosis/`：

| 路径 | 用途 |
| --- | --- |
| `provider_config.json` | Provider 定义，主 Agent、视觉、图片生成和角色路由 |
| `agent_config.json` | Tool、上下文、记忆、Skill 路径、子 Agent、MCP、Instructions 与 scratch Workspace |
| `tool_config.json` | Web Search、Web Fetch 的 provider 与密钥配置 |
| `.env` | `${ENV_NAME}` 引用的密钥，由 `settings.py` 读写 |
| `prompts/` | `Soul.md`、`SubAgent.md`、`Consolidator.md`、`GlobalMemory.md`、`WorkspaceMemory.md` |
| `skills/` | standalone Skill |
| `plugins/` | Plugin 安装目录 |
| `AGENTS.md` | 用户级全局 Workspace Instruction |
| `MEMORY.md` | 全局长期记忆 |

仓库内的 [`defaults/`](defaults) 是首次初始化时的安装源。已有配置、Prompt、Skill 或 Plugin 目录不会被默认资源覆盖。

`tool_config.json` 提供 `web_search` 和 `web_fetch` 两个配置段。搜索默认使用无需密钥的 `duckduckgo`，也可选择 `exa` 或 `deepseek`；抓取默认使用 `direct`，也可选择 `deepseek`：

```json
{
  "web_search": {
    "provider": "duckduckgo",
    "providers": {
      "exa": {"key": "${EXA_API_KEY}"},
      "deepseek": {"key": "${DEEPSEEK_API_KEY}"}
    }
  },
  "web_fetch": {
    "provider": "direct",
    "providers": {
      "deepseek": {"key": "${DEEPSEEK_API_KEY}"}
    }
  }
}
```

`key` 可以是明文或 `${ENV_NAME}` 引用，引用从进程环境（含 `~/.nosis/.env`）解析。只解析选中 provider 的密钥；密钥缺失、为空或请求返回 401 时，该次搜索降级到 DuckDuckGo，结果附带实际 `provider` 和 `fallback` 原因。降级时保留原始查询（包括已有的 `site:` 等操作符），Exa 的 `include_domains` / `exclude_domains` 按结果 URL 的主机名及其子域名过滤，不向查询拼接操作符。过滤后可能少于请求的结果数，不额外发起搜索。403 不视为鉴权失败，直接返回错误。其他请求错误直接返回，不重试、不切换 provider；降级本身失败也直接返回错误。配置或 `.env` 变化会在下次 Turn 重建执行面；主 Agent 和子 Agent 共用同一份解析后的 Tool 设置。工具是否启用仍由 `agent_config.json` 的 `main_agent.tools.web_search` 和对应角色开关控制。

DeepSeek Fetch 使用 [Fetch API](https://api-docs.deepseek.com/zh-cn/api/fetch/)，返回标题与 Markdown 正文，URL 最长为 8192 个字符。结果中的 `url` 是请求地址；API 不提供目标网页的 HTTP 状态码或重定向后的地址，因此不会生成这些信息。公开 URL 校验、响应字节上限与工具输出预算仍生效。密钥缺失、为空或返回 401 时，该次调用降级到 `direct`，结果包含结构化的 `fallback` 原因。Fetch 的 403 表示安全策略拒绝，直接返回错误；其他错误也不会触发降级或重试。两个工具分别配置 provider 和 key，可引用同一个 `DEEPSEEK_API_KEY`。`web_fetch` 的启用开关仍位于 `agent_config.json` 的主 Agent 和角色 Tool 配置中；`tool_config.json` 必须包含两个配置段。

`agent_config.json` 的 `skills.global_paths` 相对配置目录解析、`skills.workspace_paths` 相对当前 Workspace 解析。同名 Skill 保留最先注册的来源，Plugin Skill 排在配置来源之后。

Provider 路由按以下链解析：

```text
具体 subagent role → subagent 默认值 → main_agent
```

空字符串表示继续继承。`fast_model` 是独立的一路，只服务长期记忆合并，不参与上面的子 Agent 链；未配置时沿用当前选中的主模型，主 Agent 与子 Agent 的记忆合并共享这个 Provider。上下文压缩始终使用当前 Agent 自己的 LLM，复用原请求前缀。压缩和记忆调用直接写入所属 Session 的 JSONL Journal，记录实际模型、任务、状态、耗时和用量，不进入对话上下文；子 Agent 调用保存在子 Session 中。图片生成使用独立 `image_generation` 配置；模型请求超时和额外重试次数来自 `agent_config.json` 的 `provider` 字段。
每个 `provider_config.json` 的 `providers.<name>` 条目可以配置 `api: "responses"` 或 `api: "chat_completions"`，按协议选择原生 OpenAI SDK；与中转域名无关，也不在失败后切换协议。显式选择协议时，`model` 是原样发送的服务端模型 ID，例如 `gpt-6-astra` 或 `openai/gpt-5`（后者适用于要求该 ID 的网关）。省略 `api` 时，无路由前缀或使用 `openai/` 前缀的模型默认使用 Responses，移除路由前缀 `openai/`；其他带前缀的服务商路由使用 LiteLLM。协议选择不依赖联网模型目录。主 Agent、视觉和子 Agent 共用同一装配入口。

可选的 `reasoning_effort` 值为 `none`、`minimal`、`low`、`medium`、`high`、`xhigh` 或 `max`；具体模型只支持其中的子集。原生 Responses 发送 `reasoning.effort`，原生 Chat Completions 发送 `reasoning_effort`，其他路由保留其参数映射。省略该字段时使用上游默认值。`reasoning_parameters` 可以覆盖推理参数，`extra_body` 用于网关扩展字段，例如 `{"extra_body": {"enable_thinking": true}}`；原生 Chat 的额外推理字段也通过 SDK 的扩展 body 发送。

## 设置服务

`SettingsStore` 是配置读取和写入的统一应用服务：

- 生成已脱敏的 Provider、路由、Agent、Skill、Plugin、Agent component 和 MCP 快照
- 校验并原子写入 Provider、路由和 Agent 配置
- 把 API key 写入 `.env`，不在快照中返回正文
- 使用 revision 防止基于旧快照覆盖新配置
- 计算配置 fingerprint，供 Execution Plane 判断是否失效

GUI HTTP API 和 Bridge 设置消息都复用该服务。

## Workspace Instructions

`instructions.py` 在每个执行面检查点读取：

1. `~/.nosis/AGENTS.md`
2. `agent_config.json.workspace_instruction_files` 指定的 Workspace 根目录文件

默认 Workspace 文件为 `CLAUDE.md` 和 `AGENTS.md`。来源按上述顺序从高到低排列；只读取 Workspace 根目录，不递归子目录，也不向父目录搜索。

内容进入主 Agent 与子 Agent 的 system context，不写入会话 transcript，也不参与历史压缩。文件内容或配置列表变化会改变 fingerprint，并触发 Execution Plane 重建。

## Plugin 与扩展发现

`PluginManager` 从 `~/.nosis/plugins/<plugin-name>/plugin.json` 发现 Plugin。Plugin 是分发单元，不建立新的执行系统；其组件被加 namespace 后交给已有 Skill、MCP 和子 Agent 子系统。

```json
{
  "name": "example",
  "version": "1.0.0",
  "description": "Example capability package",
  "capabilities": ["skills", "mcp", "agents"],
  "components": {
    "skills": ["skills"],
    "mcp": [".mcp.json"],
    "agents": ["agents/reviewer.md"]
  }
}
```

- Skill 注册为 `<plugin>:<skill>`，每个直接子目录使用标准 `SKILL.md`。
- MCP Server 保存为 `<plugin>:<server>`；是否加载仍受 `agent_config.json.mcp.enabled` 总开关控制。
- Agent component 使用 YAML frontmatter 声明 `name`、`description`，可选 `tools` 和 `model`；注册为 `<plugin>:<agent>`。
- Agent 的 `tools` 保留内置角色工具，并可显式选择完整 MCP 工具名（例如
  `mcp__example_analyzer__analyze_code`）；省略时仍只使用原有内置角色工具。
  Runtime 在 MCP 发现与 allowlist 过滤后校验引用，缺失工具仅跳过受影响角色并报告来源。

standalone Skill 与 MCP 使用同一套 Core registry / manager，不存在 Plugin 专用执行路径。
装配、静态校验、错误隔离与文件变更的完整边界见 [Plugin 组件互通](../docs/plugin-interop.md)。

## 附件与定时任务

`attachments.py` 验证前端提交的附件确实位于当前 Workspace，探测图片格式，并转换为 Core 的 `ImagePart` 或 `FilePart`。

`ScheduledTurnRunner` 为 Scheduler 创建独立 `RuntimeHost`，在任务绑定的 Workspace 中执行 Turn；Provider 沿用 Session 已保存或默认的选择。`workspace` scope 使用 Workspace-only authority，`host` scope 使用 Host authority；两者都不提供交互式授权或用户提问能力，因此只能在预先声明的 scope 内无人值守执行。

## 模块索引

| 模块 | 职责 |
| --- | --- |
| `host.py` | RuntimeHost 公共入口和控制器协调 |
| `execution_plane.py` | 一次原子装配的资源容器 |
| `plane_manager.py` | Execution Plane 创建、复用、失效与关闭 |
| `session_controller.py` | Session、Workspace、Provider、权限与计划绑定 |
| `turn_runner.py` | Turn、steer、cancel 和结果 |
| `scheduled_runner.py` | 无人值守定时 Turn 入口 |
| `config.py` | 配置目录初始化、Provider 与 Prompt 读取 |
| `settings.py` | 配置快照、校验、写入和 fingerprint |
| `plugins.py` | Plugin manifest、Skill、MCP 与 Agent component 发现 |
| `instructions.py` | Workspace Instructions 来源读取与 fingerprint |
| `attachments.py` | 入站附件解析 |
| `computer.py` | 桌面截图与输入的平台能力 |
| `callbacks.py` | Runtime 对宿主的回调接口 |
