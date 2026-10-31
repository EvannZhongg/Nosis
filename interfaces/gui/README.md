# GUI

`interfaces/gui` 是 Nosis 的图形交互层，由 FastAPI 服务和 React + assistant-ui 前端组成。FastAPI 管理浏览器连接、文件与设置 API，并托管一个常驻 Bridge 进程，每个活动 Session 使用独立 Runtime 通道；浏览器不直接运行 Agent 或 Tool。

GUI 依赖是可选依赖。使用项目环境启动前先安装 GUI extra：

```bash
uv sync --extra gui
# 或：python -m pip install -e ".[gui]"
```

其中包含 Uvicorn 的 WebSocket 后端，Windows 上还包含用于通过 Explorer 打开 Workspace 文件夹的 `pywin32`；三个平台使用相同命令。若缺少 WebSocket 后端，`nosis-gui` 会在启动时直接提示安装命令。

## 启动

```bash
nosis-gui
nosis-gui --workspace ~/projects/my-project
```

默认监听 <http://127.0.0.1:8737>。`__main__.py` 初始化 `~/.nosis/`、默认 Workspace、Session Store 和 Settings Store，然后创建 FastAPI 应用。

## 服务端

`server.py` 组装应用和路由。服务端职责包括：

- 列出、读取和删除按 Workspace 分组的 Session
- 在应用启动时创建常驻 Bridge，并为活动 Session 创建、复用和关闭通道
- 通过 WebSocket 双向转发共享协议
- 管理单 Session 的页面附着、事件续传和显式 takeover
- 选择普通 Workspace，创建与回收 scratch Workspace
- 提供已脱敏设置快照及配置更新 API
- 展示长期记忆和定时任务，并允许启停任务
- 从某条历史用户消息的锚点创建分支 Session（fork），源 Session 保持不变
- 上传附件、下载文件和签发短期图片访问 URL
- 提供限制在当前 Session Workspace 内的文件树，并在系统文件管理器中打开该 Workspace

`ActiveSessionRegistry` 按 Session 保存后台运行的 `ActiveSession`。页面切换或临时断线不会取消 Turn；重新连接可以从 Bridge 分配的 `event_sequence` 继续。宿主缓存 Bridge 的完整运行快照和 transcript 检查点。一个 Session 同时只有一个控制页面，新页面只有显式 takeover 才替换现有 attachment。

HTTP / WebSocket 路由按模块拆分：

| 模块 | 路由范围 |
| --- | --- |
| `routes/sessions.py` | Session 查询与历史搜索、删除、fork、active session 与 `/api/session` WebSocket |
| `routes/workspace.py` | Workspace 选择、scratch 创建、文件树和打开文件夹 |
| `routes/media.py` | 附件上传、签名图片和附件下载 |
| `routes/settings.py` | 模型、设置、路由更新和长期记忆读取 |
| `routes/schedules.py` | 定时任务列表与启停 |

服务端非路由模块：

| 模块 | 职责 |
| --- | --- |
| `active_session.py` | 活动 Session 注册表、页面附着与事件续传 |
| `windows_folders.py` | 通过 Windows Explorer 定位并打开 Workspace 文件夹 |

常驻 Bridge 进程的生命周期、通道收发与故障广播由宿主侧模块 `interfaces/bridge_process.py` 提供；GUI 只决定何时创建、附着与关闭，不实现协议语义。

服务端只直接复用 Core 的只读 Session / Workspace 能力和 Runtime 的设置服务，唯一的 Session 写入是 fork：Session Store 复制锚点所在 Turn 之前的 Journal 生成新 Session，并记录来源和锚点，不经过 Bridge，也不改写源 Session。所有会改变 Session 或 Workspace 执行状态的 Agent 操作仍经 Bridge 协议完成。

## 前端

React 应用提供可折叠的 Session 列表、对话区和 Workspace 文件树。多个 Session 会稳定挂载，因此可以并行执行并在页面内切换。编辑历史用户消息不会改写原会话：GUI 以该消息为锚点创建分支 Session，先在源会话中预览待提交的内容，创建成功后再切换过去。

切换到其他会话时，只断开空闲会话的 WebSocket，保留其 Bridge、Provider 和 MCP；返回后附着已有 Runtime。离开浏览器页面时释放该页面持有的空闲会话，删除会话和关闭 GUI 宿主也会释放资源。关闭会话通道不会结束常驻 Bridge；新会话复用已加载的 SDK、tokenizer 和 TLS 上下文，仍有独立执行面。GUI 宿主退出时才关闭进程及全部通道。

