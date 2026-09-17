# GUI

GUI 由 FastAPI 服务和 React + assistant-ui 前端组成。浏览器只负责交互与展示，Agent 执行仍由 Bridge 和共用 Runtime 完成。

## 使用

```bash
nosis-gui
nosis-gui --workspace ~/projects/my-project
nosis-gui --config path/to/provider_config.json \
          --agent-config path/to/agent_config.json
```

默认地址为 <http://127.0.0.1:8737>。

权限选择器修改当前 Session 的授权模式；「停止」会取消当前轮次，但不会撤销已经完成的 Tool 操作。不同 Session 可以并行运行，切换页面或临时断线不会主动取消任务。

历史会话直接从持久化数据读取，首次发送消息时才启动对应 Runtime。上传的图片保存在当前 Workspace 的 `.nosis/attachments/`。

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
