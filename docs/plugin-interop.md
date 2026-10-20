# Plugin 组件互通与校验边界

## 调用链与修改范围

现有调用链为 `Bridge → RuntimeHost → ExecutionPlaneManager.ensure()`。
装配时，`PluginLoader` 解析 `plugin.json`，`PluginManager` 发现启用的包并路由三个组件：

1. `PluginSkillSource → SkillLoader → SkillRegistry`，注册 `<plugin>:<skill>`。
2. MCP server map 经 Core 配置解析与 namespace 处理，合并进执行面的 `McpClientManager`；
   Manager 持有进程与连接，`wait_ready()` 返回过滤后的 `McpTool`。
3. Agent Markdown 解析为 `PluginAgent`，Runtime 选 Provider 并创建 `SubagentRoleRegistry`。

原阻断点是第 3 步在读取 Markdown 时，只允许静态 `ROLE_TOOL_NAMES`，因此无法选择
第 2 步动态发现的工具。本次仅修改 Plugin 声明解析、Catalog 完成后的角色校验、MCP
名称冲突诊断及 Creator 的独立静态校验器；不改变 Agent Loop、Session 或 Bridge 协议。

## 声明与执行

```yaml
---
name: reviewer
description: Review source code
tools:
  - read_file
  - search_files
  - mcp__repo-review-kit_analyzer__analyze_code
---
Read the requested source, analyze it, and report concrete findings.
```

`tools` 保持原有内置角色工具范围及顺序，允许附加完整 MCP 工具名。省略仍选择原有
内置角色工具，空数组不选择声明工具；Skill 和图片工具仍由既有能力推导机制添加。
不增加别名、通配符或隐式授权。可以显式引用其他启用插件或 standalone MCP Server 的工具。

解析器检查数组类型、重复项和引用语法，Runtime 在 MCP 发现、allowlist 过滤和 Catalog
扩展后检查工具是否存在。缺失引用跳过该角色，warning 包含角色名、文件和工具名；
Provider 路由先对声明角色校验，因 MCP 暂不可用而跳过的角色不会被误报为配置中的未知角色。
真正不存在的配置角色仍保留原有报错行为。

调用沿用 `SubagentTool → SubagentRuntime → catalog.select → ToolSet.execute → ToolPolicy → McpTool`。
子 Agent 使用同一 Catalog、权限控制器及 MCP Manager，不获得 Runtime 内部对象，也不能再次委派。
声明工具仅选择可见能力，不能绕过 MCP allowlist、审批或执行权限。

## 契约与错误隔离

`plugin.json` 字段与组件路径规则不变。`capabilities` 仅描述包，不启用组件。
Loader 的包级错误跳过整个包；组件加载错误保留其他组件。重复 Agent 或 MCP Server
保留第一项并列出冲突来源。规范化后 MCP 工具名冲突通过既有 `mcp_server_status`
报告，仅使冲突 Server 的工具视图不可用；一次 Server 工具注册失败不会留下部分工具。
MCP 调用、服务断开和拒绝审批继续沿用现有 ToolResult 与服务生命周期处理。

Creator 的脚手架生成相同 manifest。独立校验器不 import Nosis：它检查静态结构、
组件文件、Agent 工具语法、MCP transport/字段/类型/allowlist/approval 和重复注册。
它不启动 MCP、不解析凭据、不判断动态工具是否存在；这些由 Runtime 完成。
契约矩阵测试将其接受结果与 Loader/Manager 对照，防止两份静态校验漂移。

## 文件变更与重新装配

不实现热加载，不新增 fingerprint 或配置项。Skill/Agent 文件变化不会单独触发重建，
重新打开 Session 也可能复用 Plane。配置、Workspace、Provider、Instructions 或有效 MCP
配置变化会沿用既有机制整体重建；重建时重新发现并装配所有组件。安装、更新、禁用和
删除插件后，重启 Nosis 是使声明完整生效的操作边界。

Plane 持有独立的 MCP Manager；关闭或重新装配时关闭其连接与 stdio 子进程。
重新装配会重新启动 MCP Server 并发现工具，其他 Runtime 的连接不受影响。
禁用或删除角色时，还需删除 `provider_config.json` 中指向该角色的显式路由。

## 验证

`tests/test_plugin_contract.py` 检查独立校验器与 Runtime 契约；
`tests/test_plugin_interop.py` 复用 unittest、MockProvider、Fake MCP Server 与 Bridge，
覆盖 Skill → Reviewer → MCP、子 Agent 工具选择与审批、禁用/过滤/启动失败、冲突隔离和重新装配。
第三方示例包位于 `tests/nosis-plugin-testkit/`，不导入 Nosis 内部模块，也不作为默认插件安装。
从仓库根目录运行其独立测试：

```powershell
python -m unittest discover -s tests/nosis-plugin-testkit/tests -v
```
