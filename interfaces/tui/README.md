# TUI

`interfaces/tui` 是使用 TypeScript、Ink 和 React 实现的终端交互层。它启动独立 Bridge 子进程，渲染协议消息并采集输入，不运行 Agent Loop，也不直接执行 Tool。

## 启动

```bash
nosis
nosis --workspace ~/projects/my-project
nosis --temporary
```

未传 `--workspace` 时使用当前目录。`--temporary` 由 Python 启动器在配置的 `scratch_workspace_root` 下创建持久保留的 Nosis-managed Workspace，再把解析后的路径交给 TUI；它不能与 `--workspace` 同时使用。

`interfaces/launch.py` 负责初始化 `~/.nosis/`、定位 Node 和已构建的 TUI bundle，并通过 `NOSIS_PYTHON` 告诉前端应使用哪个 Python 解释器启动 Bridge。

## 交互模型

TUI 在启动或切换 Session 时先发送 `open_session`，等待 `session_ready` 与首个 `runtime_state` 后才允许提交命令。普通消息通过 `user_turn` 发送；Turn 执行中提交的新输入转为 `user_steer`。

界面由结构化协议状态驱动，展示：

- assistant 文本与 reasoning 流
- Tool 调用、结果和错误
- Context Window、计划及后台 Job
- Provider、权限和 Runtime phase
- 授权请求与结构化用户提问
- 历史 Session 列表和恢复结果

主要命令：

| 命令 | 作用 |
| --- | --- |
| `/permissions` | 切换当前 Session 的权限 preset |
| `/sessions` | 列出并切换当前 Workspace 的历史 Session |
| `/model` | 切换当前 Session 的 Provider |
| `/provider` | 查看 Provider、Skill、Plugin 与 MCP 配置概览 |

常用按键：

| 操作 | 作用 |
| --- | --- |
| `Enter` | 提交输入或确认选项 |
| `Ctrl+J` | 输入换行 |
| `Shift+Enter` | 支持 kitty keyboard protocol 时输入换行 |
| `↑` / `↓` | 移动输入光标或列表选择 |
| `Esc` | 关闭列表、拒绝授权或取消当前 Turn |
| `Ctrl+C` | 取消当前 Turn；空输入时退出 |
| `Ctrl+D` | 退出 |

TUI 只在已知支持的终端环境启用 kitty keyboard protocol，不进行可能把探测响应写入输入框的主动探测。

## 模块索引

| 模块 | 职责 |
| --- | --- |
| `src/cli.tsx` | Node CLI 参数和 Ink 根节点 |
| `src/app.tsx` | Bridge 生命周期、界面组合和用户操作路由 |
| `src/bridge.ts` | Bridge 子进程与 NDJSON 收发 |
| `src/state.ts` | 协议消息驱动的 TUI 状态归约 |
| `src/input.tsx` | 多行输入、光标和键盘行为 |
| `src/commands.ts` | slash command 定义与匹配 |
| `src/renderer.tsx` | transcript、Tool、状态和交互卡片渲染 |
| `build.mjs` | esbuild bundle |

共享协议来自 [`interfaces/protocol`](../protocol/README.md)，Bridge 行为见 [`interfaces/bridge`](../bridge/README.md)。

## 开发

```bash
npm run build --workspace interfaces/tui
npm run typecheck --workspace interfaces/tui
npm test --workspace interfaces/tui
```

构建入口为 `src/cli.tsx`，产物为 `dist/app.js`，并作为 Python package data 随 Nosis 安装。
