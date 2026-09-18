# GUI

GUI 由 FastAPI 服务和 React + assistant-ui 前端组成。浏览器只负责交互与展示，Agent 执行仍由 Bridge 和共用 Runtime 完成。

## 使用

```bash
nosis-gui
nosis-gui --workspace ~/projects/my-project
```

默认地址为 <http://127.0.0.1:8737>。
Provider、Agent 与 Skill 固定从 `~/.nosis/` 读取。

界面分三栏：左侧按 Workspace 分组列出历史会话（可新建、删除），中间是对话与输入区，右侧是当前 Workspace 的文件树。重新打开页面会回到上次的会话。

打开会话时先建立轻量 Session 连接并取回执行面快照：连接期间可以继续写草稿，早于快照发出的命令会在快照之后应用。Provider、MCP、Tool 和 Agent 在首次发送消息时才初始化，因此可以在第一轮对话前先调整权限模式、模型和 Workspace。

同一页面可以打开多个会话并让它们并行运行，切换页面或临时断线不会主动取消任务，重新连接后继续接收事件。执行过程由结构化事件渲染：工具调用与结果、活动栏里的计划（`Plan x/y`）与后台子 Agent 任务、上下文窗口占用，以及人工确认和提问卡片。「停止」会取消当前轮次，但不会撤销已经完成的 Tool 操作。上传的图片保存在当前 Workspace 的 `.nosis/attachments/`。

## 前端开发

先启动 Python 服务，再运行 Vite：

```bash
nosis-gui
npm run dev --workspace interfaces/gui
```

访问 <http://127.0.0.1:5173>。API 和 WebSocket 会代理到 Python 服务。

```bash
npm run typecheck --workspace interfaces/gui
npm test --workspace interfaces/gui
```

前端代码位于 `src/`，FastAPI 服务入口为 `server.py`，构建产物位于 `static/`。
