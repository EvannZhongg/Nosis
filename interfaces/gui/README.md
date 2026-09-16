# GUI

GUI 由 FastAPI 中继服务和 React + assistant-ui 前端组成。它与 TUI 共用 Agent Runtime、Provider、Tool、授权、Session 和取消行为。

模型选择器右侧的权限选择器控制当前 Session 的 `Ask for approval` / `Full Access` preset；浏览器只发送协议消息，实际权限判断由 Runtime 的 `PermissionController` 完成。

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

执行中点击「停止」会取消当前轮次，等同于 TUI 的 `Esc`。不同 Session 可以并行执行；切换会话只切换显示，已经运行的 Chat 保持连接并继续接收事件。已执行的 Tool 操作不会因取消而撤销。

打开历史会话只读取持久化内容，不启动 Bridge。第一次发送消息时，GUI 服务才为该 Session 启动 Runtime。GUI 服务按 `session_id` 保存活动 Runtime；WebSocket 仅负责附着与分离，所以页面刷新或临时断网不会终止正在执行的任务。重新连接后会恢复 running/approval 状态并补发断线期间的协议事件，轮次结束后关闭对应 Bridge 进程。同一 Session 同时只由一个页面控制；新页面会接管连接，被接管的页面停止自动重连，并可由用户显式接管回来。

## 前端开发

先启动 `nosis-gui`，再在另一个终端运行：

```bash
npm run dev
```

访问 Vite 输出的 <http://127.0.0.1:5173>；API 和 WebSocket 会代理到 Python 服务。界面代码在 `src/`，构建产物在 `static/`（不提交）。

上传图片会保存到当前 Session 绑定 Workspace 的 `.nosis/attachments/`，具体存储和解析规则见 [`agent_core/README.md`](../../agent_core/README.md#session-与附件)。Session 按 Workspace 分组存储在 `~/.nosis/sessions/<workspace-key>/<session-id>/`；切换 Session 不会改变其他会话的工作区，多个会话可以同时运行。

```bash
npm run typecheck
npm test
```
