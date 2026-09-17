# Agent Core

`agent_core` 是与 TUI、GUI 无关的 Agent Runtime，负责模型调用、上下文管理、Tool 执行、Session 状态、事件输出、授权和取消。前端通过 [`interfaces/bridge`](../interfaces/bridge/README.md) 驱动它，不应在界面层实现另一套 Agent Loop。

## 配置文件

首次启动会在 `~/.nosis/` 生成 `provider_config.json` 和 `agent_config.json`。密钥可放在同目录的 `.env` 中；这些文件和 Session 数据不会提交到仓库。

`agent_config.json` 控制 Runtime：

```json
{
  "max_same_tool_calls": 5,
  "output_reserve_tokens": 8192,
  "max_generation_tokens": null,
  "main_agent": {
    "tools": {
      "read_file": true,
      "edit_file": true,
      "write_file": true,
      "search_files": true,
      "list_directory": true,
      "shell": true,
      "web_search": false,
      "subagent": true
    }
  }
}
```

常用字段：

| 字段 | 说明 |
| --- | --- |
| `max_same_tool_calls` | 单轮内完全相同 Tool Call 的连续次数上限 |
| `output_reserve_tokens` | 为下一次模型输出预留的上下文空间，只参与输入 hard limit 和压缩阈值计算 |
| `max_generation_tokens` | 可选的单次生成策略上限；默认 `null` 时不向 Provider 传生成上限，显式设置时由模型最大输出能力裁剪 |
| `context.compression` | 可选上下文压缩设置：`enabled`、`trigger_ratio`、`keep_recent_units`；达到输入 hard limit 的指定比例时，将最近 K 个完整 ContextUnit 之前的内容归档为 checkpoint |
| `main_agent.tools` | 主 Agent 的内置 Tool 开关；显式写 `true` 才启用 |
| `subagent_roles` | 子 Agent 角色表；每个角色有 `enabled`、`description` 和自己的 `tools` |

### Provider

`provider_config.json` 用 `main_agent.provider` 选择主 Provider，`subagent.provider` 设置子 Agent 的默认值，`subagent_roles.<角色>` 可为单个角色单独指定：

```json
{
  "main_agent": {"provider": "deepseek", "vision_provider": "gemini"},
  "subagent": {"provider": "", "vision_provider": ""},
  "subagent_roles": {
    "coder": {"provider": "anthropic"}
  },
  "providers": {
    "deepseek": {
      "model": "deepseek/deepseek-chat",
      "url": "https://api.deepseek.com",
      "key": "${DEEPSEEK_KEY}",
      "max_context_tokens": 1048576
    }
  }
}
```

`provider` 和 `vision_provider` 都按 `subagent_roles.<角色>` → `subagent` → `main_agent` 逐级回退，空字符串表示继承上一级。因此一个角色可以只覆盖模型而继续共用视觉 Provider，反之亦然。`subagent_roles` 中出现 `agent_config.json` 里不存在的角色名会启动失败，避免两个文件不一致。

`key` 支持直接填写或 `${ENV_NAME}`；Ollama 等无密钥 Provider 可以省略。`max_context_tokens` 省略时从 LiteLLM 元数据读取，元数据缺失时必须填写。OpenAI 兼容服务的 `model` 需要带 LiteLLM 接口前缀，例如 `openai/glm-5.3`。

### 图片与 vision_provider

模型是否支持图片由 LiteLLM 的 `supports_vision` 自动识别，据此决定图片怎么走，无需配置：

* 模型支持图片 → 图片直接内联进对话，并自动注册 `read_image`，模型可以自己打开 Workspace 里的图片；
* 模型不支持、且解析出 `vision_provider` → 自动注册 `analyze_image`，模型可以把图片交给该 Provider 分析；
* 模型不支持、且没有 `vision_provider` → 两个工具都不注册，提示里也不会出现它们。

`read_image` 和 `analyze_image` 互斥，永远只注册其中一个：前者把像素交给模型自己看，后者用第二次 Provider 调用把像素换成文字。两者都不出现在 `tools` 开关中，由上述规则推导。

