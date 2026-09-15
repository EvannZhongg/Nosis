# TUI

Nosis 的终端界面使用 TypeScript、Ink 和 React 编写。它只负责渲染协议消息、收集输入，Agent 执行由共用的 Runtime 完成。

## 使用

构建后从任意 Workspace 启动：

```bash
nosis
nosis --workspace ~/projects/my-project
nosis --session SESSION_ID
nosis --config path/to/provider_config.json \
      --agent-config path/to/agent_config.json
```

| 按键 | 作用 |
| --- | --- |
| `Enter` | 提交输入；执行中输入会排队 |
| `Ctrl+J` | 在输入框内换行（所有终端可用） |
| `Shift+Enter` | 在输入框内换行（需终端支持 kitty keyboard protocol） |
| `←` / `→` | 移动光标；在 shell 授权中切换 Allow / Deny |
| `Enter` | 确认授权选项 |
| `Esc` | 拒绝授权；执行中取消当前轮次 |
| `Ctrl+C` | 取消当前轮次；空输入时退出 |
| `Ctrl+D` | 退出 |

传统终端把 `Shift+Enter` 和 `Enter` 发送为同一个字节，程序无法区分，因此只有支持 kitty
keyboard protocol 的终端才能用 `Shift+Enter` 换行。目前按环境变量识别 kitty、Ghostty、WezTerm
（Windows 与 Linux 同样适用）；其他终端（Apple Terminal、旧版 iTerm2 等）请用 `Ctrl+J`。

未传 `--workspace` 时，启动命令的当前目录就是 Workspace。

## 开发

```bash
npm install
npm run build
npm run typecheck
npm test
```

入口在 `src/app.tsx`，构建产物为 `dist/app.js`。协议类型来自 [`../protocol`](../protocol/README.md)，Runtime 通过 [`../bridge`](../bridge/README.md) 作为子进程运行。