对话层由 `useSessionRuntime` 统一管理：

- 建立和恢复 Session WebSocket
- 等待首个 `runtime_state` 同步屏障
- 回放 Journal 并把协议事件投影为 assistant-ui 消息
- 管理流式文本、reasoning、Tool、计划、Job 和 Context Window
- 处理授权、用户提问、停止、steer、Provider 与 Workspace 修改
- 上传附件并提交文本或 attachment-only Turn
- 在重连期间合并待应用的本地选择与服务端权威状态

主要前端模块：

| 模块 | 职责 |
| --- | --- |
| `src/main.tsx` | React 入口 |
| `src/App.tsx` | 应用布局、多 Session、设置入口和 Workspace 分组 |
| `src/Chat.tsx` | 单 Session 对话容器 |
| `src/chat/useSessionRuntime.ts` | 协议连接与 assistant-ui Runtime 适配 |
| `src/chat/runtimeState.ts` | Job、计划与审批请求的运行状态投影 |
| `src/chat/composerState.ts` | 输入框的提交与附件提交判定 |
| `src/transcript.ts` | 协议事件到 transcript 的纯状态投影 |
| `src/chat/Transcript.tsx` | transcript 视口与滚动 |
| `src/chat/TurnNavigation.tsx` | 按 Turn 跳转的导航条 |
| `src/session.ts` | 浏览器到服务端的 WebSocket client 与 JSON 消息接口 |
| `src/chat/Composer.tsx` | 输入、附件、Provider、权限和 Workspace 控件 |
| `src/chat/Messages/` | assistant、user、Tool 与媒体消息渲染 |
| `src/chat/RuntimeIndicators/` | Context Window、计划和后台 Job |
| `src/Workspace.tsx` | Workspace 文件树与逐项复制相对路径 |
| `src/CopyTextButton.tsx` | 复制按钮（代码块、表格与文件树共用） |
| `src/Settings.tsx` | Provider、Agent、Memory、Skill、Plugin、MCP、Schedule 设置页 |
| `src/api.ts` | HTTP API 类型与请求函数 |

## 附件与媒体

上传文件和 Agent 生成图片保存在当前 Workspace 的 `.nosis/attachments/`。Session 只持久化稳定的 Workspace 相对路径、文件名、MIME type 和大小。

浏览器不直接获得任意本地路径。图片显示前向服务端换取短期签名 URL；附件下载和文件树读取也会校验目标属于该 Session 的 Workspace。图片作为视觉附件进入模型，其他文件由 Agent 通过 Workspace Tool 读取。

## 设置与运行状态

Settings 页面覆盖 Provider、默认路由、Agent Tool、上下文、Memory、Skill、Plugin、MCP 和 Schedule。API key 只写入 `~/.nosis/.env`，服务端返回的设置快照只包含凭据来源与是否已配置。

对话输入框首字符为 `/` 时展开命令菜单。`/refresh` 在当前会话空闲时重新加载运行环境，保留对话、权限和计划，并重连 MCP；支持点击、Enter 执行、Tab 补全、Esc 收起。运行中不能执行刷新，草稿保持不变。配置变化仍会在下一轮对话自动检查并应用。

Context Window 指示器展开后显示当前输入、缓存命中率、可用输入上限、剩余输入空间、压缩触发点、模型总窗口、压缩次数与当前压缩总结。后台任务指示器显示任务数量、类型、Job ID 与状态，shell Job 展开命令、subagent Job 展开角色与任务描述。Assistant 消息把 reasoning 与 Tool 调用折叠为可展开的推理与执行过程区块：Turn 运行且尚未出现回答文本时默认展开，回答开始后自动折叠，回答本身始终可见。

执行过程完全由结构化协议渲染，不从 assistant 自由文本推断 Tool、计划或状态。停止操作会取消当前 Turn，但不会撤销已完成的 Tool 操作。

## 开发

先启动 Python 服务，再运行 Vite：

```bash
nosis-gui
npm run dev --workspace interfaces/gui
```

访问 <http://127.0.0.1:5173>，Vite 将 API 和 WebSocket 代理到 FastAPI。

```bash
npm run build --workspace interfaces/gui
npm run typecheck --workspace interfaces/gui
npm test --workspace interfaces/gui
```

前端源码位于 `src/`，Vite 构建产物位于 `static/`，并作为 Python package data 随 Nosis 安装。