`vision_provider` 必须显式配置，不会自动从已配置的 Provider 中挑选：把用户的图片发给第二个 Provider 涉及隐私和成本，不适合由 Runtime 推断。省略即表示该 Agent 没有视觉降级通道。显式指定的 `vision_provider` 会在启动时校验图片输入能力。

图片进入上下文有两条通路：用户在输入框里上传的附件，以及模型自己调用 `read_image` 读到的文件。后者的像素无法放进 `tool` 消息（OpenAI 兼容的 Chat API 不接受），因此 Runtime 在一批 Tool Result **全部写入之后**追加一条 `origin="tool_media"` 的 `user` 消息来承载它们——一批工具调用最多追加一条，否则会在 `assistant` 的 tool_calls 和它的结果之间插入消息，Provider 会直接拒绝。这条消息不是用户说的话：它不会被子 Agent 当作用户附件继承，UI 也会把它并入助手的回复而不是渲染成一个用户气泡。

图片在被归档进 checkpoint 之前一直内联；归档后历史里只留下路径占位符，所以只有最近 `context.compression.keep_recent_units` 个单元内的图片会在后续每次模型调用里反复上传。计数不走 base64：图片按像素尺寸估算 token（只读文件头，不读整个文件），编码结果按 `(路径, mtime, 大小)` 缓存，一个 turn 里多次模型调用只编码一次。

token 估算刻意取各家计价模型的**上界**（固定 tile 与按面积两种口径取大），而不是取平均：保留区内的图片仍会内联，低估会让 Runtime 以为还有余量而不触发压缩，最终被 Provider 直接拒绝——那是一个不透明的上游错误，而压缩检查存在的意义正是把它变成可控失败。高估只是稍微提前压缩。无法从文件头读出尺寸时按上界计价，而不是按某个中间值。

单张图片上限 5 MiB（仅约束送进模型的图片；GUI 展示不受此限），媒体类型按文件 magic bytes 识别而不是按扩展名，`read_image` 单次最多读 4 张、并在路径解析之后去重（`a.png`、`./a.png`、`b/../a.png` 只算一张）。

### MCP

在同一 `agent_config.json` 的 `mcp` 字段配置 MCP Server，支持 `stdio` 和 `streamable_http`。`transport` 可以显式填写；省略时，有 `command` 的 Server 推断为 `stdio`，有 `url` 的 Server 推断为 `streamable_http`，两者同时存在或同时缺失时拒绝配置。HTTP Server 使用 `url`，请求头中的 `${ENV_NAME}` 从环境变量读取。启用后工具以 `mcp__服务器名__工具名` 注册。

每个 Server 的 `tools.enabled` 用于限制暴露给模型的远程工具；省略时暴露全部工具，空数组表示不暴露工具。`tools.approval` 可以省略，默认按 `"always"` 处理，即每次调用都请求人工确认；`"never"` 表示无需确认；也可以用 `{"always": ["工具名"]}` 仅确认指定工具。配置采用严格校验，传输方式不匹配、未知字段或缺少必填字段都会启动失败。

```json
{
  "mcp": {
    "enabled": true,
    "servers": {
      "filesystem": {
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "."],
        "cwd": ".",
        "tools": {
          "enabled": ["read_file", "write_file"],
          "approval": {
            "always": ["write_file"]
          }
        }
      }
    }
  }
}
```

## 内置 Tool

