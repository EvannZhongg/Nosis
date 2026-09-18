# TUI

Nosis 的终端界面使用 TypeScript、Ink 和 React。它只负责渲染 Bridge 消息和采集输入。

## 使用

```bash
nosis
nosis --workspace ~/projects/my-project
nosis --config path/to/provider_config.json \
      --agent-config path/to/agent_config.json
```

未传 `--workspace` 时，当前目录即 Workspace。

常用操作：

| 操作 | 作用 |
| --- | --- |
| `Enter` | 提交输入或确认当前选项 |
| `Ctrl+J` | 输入换行 |
| `Shift+Enter` | 在支持 kitty keyboard protocol 的终端中换行 |
| `↑` / `↓` | 移动光标或选择列表项 |
| `Esc` | 关闭列表、拒绝授权或取消当前轮次 |
| `Ctrl+C` | 取消当前轮次；空输入时退出 |
| `Ctrl+D` | 退出 |

输入 `/` 可打开命令列表。主要命令：

- `/permissions`：切换当前 Session 的授权模式
- `/sessions`：列出并切换当前 Workspace 的历史会话

执行中提交的新输入会作为 steering 发送给当前轮次。

TUI 启动或切换会话时只打开 Session；Provider、MCP、Tool 和 Agent 在首次发送普通消息时才初始化。`/permissions` 因此可以在第一次对话前直接修改授权模式。

## 开发

```bash
npm run build --workspace interfaces/tui
npm run typecheck --workspace interfaces/tui
npm test --workspace interfaces/tui
```

入口为 `src/app.tsx`，构建产物为 `dist/app.js`。协议来自 [`../protocol`](../protocol/README.md)，Runtime 通过 [`../bridge`](../bridge/README.md) 启动。
