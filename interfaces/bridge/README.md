# Bridge

Bridge 是前端与 Agent Runtime 之间的唯一执行通道。它以独立进程运行，通过 stdin/stdout 交换 newline-delimited JSON：

```text
nosis / nosis-gui
  └─ python -m interfaces.bridge
       └─ agent_core
```

Bridge 负责：

- 初始化并读取配置、Prompt 模板、Workspace Instructions、standalone Skill 和 Plugin descriptor，选择 Provider
- 组装 Tool、权限策略、MCP 和子 Agent Runtime
- 绑定 Workspace 与 Session
- 在 `AgentEvent` 和协议消息之间转换
- 转发用户输入、授权、提问、取消和运行状态

一次具体装配由 `ExecutionPlane` 原子持有，包括 Workspace、Provider、已解析 Agent 配置、Workspace Instructions、Agent、MCP、后台 Jobs 和 context window。Bridge 只保留一个当前 plane；Provider、Workspace、配置或 Instructions 改变时，旧 plane 整体关闭，下一次装配成功后再以单一对象挂载。

Agent Loop、Tool 执行和上下文管理仍属于 `agent_core`，前端与 Bridge 都不复制这些语义。

## Plugin discovery

Bridge 从 `~/.nosis/plugins/<plugin-name>/plugin.json` 发现 Plugin。Plugin 是能力组合与分发单元，不是 Runtime 或执行接口；启用后，Bridge 将 Skill、MCP 与 Agent 组件引用分别交给已有子系统。Hook 引用为对应子系统后续适配保留：

```json
{
  "name": "example",
  "version": "1.0.0",
  "enabled": true,
  "description": "Example capability package",
  "dependencies": [],
  "capabilities": ["skills"],
  "components": {
    "skills": ["skills"],
    "mcp": [],
    "agents": [],
    "hooks": []
  }
}
```

`components.skills` 中的路径是相对 Plugin 根目录的 Skill source 目录，每个直接子目录继续使用标准 `SKILL.md`。Plugin Skill 以 `<plugin-name>:<skill-name>` 注册；`~/.nosis/skills/<skill-name>/SKILL.md` 仍作为无 namespace 的 standalone Skill 加载。

`components.agents` 中的每个路径指向一个 Markdown Agent 定义。YAML frontmatter 必须包含 `name` 与 `description`，可选的 `tools` 使用 Nosis Tool 名称；未声明 `tools` 时启用所有可供 subagent 使用的内置 Tool。frontmatter 后的 Markdown 正文作为该角色的 system prompt，并与 `SubAgent.md` 组合。Plugin Agent 以 `<plugin-name>:<agent-name>` 注册。

Agent 的可选 `model` 若与 `provider_config.json` 中的 provider 名称相同，则直接选用该 provider；`inherit`、未配置或找不到同名 provider 时，沿用 role → subagent → main agent 的 provider 继承链。未被 Runtime 消费的 frontmatter 字段会被忽略。

`components.mcp` 指向 `.mcp.json` server map，且只在 `agent_config.json` 的 `mcp.enabled` 为 true 时加载：插件声明 server，MCP 是否运行仍由用户的总开关决定。每个 server 继续使用 MCP 的 `stdio` 或 `http` transport 配置；Bridge 通过现有 MCP subsystem 加载，并以 `<plugin-name>:<server-name>` 保存 Runtime identity。模型侧 Tool 名会规范化为 `mcp__<plugin>_<server>__<tool>`。例如：

```json
{
  "local": {
    "type": "stdio",
    "command": "python",
    "args": ["server.py"]
  },
  "remote": {
    "type": "http",
    "url": "https://example.com/mcp"
  }
}
```

`agent_config.json` 中的 `mcp.enabled` 是 standalone 与 Plugin MCP 共用的全局开关。关闭时 Bridge 不读取 Plugin 的 MCP component 文件，也不启动任何 MCP server；Plugin 的 Skill 等其他组件不受影响。

首条输入必须是 `open_session`。Bridge 先完成轻量 Session 初始化，依次发送 `session_ready` 与初始 `runtime_state`；后者是前端可以开始发送命令的同步屏障。Provider、MCP、Tool 与 Agent 只在首个 `user_turn` 到达时初始化。协议类型分别定义在 [`protocol.py`](protocol.py) 和 [`../protocol/src/protocol.ts`](../protocol/src/protocol.ts)，修改消息时必须同步更新两端。

进程入口为 `__main__.py`，主要装配位于 `bridge.py`，执行生命周期定义在 `execution_plane.py`。Bridge 可使用内存输入输出进行测试，无需启动 TUI 或 GUI。
