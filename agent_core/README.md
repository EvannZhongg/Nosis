# Agent Core

`agent_core` 是与 TUI、GUI 无关的 Agent Runtime，负责模型调用、上下文管理、Tool 执行、Session 状态、事件输出、授权和取消。前端通过 [`interfaces/bridge`](../interfaces/bridge/README.md) 驱动它，不应在界面层实现另一套 Agent Loop。

## 配置文件

首次启动会在 `~/.nosis/` 生成 `provider_config.json` 和 `agent_config.json`。密钥可放在同目录的 `.env` 中；这些文件和 Session 数据不会提交到仓库。

`agent_config.json` 控制 Runtime：

```json
{
  "max_same_tool_calls": 5,
  "max_output_tokens": 8192,
  "shell_timeout_seconds": 60,
  "main_agent": {
    "tools": {
      "read_file": true,
      "edit_file": true,
      "search_files": true,
      "list_directory": true,
      "shell": true,
      "web_search": false,
      "analyze_image": true
    }
  }
}
```

常用字段：

| 字段 | 说明 |
| --- | --- |
| `max_same_tool_calls` | 单轮内完全相同 Tool Call 的连续次数上限 |
| `max_output_tokens` | 每次回答预留的输出 token 数；其余为模型 hard limit |
| `context.compression` | 可选上下文压缩设置：`enabled`、`trigger_ratio`、`target_ratio`；`target` 必须小于 `trigger` |
| `shell_timeout_seconds` | shell 默认超时，默认 60 秒、上限 900 秒 |
| `main_agent.tools` | 主 Agent 的内置 Tool 开关；显式写 `true` 才启用 |
| `subagent_roles` | 子 Agent 角色表；每个角色有 `enabled`、`description` 和自己的 `tools` |

### Provider

`provider_config.json` 用 `main_agent.provider` 选择主 Provider，`subagent.provider` 可单独指定子 Agent（空字符串表示复用主 Agent）：

```json
{
  "main_agent": {"provider": "deepseek", "vision_provider": "gemini"},
  "subagent": {"provider": "", "vision_provider": ""},
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

`key` 支持直接填写或 `${ENV_NAME}`；Ollama 等无密钥 Provider 可以省略。`max_context_tokens` 省略时从 LiteLLM 元数据读取，元数据缺失时必须填写。视觉能力依据 LiteLLM 的 `supports_vision` 自动识别；主模型不支持视觉且未指定覆盖时，Runtime 会从已配置且凭据可用的视觉 Provider 中选择第一个。需要时可用 `main_agent.vision_provider` 覆盖自动选择。Subagent 默认继承主 Agent 已解析的视觉 Provider，也可以通过 `subagent.vision_provider` 独立指定；显式指定的视觉 Provider 会在启动时校验图片输入能力。OpenAI 兼容服务的 `model` 需要带 LiteLLM 接口前缀，例如 `openai/glm-5.3`。

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

`read_file`、`edit_file`、`search_files`、`list_directory`、`shell`、`web_search`、`analyze_image` 和 `subagent` 构成共享的 Tool Catalog。Catalog 中的 Tool 实例无状态，由 Runtime 内所有 Agent 共用；每个角色按名字从 Catalog 中筛选出自己的 ToolSet，不重复创建实例。Runtime 依赖（Workspace、Command Executor、Session 根目录、视觉 Provider、MCP、子 Agent Runtime）通过 `ToolExecutionContext` 在调用时传入；缺少依赖的 Tool 不会出现在 ToolSet 中，而不是在调用时报错。文件 Tool 只接受 Workspace 内的相对路径；`search_files` 默认跳过超大文件、非文本文件以及 `.git`、`node_modules`、`build` 等目录。`shell` 每次执行前需要授权，并返回退出码、标准输出、标准错误和超时信息。`web_search` 默认关闭，通过 [Exa](https://exa.ai) 检索公网内容，单次最多返回 10 条结果，启用时需要 `EXA_API_KEY`（可在 [Exa Search](https://exa.ai/products/search) 申请）。单个 Tool Result 回灌模型最多 16K 字符，较大的结果会保存为 Session Artifact。

## 子 Agent 角色

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

每轮对话都会追加到 `~/.nosis/sessions/<WORKSPACE_KEY>/<SESSION_ID>/<SESSION_ID>.jsonl`，完整上下文、Tool Result 和模型响应均可恢复；被取消或失败的轮次只写入已经产生的 items，因此中断的对话同样可以恢复。同一 Workspace 下的 Session 在 GUI 中分组显示，Session 是否存在以其 `<SESSION_ID>.jsonl` 为准，Workspace 目录的 `workspace.json` 记录该分组的绝对路径。修改 Workspace 会将 Session 目录移动到新的 Workspace 分组。超过回灌上限的 Tool Result 保存为同目录下的 `<TOOL_CALL_ID>.txt`；使用 `nosis --session SESSION_ID` 恢复。子代理的子会话转录写在父 Session 的 `subagents` 目录下，因此不会出现在会话列表中，但仍可查阅。

GUI 上传的图片保存在 Workspace 的 `.nosis/attachments/<id>.<ext>`，Session 只记录路径和 MIME 类型。Runtime 请求 Provider 时才读取图片，并通过 `LLMRequest(media_root=workspace.path)` 解析工作区路径。
