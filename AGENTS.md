# AGENTS.md
## 1. 总体原则

* 保持架构简单、清晰、可扩展，优先解决当前明确需求。
* 不进行未经要求的功能扩展、兼容处理或过度抽象。
* 不因潜在需求提前增加复杂的框架、配置层、防御逻辑或中间层。
* 修改应聚焦任务本身，不顺带重构无关代码。
* 优先复用现有实现，避免重复实现相同能力。
* 重构过程无需考虑旧数据兼容，禁止为了旧实现加兼容层。

## 2. 架构要求

```text
TUI (Ink + React) ──────┐
                        ├─── Bridge ── agent_runtime ── Agent Core
GUI (React) ── FastAPI ─┘
```

`agent_runtime` 是共享的应用 Runtime；Bridge 以 `python -m interfaces.bridge` 作为独立进程运行，由前端宿主启动，是前端与 Runtime 之间的唯一通道，两端用 newline-delimited JSON 交换协议消息。

* 依赖方向单向：`interfaces/* → agent_runtime → agent_core`；`agent_core` 不得 import `interfaces` 或 `agent_runtime`，`agent_runtime` 不得 import `interfaces`。
* 驱动 Agent、执行 Tool、发起审批只能经由 Bridge 协议；前端不得运行 Agent Loop，也不得执行会改变 Workspace 或 Session 的 Tool。
* 前端对 Core 的复用仅限只读能力（如 Session 读取、Workspace 路径解析、只读 Tool 的文件列举）；执行语义不得在前端复制。
* TUI 和 GUI 仅作为交互层，共享同一套 Agent Runtime。
* 授权、取消、Session 与 Tool 执行语义只实现一次，不得由两个前端各维护一套。
* 保持清晰的依赖方向，避免模块间循环依赖。

## 3. Agent Core 与 Bridge

新增能力的落点由职责决定：

* 影响所有前端共享的执行语义 → `agent_core`。
* 只决定选择、装配与协议 → `agent_runtime` 与 `interfaces/bridge`（Bridge 进程自身的生命周期由前端宿主管理，见 §2）。
* 只影响单个前端的呈现与交互 → 对应前端；不得反向进入 Core。

### 3.1 Agent Core：机制

`agent_core` 提供与前端无关、可被复用的机制；它不知道前端存在，也不知道配置与进程从何而来：

* Agent Loop、模型调用、错误处理与终止条件
* 上下文管理与上下文压缩
* Tool 调用与结果回灌；超出预算的输出落盘为 Session Artifact
* Session 状态与 append-only JSONL Journal，含权限 preset、Provider 与当前计划
* 执行事件输出
* Tool 抽象、Tool Catalog 与调用时依赖注入
* Shell 进程执行的超时、输出上限与 spool
* Skill 加载（`SkillSource` 与 Registry；Core 不决定 Skill 目录来源）
* 计划状态与跨 Turn 的计划恢复
* 后台 Jobs 与 Turn 控制（steer、cancel 的语义与终止条件）
* 媒体与附件处理（图片探测、降级分析）
* Workspace Instruction 的数据类型与 fingerprint（读取时机与来源顺序由 `agent_runtime` 决定）
* 子 Agent Runtime 与角色注册表
* Provider 抽象、MCP 客户端机制

约束：

* Agent Loop 应保持显式、可理解和可测试，不隐藏关键执行流程。
* 机制通过已有稳定接口暴露；不引入只服务某个前端的参数、事件或分支。
* Core 不感知 TUI/GUI、终端、浏览器或协议消息。

### 3.2 agent_runtime：Runtime 装配

`agent_runtime` 是唯一把 Core 拼装成可运行 Runtime 的地方，负责：

* 选择 Provider，读取 `provider_config.json` 与 `agent_config.json`，并处理配置写入
* 初始化配置目录、安装内置 Skill 与 Plugin、读取 Prompt 模板与 Workspace Instructions
* 组装 Tool Catalog、按角色 select、安装 `ToolPolicy`
* MCP Server 生命周期
* 发现 Plugin：把其声明的 Skill、MCP 与 Agent 组件加 namespace 后交给对应子系统
* 子 Agent 角色注册表
* Workspace 绑定、Session 存储位置、取消与持久化时机
* 一次装配由一个 `ExecutionPlaneManager` 原子持有；Provider、Workspace、配置或 Instructions 变化时整体重建
* Turn 生命周期（含取消与 steer）与定时任务的执行入口

约束：

* `agent_runtime` 不含 Agent 决策逻辑：Agent Loop、上下文压缩、结果回灌都属于 Core。
* Runtime 装配只有一处实现；TUI 与 GUI 不得各建一套。前端只决定进程与附着（何时启动、接管或释放 Bridge），不参与 Runtime 装配。

### 3.3 Bridge：协议适配

Bridge 把 `AgentEvent` 与运行状态翻译为协议消息，并转发用户输入、授权、提问与取消，不持有装配工厂。

* 协议消息的增改必须同时更新 `interfaces/protocol/src/protocol.ts` 与发送方：`protocol.py` 定义大部分消息，其余由 `bridge.py` 与 `__main__.py` 内联发出。
* 允许存在只服务单个前端的协议消息（如 `attach_only`、`takeover`、`attachment_replaced`），但它们只表达呈现与进程附着，不得承载 Agent 语义。

## 4. Tool 系统

* 所有工具使用统一接口和注册机制。
* Tool 本身只负责自身能力，不包含 Agent 决策逻辑。
* Shell、文件修改等高风险能力必须经过权限控制层。
* 不允许模型绕过 Tool 系统直接执行系统操作。
* Tool 的输入、输出和错误应使用结构化数据。
* 输出超出预算的 Tool 由 Runtime 落盘为 Session Artifact，前端不参与截断或存储决策。

## 5. TUI 与 GUI

* 前端只负责渲染协议消息与采集输入，不得在其中实现或改写 Agent 行为。
* Agent 状态必须由结构化事件驱动，不得从自由文本中解析；如何渲染由各前端自行决定。
* GUI 应重点展示对话、Tool 执行、状态和人工确认。

## 6. Session 与状态

* 对话和执行过程应围绕 Session 管理。
* Session 以 append-only JSONL Journal 保存对话、执行事件与当前计划。
* Session 状态不得依赖某个具体前端。
* TUI 与 GUI 应能够读取和恢复同一类 Session。
* 持久化数据与运行时对象分离。

## 7. 代码要求

* 优先小模块、明确职责和显式接口。
* 避免无必要的设计模式和层级包装。
* 不创建仅有一次调用、没有实际抽象价值的 wrapper。
* 不为了“未来可能使用”添加当前没有消费者的接口。
* 不保留无用兼容代码、废弃实现或重复逻辑。
* 删除功能时直接删除相关实现，不添加解释性残留代码。
* 注释解释必要的设计原因，不记录开发过程、争论过程或已删除方案。

## 8. 开发行为约束

实现需求时：

1. 先理解现有架构和调用链。
2. 找到最小且正确的修改范围。如有更优的解决方案可以重构相关模块而不是只做最小补丁。
3. 优先修改已有实现，而不是新增平行实现，重构功能时不必兼容旧实现。
4. 完成后检查是否引入重复逻辑、无用抽象或额外功能。
5. 执行相关测试，并修复由本次修改直接导致的问题。

除非明确要求，否则不要：

* 改变无关 UI 或文案。
* 增加新的配置项。
* 增加旧版本兼容逻辑。
* 增加 fallback、retry 或 defensive behavior。
* 新建额外服务或框架层。
* 扩大任务范围。
* 将开发要求、内部约束或实现说明展示给最终用户。
