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

侧边栏的 `Temporary` 会在 `agent_config.json` 的 `scratch_workspace_root` 下创建一个独立 Workspace。该目录按 Session 标识命名并持久保留；删除 Session 不会删除其中的文件。输入区的 Workspace 菜单也可以把尚未运行的会话切换到对应的临时工作区。

左下角 Settings 可管理 Provider 与默认路由、调整 Agent Runtime 配置，并查看当前发现的 Skill、Plugin 与 MCP Server。API key 只写入 `~/.nosis/.env`，设置接口不会回传密钥正文。

打开会话时在后台建立轻量 Session 连接并取回执行面快照。连接期间可以写草稿、选择图片，并调整权限模式、模型和 Workspace；选择立即显示，连接就绪后经由 Bridge 应用，初始化快照不会覆盖待应用的选择。连接与设置确认完成后即可发送任务。Provider、MCP、Tool 和 Agent 在首次发送消息时才初始化。

重新连接会先回放历史事件，再同步当前状态；历史快照不会触发待发送命令。如果当前会话仍在执行任务，连接期间选择的模型和 Workspace 不会提交，界面会恢复原值并提示任务结束后重试。权限模式仍可在任务运行中修改。

同一页面可以打开多个会话并让它们并行运行，切换页面或临时断线不会主动取消任务，重新连接后继续接收事件。执行过程由结构化事件渲染：工具调用与结果、活动栏里的计划（`Plan x/y`）与后台子 Agent 任务、上下文窗口占用，以及人工确认和提问卡片。「停止」会取消当前轮次，但不会撤销已经完成的 Tool 操作。上传的图片保存在当前 Workspace 的 `.nosis/attachments/`。

多个页面可以分别操作不同会话，任务互不阻塞。同一会话只由一个页面控制：新页面和自动重连不会抢占已有连接，只有点击「在此页面接管」才会转移控制权。原页面断开后，其他页面可以直接连接仍在后台运行的会话。

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
