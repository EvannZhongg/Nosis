# Interfaces

`interfaces/` 是交互层与前端宿主的集合。它决定进程何时启动、Bridge 如何附着、协议消息如何呈现，但不装配 Runtime、不运行 Agent Loop，也不执行会改变 Workspace 或 Session 的 Tool。

```text
nosis ── launch.py ── TUI ── Bridge 子进程 ── agent_runtime ── agent_core
nosis-server ── application_host
        ├─ gui/      可选 HTTP / WebSocket + React
        ├─ mobile/   IM 控制器与飞书收发进程
        └─ Session Host ── Bridge 子进程 ── agent_runtime ── agent_core
```

`interfaces/*` 只依赖 `agent_runtime` 与 `agent_core`，不反向被依赖；两个 Python 层都不得 import `interfaces`。

## 共享模块

| 模块 | 职责 |
| --- | --- |
| `launch.py` | `nosis` 启动器：初始化配置目录、定位 Node 与 TUI bundle、处理 `--temporary`、以子进程运行 Ink UI |
| `application_host.py` | 常驻应用宿主：配置目录进程锁、共享 Session Host、并列 HTTP / IM 适配器与整体生命周期 |
| `bridge_process.py` | 一个前端持有的 Bridge 子进程及其通道收发 |
| `session_host.py` | 活动 Session 注册表、页面附着与事件续传缓存 |
| `bridge/`、`protocol/`、`client/` | 协议适配、TS 类型、公开 SDK |
| `tui/`、`gui/`、`mobile/` | 三个前端的实现 |

## 启动入口

| 命令 | 入口 | 进程归属 |
| --- | --- | --- |
| `nosis` | `launch.py:main()` | 自行拥有 Bridge 子进程；UI 作为子进程运行并被等待 |
| `nosis-server` | `application_host.main()` | 常驻应用宿主，拥有共享 Session Host、Bridge、HTTP 与 IM；`--no-http` 仅关闭 HTTP 适配器 |

`launch.py` 之所以用 `subprocess.call` 等待而不是 `execvp`，是因为 Windows 上 `execvp` 会另起进程并结束自己，终端提示符会先回来并与 UI 争抢控制台输入。它同时通过 `NOSIS_PYTHON` 把当前解释器交给前端，Bridge 因此运行在拥有 `agent_core` 的那个 Python 环境里。

`--temporary` 由启动器在 `scratch_workspace_root` 下创建 Nosis 管理的持久 Workspace，再把解析后的 `--workspace` 交给 TUI；二者不能同时使用。

## 应用宿主与单实例

`run_application_host()` 解析配置目录后，先在该目录的 `server.lock` 上取得非阻塞操作系统文件锁，再初始化配置并启动宿主。同一配置目录的第二个进程明确报错，不启动 Bridge、IM 或 HTTP，也不通过 HTTP 请求识别已有实例。锁随句柄关闭或进程退出释放，锁文件保留；不同配置目录的锁互不影响。

`ApplicationHost` 持有 `JsonlSessionStore`、`SettingsStore` 和 `ActiveSessionRegistry`。它先启动共享 Bridge，再并行运行 HTTP 与 IM 适配器，慢速 IM 连接不阻止 HTTP 启动。适配器启动失败会记录错误并释放自身资源，其余适配器继续运行；最后一个适配器失败时服务报错退出，不自动重试。宿主停止时先等待适配器关闭，再关闭 Session 与 Bridge。

默认 HTTP 适配器监听 `127.0.0.1:8737`；端口占用只影响该适配器，不影响 IM。HTTP 不参与单实例判断，FastAPI lifespan 不负责启动或关闭共享资源。

`nosis-server --no-http` 不导入 FastAPI、Uvicorn 或 GUI 路由，不需要 `[server]` HTTP 可选依赖，也不监听本地端口。飞书收发使用出站长连接。未启用 IM 通道时不创建收发进程，共享 Bridge 及其 Scheduler 仍保持运行。Ctrl+C、SIGTERM 和 Windows Ctrl+Break 都由宿主管理关闭。

## Bridge 进程与通道

`BridgeProcess` 持有一个常驻 Bridge 子进程，`BridgeChannel` 表示其中一个通道：发送 `channel_command`、缓存收到的 `channel_event` 并跟着它维护事件序号。宿主只决定何时创建、附着与关闭通道，协议语义、事件转换和执行仍然属于 `interfaces/bridge` 与 Runtime。

宿主可以同时打开多个通道（GUI 每个活动 Session 一个，TUI 每个访问过的 Session 一个）。关闭通道不会结束进程；宿主退出时才关闭进程及全部通道。

## 活动 Session

`ActiveSession` 是生命周期与 WebSocket 无关的运行时通道：它缓存 Bridge 的权威 `runtime_state` 与 transcript 检查点，按 `event_sequence` 支持断线续传，并记录当前控制附着。页面切换或临时断线不会取消 Turn；一个 Session 同时只有一个控制页面，只有显式的 takeover 才替换现有附着。

`ActiveSessionRegistry` 按 Session 保存这些对象，被 GUI 的 `server.py`、`routes/` 和移动端的 `controller.py`、`service.py` 共同复用，不依赖 FastAPI。

## Core 复用边界

前端宿主允许直接复用 Core 的只读能力：`JsonlSessionStore` 的 Session 读取与列表、`Workspace` 路径解析与包含校验、`SettingsStore` 的配置快照与写入。GUI 的 Session fork 是唯一直接写 Session 的路径，它复制锚点所在 Turn 之前的 Journal 生成新 Session，不改写源 Session，也不经过 Bridge。

其余会改变 Session 或 Workspace 执行状态的 Agent 操作必须经 Bridge 协议完成；前端不得自行创建 Agent、执行 Tool 或维护另一套授权状态。

## 前端文档

- [Bridge](bridge/README.md) · [协议类型](protocol/README.md) · [Node SDK](client/README.md)
- [TUI](tui/README.md) · [GUI](gui/README.md) · [移动端 / IM](mobile/README.md)
