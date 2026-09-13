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
```

常用字段：

| 字段 | 说明 |
| --- | --- |
| `max_same_tool_calls` | 单轮内完全相同 Tool Call 的连续次数上限 |
| `max_output_tokens` | 每次回答预留的输出 token 数；其余为模型 hard limit |
| `context.compression` | 可选上下文压缩设置：`enabled`、`trigger_ratio`、`target_ratio`；`target` 必须小于 `trigger` |
| `shell_timeout_seconds` | shell 默认超时，默认 60 秒、上限 900 秒 |
| `tools` | 内置 Tool 开关；显式写 `true` 才启用 |

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

在同一 `agent_config.json` 的 `mcp` 字段配置 MCP Server，支持 `stdio` 和 `streamable_http`。HTTP Server 使用 `url`，请求头中的 `${ENV_NAME}` 从环境变量读取。启用后工具以 `mcp__服务器名__工具名` 注册；`tool_allowlist` 可限制暴露的工具，`approval: "prompt"` 会在调用前请求人工确认。配置采用严格校验，传输方式不匹配、未知字段或缺少必填字段都会启动失败。

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
        "tool_allowlist": ["read_file"],
        "approval": "prompt"
      }
    }
  }
}
```

## 内置 Tool

`read_file`、`edit_file`、`search_files`、`list_directory`、`shell`、`web_search`、`analyze_image` 和 `subagent` 由统一 Tool 注册机制管理。Main Agent 与 Subagent 分别按各自的 `tools`/`subagent.tools` 开关注册工具；Subagent 默认配置已启用 `analyze_image`。文件 Tool 只接受 Workspace 内的相对路径；`search_files` 默认跳过超大文件、非文本文件以及 `.git`、`node_modules`、`build` 等目录。`shell` 每次执行前需要授权，并返回退出码、标准输出、标准错误和超时信息。`web_search` 默认关闭，通过 [Exa](https://exa.ai) 检索公网内容，单次最多返回 10 条结果，启用时需要 `EXA_API_KEY`（可在 [Exa Search](https://exa.ai/products/search) 申请）。单个 Tool Result 回灌模型最多 16K 字符，较大的结果会保存为 Session Artifact。

## Session 与附件

成功轮次会追加到 `~/.nosis/sessions/<SESSION_ID>/<SESSION_ID>.jsonl`，完整上下文、Tool Result 和模型响应均可恢复。超过回灌上限的 Tool Result 保存为同目录下的 `<TOOL_CALL_ID>.txt`；使用 `nosis --session SESSION_ID` 恢复。

GUI 上传的图片保存在 Workspace 的 `.nosis/attachments/<id>.<ext>`，Session 只记录路径和 MIME 类型。Runtime 请求 Provider 时才读取图片，并通过 `LLMRequest(media_root=workspace.path)` 解析工作区路径。
