# GUI

GUI 由 FastAPI 中继服务和 React + assistant-ui 前端组成。它与 TUI 共用 Agent Runtime、Provider、Tool、授权、Session 和取消行为。

## 使用

构建并启动：

```bash
npm install
npm run build
nosis-gui
```

打开 <http://127.0.0.1:8737>。参数与 TUI 一致：

```bash
nosis-gui --workspace ~/projects/my-project
nosis-gui --config path/to/provider_config.json \
          --agent-config path/to/agent_config.json
```

执行中点击「停止」会取消当前轮次，等同于 TUI 的 `Esc`。同一 Session 同时只允许一个页面驱动 Agent，不同 Session 可以并行执行；已执行的 Tool 操作不会因取消而撤销。

## 前端开发

先启动 `nosis-gui`，再在另一个终端运行：

```bash
npm run dev
```

访问 Vite 输出的 <http://127.0.0.1:5173>；API 和 WebSocket 会代理到 Python 服务。界面代码在 `src/`，构建产物在 `static/`（不提交）。

上传图片会保存到当前 Session 绑定 Workspace 的 `.nosis/attachments/`，具体存储和解析规则见 [`agent_core/README.md`](../../agent_core/README.md#session-与附件)。每个 Session 目录下的 `session.json` 保存其 Workspace；切换 Session 不会改变其他会话的工作区，多个会话可以同时运行。

```bash
npm run typecheck
npm test
```
