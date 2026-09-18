# Agent Core

`agent_core` 是与前端无关的 Agent Runtime，负责 Agent Loop、模型调用、上下文、Tool 执行、Session 和执行事件。它不感知 TUI、GUI 或 Bridge 协议。

Runtime 由 [`interfaces/bridge`](../interfaces/bridge/README.md) 统一装配；前端不直接运行 Agent 或 Tool。

## 配置

首次启动会在 `~/.nosis/` 生成：

- `provider_config.json`：Provider、模型和角色对应关系
- `agent_config.json`：Tool、上下文压缩、子 Agent 和 MCP
- `AGENTS.md`：空的用户级全局 Workspace Instruction（已存在时不覆盖）
- `prompts/`：主 Agent、子 Agent 与上下文压缩使用的 Prompt 模板

密钥可自行写入同目录的 `.env`，供配置中的 `${ENV_NAME}` 引用。

仓库中的默认配置是字段与结构的直接参考：

- [`provider_config.json`](../interfaces/bridge/defaults/provider_config.json)
- [`agent_config.json`](../interfaces/bridge/defaults/agent_config.json)
- [`prompts_template/`](../interfaces/bridge/defaults/prompts_template)

最小 Provider 配置示例：

```json
{
  "main_agent": {"provider": "openai"},
  "providers": {
    "openai": {
      "model": "openai/gpt-5",
      "url": "https://api.openai.com/v1",
      "key": "${OPENAI_KEY}"
    }
  }
}
```

模型名称遵循 LiteLLM 约定。`subagent` 和 `subagent_roles.<role>` 可以覆盖主 Agent 的 Provider；空字符串表示继承上一级。图片降级分析需要显式配置 `vision_provider`。

## Runtime 能力

### Tool

内置 Tool 包括文件读写、搜索、Shell、Web Search、图片读取/分析、子 Agent、用户提问、计划更新和 Skill 读取。每个 Agent 只获得配置中启用且运行依赖齐全的 Tool。`ask_user` 与 `update_plan` 是仅由主 Agent 使用的 Runtime 能力，不属于用户配置开关。

文件 Tool 限制在当前 Workspace；Shell 与需要确认的 MCP Tool 统一经过 Session 权限控制。较大的 Tool 输出会保存为 Session Artifact，供 Agent 后续读取。

### MCP 与 Skills

MCP 在 `agent_config.json` 的 `mcp.servers` 中配置，支持 `stdio` 和 `streamable_http`。远程 Tool 可分别限制是否暴露以及是否需要人工确认。

Skills 位于 `~/.nosis/skills/<name>/SKILL.md`。首次启动会安装内置 Skill；Runtime 只在需要时读取完整 Skill 内容。

### Workspace Instructions

Bridge 在每个 user turn 边界先读取固定的 `~/.nosis/AGENTS.md`，再读取 `agent_config.json` 的 `workspace_instruction_files` 所列文件。默认 Workspace 文件列表为 `CLAUDE.md`、`AGENTS.md`。所有来源均按从高优先级到低优先级排列，因此全局 `~/.nosis/AGENTS.md` 始终拥有最高优先级。Workspace 文件只从根目录读取，不递归扫描子目录，也不向父目录查找。加载结果会进入主 Agent 和子 Agent 的 system context，但不进入 Session transcript 或上下文压缩；文件或列表变化后，下一 turn 会重建 Runtime execution plane。

### 子 Agent

子 Agent 角色定义在 `agent_config.json` 的 `subagent_roles` 中。每个角色有独立的说明、Provider 和 Tool 集合；子 Agent 通过 `subagent(role, task)` 调用，并使用独立 Session。

### Session

Session 是 append-only JSONL Journal，保存对话、执行事件和当前计划，可由 TUI 和 GUI 共同读取和恢复。计划跨 Turn 延续，并由最新的 `plan_updated` 事件恢复。活动计划会进入模型上下文；所有步骤完成后仍保留并展示（GUI 完成后收起），但不再进入后续模型请求。Step 可保存不超过 500 字符的简短最终结果或阻塞原因。默认位置：

```text
~/.nosis/sessions/<workspace-key>/<session-id>/
```

GUI 上传的附件保存在 Workspace 的 `.nosis/attachments/`，Session 只记录附件路径和类型。

## 代码入口

| 模块 | 职责 |
| --- | --- |
| `agent.py` | Agent Loop |
| `context_manager.py` | 上下文窗口与压缩 |
| `tools/` | Tool 接口、Catalog 与 Policy |
| `providers/` | Provider 实现 |
| `session.py`、`session_store.py` | Journal 与持久化 |
| `subagent.py`、`jobs.py` | 子 Agent 与后台任务 |
| `mcp/` | MCP 配置与生命周期 |
