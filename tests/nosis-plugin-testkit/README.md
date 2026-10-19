# Nosis Plugin Testkit

独立第三方示例包，Plugin 名为 `repo-review-kit`。插件代码不导入 Nosis，不访问 Runtime
内部对象，仅组合标准 Skill、Agent Markdown 与 MCP Server。示例放在
`tests/nosis-plugin-testkit/`，不作为默认插件安装。

## 组件

- `repo-review-kit:code-review`：通过现有 `subagent` 工具委派 Reviewer。
- `repo-review-kit:reviewer`：选择 `read_file`、`search_files` 和
  `mcp__repo-review-kit_analyzer__analyze_code`。
- `repo-review-kit:analyzer`：本地 stdio MCP Server，通过 AST 检查 Python 语法、
  `eval` 与裸 `except`；另有故意报错的 `fail_review` 和被 allowlist 隐藏的 `internal_probe`。

该分析器用于验证组件互通，不是完整的静态分析工具。

## 安装与使用

1. 使用已安装 Nosis 的 Python 环境，安装 `requirements.txt` 中的 MCP SDK。
2. 将整个目录复制到 `~/.nosis/plugins/repo-review-kit/`。
3. 确保 `mcp/servers.json` 的 `command` 指向有 MCP SDK 的 Python；激活环境后可以用
   `python`，也可以填写该解释器的绝对路径。stdio cwd 默认是插件根目录。
4. 在已有 `agent_config.json` 中启用 `main_agent.tools.subagent` 和 `mcp.enabled`。
   无需修改 Nosis 源码或新增工具注册代码。
5. 重启 Nosis，要求使用 `repo-review-kit:code-review` 检查一个 Python 文件，审批
   `repo-review-kit:analyzer/analyze_code` 后查看结果。

可以从 Nosis 自带的 Plugin Creator Skill 运行独立校验器：

```powershell
python <plugin-creator目录>/scripts/validate_plugin.py <本插件目录>
```

静态校验不会运行服务器或确认动态工具是否可用。

## 测试

在已安装 Nosis 的 Python 环境中，从仓库根目录运行：

```powershell
python -m unittest discover -s tests/nosis-plugin-testkit/tests -v
```

从其他目录运行时，将 `-s` 改为本插件 `tests` 目录的路径。

`McpServerTest` 直接使用 MCP 客户端验证发现、调用、结构化结果和报错后的继续调用。
`BridgeIntegrationTest` 启动 `python -m interfaces.bridge`，仅通过公开的 NDJSON 消息
收发输入、设置快照、审批与结果；本地 HTTP Mock Provider 提供可重复的模型响应，
不需要外部 API、真实密钥或额外的插件执行框架。

Bridge 测试目前在 Windows 运行，使用通用 Windows Job 在结束时回收测试进程及其
MCP 子进程；其他平台跳过这组进程测试，MCP 独立测试仍可运行。每例测试使用临时
HOME/USERPROFILE、配置和 Workspace，不改动真实 Nosis 安装目录或用户插件。

覆盖：组件发现与注册、Skill → Reviewer → MCP、审批通过和拒绝、角色工具选择、
allowlist、Plugin 禁用、无效角色与 Server 启动失败隔离，以及 Tool 调用错误后的继续执行。

## 修改与禁用

Plugin、Skill、Agent 文件不热加载；重启后重新装配声明。禁用时将 `plugin.json`
中的 `enabled` 改为 `false` 并重启；若另行配置过该角色的 Provider 路由，也应删除该路由。
ExecutionPlane 重建可能复用同一 MCP 服务进程。更新 Server 源码时，应关闭所有持有
该连接的 Nosis Runtime 并等待 Broker 回收，再重新启动；只重开 Session 不保证刷新。