`read_file`、`edit_file`、`write_file`、`search_files`、`list_directory`、`shell`、`web_search`、`subagent`、`ask_user`、`read_image`、`analyze_image` 和 `read_skill` 构成共享的 Tool Catalog。Catalog 中的 Tool 实例无状态，由 Runtime 内所有 Agent 共用；每个角色按名字从 Catalog 中筛选出自己的 ToolSet，不重复创建实例。`ask_user` 由 Bridge 注入交互能力，只注册给 main Agent，用于暂停当前 Tool 调用并向 TUI/GUI 展示选项或接收自由输入，用户回答随后作为结构化 Tool Result 回灌模型。Runtime 依赖（Workspace、Command Executor、Session 根目录、模型是否能读图、视觉 Provider、MCP、子 Agent Runtime、Job Manager、Skill Registry、用户交互回调）通过 `ToolExecutionContext` 在调用时传入；缺少依赖的 Tool 不会出现在 ToolSet 中，而不是在调用时报错。文件 Tool 只接受 Workspace 内的相对路径；`read_file` 和 `read_image` 另外接受 `.nosis/sessions/...` 形式的 Session Artifact 路径，两者共用同一个路径解析器。`write_file` 以 UTF-8 整文件内容写入：目标不存在时创建，目标存在时必须显式设置 `overwrite=true`（否则应使用 `edit_file`），并通过同目录临时文件、flush/fsync、原子替换完成落盘。`search_files` 默认跳过超大文件、非文本文件以及 `.git`、`node_modules`、`build` 等目录。Runtime 的 `PermissionController` 持有 Session 级 `PermissionPreset`：`ask_for_approval` 继续通过既有 Shell/MCP policy 请求人工确认，`full_access` 仅跳过这层确认，不改变 ToolSet，也不引入 Sandbox，命令仍由当前 Host 上的 `SubprocessCommandExecutor` 执行。Shell 的命令流先写入临时 spool，返回有界的头尾预览；如果结果过大，Runtime 会把完整结果保存为 Session Artifact，模型可通过 `read_file` 分段读取。`web_search` 默认关闭，通过 [Exa](https://exa.ai) 检索公网内容，单次最多返回 10 条结果，启用时需要 `EXA_API_KEY`（可在 [Exa Search](https://exa.ai/products/search) 申请）。单个 Tool Result 回灌模型最多 16K 字符，较大的结果会保存为 Session Artifact。

`shell` 与 `subagent` 接受可选的 `background=true`。后台调用的 Tool Result 立即返回 `{job_id, kind, status: "submitted"}`，真实工作由 Runtime 的 `JobManager` 继续执行，因此不会阻塞当前 Tool Batch。Job 属于创建它的 turn：Agent 可继续模型调用和其他 Tool；如果模型先给出文本而 Job 尚未完成，turn 会保持活动并等待结果。完成、失败或取消结果只在完整 Tool Batch 之外的安全点以 `origin="job_result"` 的 Runtime 消息写入上下文，再触发下一次模型调用。取消 turn 会取消其全部 Job；Shell 会终止进程组，子 Agent 会收到独立取消信号。进程恢复时未完成 Job 标记为 `interrupted`，不会自动重放。

Skills 放在 Provider 配置文件同目录的 `skills/<目录>/SKILL.md`；默认配置下即 `~/.nosis/skills/<目录>/SKILL.md`。Runtime 启动时只把已注册 Skill 的 front matter `name` 和 `description` 放进 system prompt；完整说明由派生 Tool `read_skill(name, path="SKILL.md", offset=1, limit=2000)` 按行渐进读取，`path` 也可指向该 Skill 目录内引用的其他 UTF-8 文本文件。没有发现 Skill 时不注册 `read_skill`。Skill 默认注册；可在 front matter 中用 `metadata: {nosis: {register: false}}` 禁用。无法读取、front matter 非法或字段无效的 Skill 会被跳过并输出警告，不影响其他 Skill 和 Runtime 启动；同名 Skill 按目录名排序保留第一个，其余跳过并警告。

## 子 Agent 角色

并行 Tool Batch 明确区分四种顺序：

* **Invocation order**：模型发出 Tool Call 的顺序；`ToolCallEvent` 按此顺序输出。
* **Completion order**：Tool 实际执行完成的顺序；`ToolResultEvent` 按此顺序立即输出，避免较快的调用等待较慢的前序调用。
* **Journal order**：Tool 的开始与实际完成按发生顺序立即写入 Journal，不等待前序调用。
* **Provider projection order**：下一次模型调用从 Journal 投影合法消息，并把已完成结果恢复为 Invocation order。

因此模型发出 `A → B → C`、实际完成 `B → C → A` 时，UI 与 Journal 都立即记录 `B → C → A`；Provider Conversation 则投影为 `A → B → C`。真实执行顺序不会为了满足 Provider 格式而被改写。

`subagent` 是唯一的委派入口，调用形式为 `subagent(role, task)`。角色在 `agent_config.json` 的 `subagent_roles` 中定义，角色名会写进 Tool Schema 的 `role` 枚举，新增角色只需加一段配置：

```json
{
  "subagent_roles": {
    "researcher": {
      "enabled": true,
      "description": "Read and search the workspace to answer a question. Cannot modify files.",
      "tools": {"read_file": true, "search_files": true, "list_directory": true}
    }
  }
}
```

`enabled` 可以省略，默认为 `true`；写 `false` 时该角色保留在配置里但不会提供给模型。角色的 `tools` 不接受 `subagent`：子 Agent 的 Context 不携带子 Agent Runtime，因此无法再次委派。每次委派都会创建独立的 Session，父 Agent 只收到子 Agent 的最终报告。同一批 `subagent` 调用并行执行，共享的 Tool 实例不持有任何调用状态。

## Session 与附件

Session 文件是 append-only Runtime Journal。每条 JSONL record 都有单调递增的 `seq`、唯一 `event_id`、`turn_id`，Tool 事件另有 `tool_call_id`；turn、message、model call、context archive 以及 Tool 的 started/completed/failed/cancelled/unknown 状态均按发生顺序逐条 flush/fsync。Journal 不保存完整 Provider request、system prompt 或重复的 Tool schema。

Execution History 直接由 Journal replay；Provider Conversation 由独立 projection 构建。未完整配对的 Tool batch 仍保留在真实历史中，但不会进入下一次模型请求。进程异常退出后，尚在 running/started 的 Turn 与 Tool 会恢复为 unknown，可能已产生副作用的 Tool 不会自动重放；正常取消与失败则分别保留 cancelled/failed 状态。JSONL 最后一条若因进程退出而只写入一部分，恢复时以前一条完整且已 fsync 的 record 为准。

上下文压缩以 `ContextUnit` 为边界，而不是以 turn 或裸 Message 为边界。普通消息各自构成单元；包含 Tool Call 的 assistant 消息、该批次的全部 Tool Result 和尾随 Tool Media 共同构成一个不可拆分单元。触发压缩时，最近 `context.compression.keep_recent_units` 个完整单元保留原文，更早的单元合并进 checkpoint，因此归档 cursor 可以在仍执行的 turn 内推进，但不会落到 Tool batch 中间。

模型上下文分为三层：当前 turn 与上一 turn 的用户原文和交互决策作为 `Lossless User Anchors` 保留；更早的目标、结论和工作状态进入 `Archived Context Summary`；最近 `context.compression.keep_recent_units` 个 `ContextUnit` 作为原始 tail 保留。仍在 tail 中的用户消息不会重复写入 anchors。用户 steering、`ask_user` 回答和审批响应都归入对应 turn 的 anchors，因此 active turn 内压缩不会丢失这些输入。

运行中用户 steering 在安全点作为普通 `user` Message 追加，因此它自然构成新的 `ContextUnit`，沿用同一套 token 统计、hard limit 与压缩保留规则，不维护额外的上下文计数。

Session 位于 `~/.nosis/sessions/<WORKSPACE_KEY>/<SESSION_ID>/`，其中 `<SESSION_ID>.jsonl` 是运行日志，`session.json` 保存该 Session 的权限 preset。Session ID 来自目录名，Workspace 路径由可逆的 `<WORKSPACE_KEY>` 取得，不在配置中重复保存。修改 Workspace 会将整个 Session 目录移动到新的 Workspace 分组。超过回灌上限的 Tool Result 保存为同目录下的 `<TOOL_CALL_ID>.txt`；TUI 用 `/sessions` 列出当前 Workspace 的 Session 并切换，GUI 直接读取同一份存储。子代理的 Journal 写在父 Session 的 `subagents` 目录下，因此不会出现在会话列表中，但仍可查阅。

GUI 上传的图片保存在 Workspace 的 `.nosis/attachments/<id>.<ext>`，Session 只记录路径和 MIME 类型。Runtime 请求 Provider 时才读取图片，并通过 `LLMRequest(media_root=workspace.path)` 解析工作区路径。
