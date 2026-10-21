# Nosis Plugin Testkit

独立第三方测试包，Plugin 名为 `repo-review-kit`，统一验证 Skill、Agent、MCP 和 Hook。
插件代码仅使用公开契约，不导入 Nosis；不作为默认插件安装。

## 组件

- `code-review` Skill → `reviewer` Agent → `analyzer` MCP：检查 Python 的 `eval` 和裸 `except`。
- `writing-rules` Skill 与 `conversation-analyzer` Agent：编写 Hookify 项目规则。
- `hooks/hooks.json`：声明 InputSubmitted、PreToolUse、PostToolUse、Stop 四类命令 Hook，使用 Workspace scope。

组件统一使用 `repo-review-kit:` 命名空间。Hookify 规则引擎仅依赖 Python 标准库，原始授权见 `LICENSE`。

## 安装与使用

1. 在已安装 Nosis 的 Python 环境中安装 `requirements.txt`。
2. 将本目录复制到 `~/.nosis/plugins/repo-review-kit/`。
3. 将 `mcp/servers.json` 和 `hooks/hooks.json` 中的 `command` 指向对应 Python 解释器。
4. 在 `agent_config.json` 中启用 `main_agent.tools.subagent` 和 `mcp.enabled`。
5. 执行 `/refresh` 重新装配插件，再要求使用 `repo-review-kit:code-review` 检查 Python 文件。

`/refresh` 不负责复制或安装文件。修改组件声明、代码或 `enabled` 后也用它重新加载。
静态校验：`python <plugin-creator目录>/scripts/validate_plugin.py <本插件目录>`。

将 `examples/` 中的规则复制到 Workspace 的 `.nosis/hookify.<name>.json` 即可启用；
每次 Hook 都重新读取规则，无需刷新。规则格式见 `skills/writing-rules/SKILL.md`。
路径规则只检查声明的工具与参数，不覆盖 Shell、MCP 等其他写入方式。
`require-tests` 检查当轮测试命令的实际完成结果，不代表测试覆盖率；Stop 最多请求一次纠正。

## 测试

在已安装 Nosis 的 Python 环境中，从仓库根目录统一运行：

```powershell
python -m unittest discover -s tests/nosis-plugin-testkit/tests -v
```

覆盖 Hook 命令与规则、MCP 发现与错误、Skill → Agent → MCP → Hook 协同、审批、组件禁用与隔离、
输入拒绝、文件写入拦截、规则更新、结果反馈和 Stop 纠正。
Bridge 测试仅经公开 NDJSON 与本地 HTTP Mock Provider 驱动，不使用外部 API 或真实密钥。
每例使用临时配置和 Workspace，不改动本机配置。

Bridge 进程测试目前使用 Windows Job 回收子进程，其他平台跳过；独立 Hook 与 MCP 测试可跨平台运行。
Core/Runtime 的机制回归测试使用从本插件裁剪出的 `tests/hookify` fixture：

```powershell
python -m unittest tests.test_hooks tests.test_hook_interop -v
```
