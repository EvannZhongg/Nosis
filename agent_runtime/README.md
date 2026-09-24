# Agent Runtime

`agent_runtime` 是 Nosis 唯一的应用 Runtime 装配层。它读取应用配置和 Workspace 状态，把 [`agent_core`](../agent_core/README.md) 的机制组装成 Bridge 与定时任务可调用的运行实例，但不实现 Agent 决策逻辑。

## Runtime 入口

`RuntimeHost` 是共享入口，组合三个主要控制器：

```text
RuntimeHost
├── SessionRuntimeController   Session、Workspace、Provider、权限与计划
├── ExecutionPlaneManager     Agent、Tool、MCP、Memory、Job 等原子装配
└── TurnRunner                turn、steer、cancel 与结束结果
```

交互式 Bridge 和无人值守的 Scheduled Runner 都通过这套装配进入 Core。前端不自行创建 Agent、执行 Tool 或维护另一套授权状态。

## Execution Plane

`ExecutionPlane` 持有一次完整执行所需的对象：

- Workspace 和选定 Provider
- 已解析的 Agent 配置及配置 fingerprint
- Workspace Instructions 及其 fingerprint
- Agent、Tool Catalog / ToolSet 与执行路由
- Memory、MCP Client、后台 Job 和上下文窗口

`ExecutionPlaneManager.ensure()` 在首个 `user_turn` 到来时延迟装配。Workspace、Provider、配置或 Instructions 变化时，旧 plane 整体关闭并在下一次使用时重新创建，避免部分组件仍使用旧状态。

装配阶段负责：

- 创建主 Provider、视觉 Provider 和图片生成 Provider
- 建立宿主与平台沙箱执行器，并施加 Runtime authority 上限
- 从内置 Catalog 按主 Agent 和子 Agent 角色选择 Tool
- 注入 Session、Plan、Memory、Scheduler、权限、图片和用户交互能力
- 加载 standalone Skill、Plugin Skill、Plugin Agent 与 MCP Server
- 构造子 Agent 角色注册表和后台 Job Manager
- 启动 MCP 并汇总非致命 Runtime warning

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
| `agent_config.json` | Tool、上下文、记忆、子 Agent、MCP、Instructions 与 scratch Workspace |
| `.env` | `${ENV_NAME}` 引用的密钥，由 `settings.py` 读写 |
| `prompts/` | `Soul.md`、`SubAgent.md`、`Consolidator.md`、`GlobalMemory.md`、`WorkspaceMemory.md` |
| `skills/` | standalone Skill |
| `plugins/` | Plugin 安装目录 |
| `AGENTS.md` | 用户级全局 Workspace Instruction |
| `MEMORY.md` | 全局长期记忆 |

仓库内的 [`defaults/`](defaults) 是首次初始化时的安装源。已有配置、Prompt、Skill 或 Plugin 目录不会被默认资源覆盖。

Provider 路由按以下链解析：

```text
具体 subagent role → subagent 默认值 → main_agent
```

空字符串表示继续继承。图片生成使用独立 `image_generation` 配置；模型请求超时和额外重试次数来自 `agent_config.json` 的 `provider` 字段。

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

standalone Skill 与 MCP 使用同一套 Core registry / manager，不存在 Plugin 专用执行路径。

## 附件与定时任务

`attachments.py` 验证前端提交的附件确实位于当前 Workspace，探测图片格式，并转换为 Core 的 `ImagePart` 或 `FilePart`。

`ScheduledTurnRunner` 为 Scheduler 创建独立 `RuntimeHost`，以任务绑定的 Workspace 和 Provider 执行 Turn。`workspace` scope 使用 Workspace-only authority，`host` scope 使用 Host authority；两者都不提供交互式授权或用户提问能力，因此只能在预先声明的 scope 内无人值守执行。

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
| `callbacks.py` | Runtime 对宿主的回调接口 |
