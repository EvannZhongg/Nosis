# Parallel Search MCP 示例

这个可选 Plugin 通过 Nosis 现有的 Streamable HTTP MCP 客户端提供网页搜索和内容提取。[Parallel Search MCP](https://docs.parallel.ai/integrations/mcp/search-mcp) 支持免费、无需 API Key 的轻量使用；匿名搜索使用 `fast` 模式，有用量限制。Nosis 的模型 Provider 仍需单独配置。

## 安装与使用

先按仓库 README 配置并启动一次 Nosis，再退出。从仓库根目录把示例中的两个配置文件复制到一个新的 Plugin 目录（若该目录已存在，请先检查已有配置）：

```bash
mkdir ~/.nosis/plugins/parallel-search
cp examples/parallel-search/plugin.json examples/parallel-search/.mcp.json ~/.nosis/plugins/parallel-search/
```

保留 `~/.nosis/agent_config.json` 中原有配置，并确认 `mcp.enabled` 为 `true`。无需改动 `mcp.servers`，Plugin 会与已有 Server 一起加载。重新启动 `nosis` 或 `nosis-gui`，发送例如：

> 请用 Parallel Search 搜索 Python 官方文档，说明 asyncio.TaskGroup 的用途，并读取相关页面，附上来源链接。

首个 Turn 会连接 `parallel-search:search`，发现工具后，主 Agent 可调用：

- `mcp__parallel-search_search__web_search`：按 `objective` 和 `search_queries` 搜索网页。
- `mcp__parallel-search_search__web_fetch`：按 `urls` 读取页面，可附上 `objective`。

示例使用默认 15 秒启动超时与 60 秒调用超时。请求带有 `User-Agent: Nosis/0.1.0`，不带认证头。查询和 URL 会发送到 Parallel 服务。`tools.approval` 为 `always`，调用遵循 Nosis 当前 Session 权限与授权流程；在需要确认时请检查参数后授权。

## 控制与停用

示例只允许上述两个工具，不修改内置 `web_search`（Exa）、`web_fetch`、模型路由或默认资源，也不会自动安装。内置工具和 MCP 工具使用不同名称，可以同时保留。

临时停用时，在复制的 `.mcp.json` 中为 `search` 添加 `"enabled": false`；只保留搜索时，把 `tools.enabled` 改为 `["web_search"]`。`agent_config.json` 的 `mcp.enabled: false` 会关闭所有 MCP Server。完全移除时删除手动安装的 `parallel-search` Plugin 目录。修改后重启 Nosis。

若工具未出现，检查 MCP 状态和 Runtime warning、JSON 格式、总开关及网络是否能访问 `https://search.parallel.ai/mcp`。服务不可用或达到限额时会报告连接或工具错误。
