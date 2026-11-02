# 移动端 / IM 接入

```text
飞书应用机器人 ─ 官方 Node SDK 收发进程 ─ MobileController ─┐
                                                        ├─ Session Host ─ Bridge ─ agent_runtime ─ agent_core
GUI HTTP / WebSocket ────────────────────────────────────┘
TUI ─ @nosis/client ─ Bridge ─ agent_runtime ─ agent_core
```

`interfaces/mobile` 是交互层，不装配 Runtime，不执行 Agent 或 Tool，也不直接修改 Session Journal。命令和按钮最终只发送既有 Bridge 命令。它复用 `interfaces/session_host.py` 的后台 Session、事件快照、断线附着与显式接管规则，不依赖 FastAPI。

飞书官方 Node SDK 在独立进程中管理平台长连接、回调 ACK 和消息 API。Python 宿主用 JSONL 与该进程通信，凭证通过 stdin 传入而不放在进程参数中；该进程不持有 Runtime 或会话。不同 IM 平台实现 `views.Transport`，复用通道无关的交互控制器，平台卡片格式只存在于适配器中。

## 配置与启动

```powershell
npm install
npm run build --workspace @nosis/mobile
uv sync
```

首次初始化会从 `agent_runtime/defaults/im_channels.json` 安装禁用状态的飞书模板，不覆盖已有配置。在 `~/.nosis/im_channels.json` 中配置：

```json
{
  "channels": [
    {
      "id": "personal-feishu",
      "type": "feishu",
      "enabled": true,
      "app_id": "cli_your_app_id",
      "app_secret": "${FEISHU_APP_SECRET}",
      "allowed_users": ["ou_your_open_id"]
    }
  ]
}
```

密钥可以直接填写，也可用 `${ENV_NAME}` 引用进程环境变量或同目录 `.env`（`.env` 优先，值不做二次插值）。`allowed_users` 使用当前应用下的 `open_id`，空数组拒绝所有用户；被拒用户的 open_id 会写入本地日志，便于补配置。当前只接收人工用户与机器人的单聊文本，不开放群聊或自动授权。

配置完成后运行 `nosis-server --no-http`，只启动共享 Session Host、Bridge 与 IM，不需要 FastAPI、Uvicorn 或任何本地 HTTP 端口。需要同时使用 GUI 时安装 `uv sync --extra server` 并运行 `nosis-server`。两种模式使用同一个配置目录级进程锁，重复启动会明确报错。宿主与单实例规则见 [`interfaces/README.md`](../README.md)。

通道由 `interfaces/application_host.py` 装配并管理生命周期，与 HTTP 并列启动，IM 连接不等待 HTTP 就绪。HTTP 端口占用或依赖缺失不关闭 IM；IM 配置错误或收发进程启动失败时释放移动端资源，HTTP 仍可使用。无 HTTP 模式下 IM 启动失败会报错退出，修正后需重启宿主。未配置任何通道时不启动 IM 进程，共享 Bridge 与 Scheduler 仍保持运行。构建产物随 wheel 的 `interfaces.mobile/dist/feishu.cjs` 分发。

收发进程由 Transport 监督：意外退出时先结束旧连接上的请求与入站处理，再按 1–30 秒退避重建连接并恢复已持久化的入站消息；关闭宿主会停止监督和各对话任务。入站按聊天与用户分队列，不同对话并发，同一对话的消息和按钮保持到达顺序。

## 飞书开放平台

使用企业自建应用并开启机器人能力。最小应用身份权限：

- `im:message.p2p_msg:readonly`：接收用户发给机器人的单聊消息。
- `im:message:readonly`：获取消息中的资源文件。仅有单聊事件权限不足以调用资源下载接口。
- `im:message:send_as_bot`：以机器人身份发送和更新消息卡片。
- `im:resource`：下载图片、文件，以及上传机器人回传的附件。
- `im:message.reactions:write_only`：添加和删除消息表情回复。
- `cardkit:card:write`：创建和更新流式卡片实体。

在“事件与回调”中把事件和回调都配置为长连接，并订阅事件 `im.message.receive_v1` 与回调 `card.action.trigger`（新版卡片回传交互）。保存长连接方式时本地收发进程必须已连接，随后发布应用版本并设置可用范围。无需公网监听端口或内网穿透。

## 交互与会话绑定

首次普通消息不会创建隐式 Session，也不会自动选择最近会话。先 `/workspaces` 选择工作区，再 `/sessions` 选择已有会话或 `/new` 新建；菜单每页 8 项，提供下一页按钮。

| 输入 | 行为 |
| --- | --- |
| `/` | 显示可点击的命令菜单 |
| `/start`、`/help` | 显示使用方式 |
| `/workspaces` | 选择工作区 |
| `/sessions` | 恢复当前工作区的会话 |
| `/new` | 在当前工作区新建 Session |
| `/permissions` | 查看与切换三种权限 preset |
| `/model` | 从已配置的 Provider 中选择当前会话模型 |
| `/provider` | 查看模型、Skill、Plugin 和 MCP 配置概览 |
| `/refresh` | 空闲时重载运行环境，保留会话、计划与权限 |
| `/status` | 查看工作区、会话、运行状态、模型与权限 |
| `/cancel` | 取消当前 Turn |
| `/takeover` | 显式替换另一端的控制附着 |
| 普通文本 | 空闲时开始 Turn；执行期间发送 steer；允许自由输入的提问期间提交答案 |
| 图片、文件、图文消息 | 空闲时下载附件并开始 Turn；运行、提问或审批期间提示稍后重发 |

绑定按通道、单聊、用户隔离，保存在 `im_bindings.json` 而不进入 Session Journal。该文件记录入站内容、`received` / `submitted` / `completed` 状态，以及平台消息与 Session、Turn ID 的映射：提交前先持久化 Turn ID，重投、宿主重启与通道重连时先核对活动 Runtime 与只读 Journal，再恢复尚未提交的消息。Journal 已开始的 Turn 不会自动重跑，避免重复执行有副作用的工具。保留全部未完成记录与最近 1000 条完成记录。平台 ACK 到 Python 持久化之间仍有窗口，不承诺端到端恰好执行一次。

补充指令、取消和文字答复还会保存原始控制消息及目标 Session / Turn / 请求身份，恢复时只向仍然有效的原请求重投，不转成新 Turn；Core 在同一 Turn 中按 `steer_id` 去重补充指令。`/status` 优先读取宿主中真实的 Session 状态而不取得控制权，无法获取时显示未知。`allowed_users` 是可信用户名单，包含通过 `/permissions` 切换到完全访问的能力；当前没有独立的远程权限上限或本机二次确认。

## 附件与媒体

附件先经过身份、消息去重、Session 绑定和控制权检查，再用官方 SDK 的[获取消息中的资源文件](https://open.feishu.cn/document/server-docs/im-v1/message/get-2)接口下载到临时目录，交给 `agent_runtime.attachments.store_attachment` 保存到当前工作区的 `.nosis/attachments/`。普通文件保留原始文件名，图片按真实字节确定类型与扩展名；图文消息中的文字和多张图片合并为同一个 `user_turn.attachments`，纯附件消息也可发起 Turn。失败的下载会清理临时文件和本条消息已保存的附件。

流式下载时单文件上限 50 MiB，单条消息累计 100 MiB；单个资源最多 30 秒，一条消息的附件处理最多 60 秒。超限不提交 Turn 并清理本条消息的附件。这些限制不代替 Workspace 中历史附件的总存储配额。

Tool 附件沿用结构化 `tool_media` 事件，并通过 `present_to_user` 区分模型观察与显式交付：截图、`read_image`、`generate_image` 的图片默认只进入模型上下文，需要交付时由 Agent 调用 `present_file`。Core 把同一批中的观察媒体与交付附件分开发出，飞书只上传并发送显式交付的附件；`tool_delivery` 消息不进入模型上下文与压缩请求。单个附件发送失败只显示错误，继续处理后续附件与正文。

## 附着、审批与正文投递

一个 Session 只有一个控制附着。GUI 或飞书接管后原端停止控制，待处理的提问与审批从 Runtime 快照在新端重绘。运行期间不能切换工作区或新建替换当前会话，需先取消或等待结束。

`ask_user` 使用选项按钮，支持自由文本时可直接发送回复；审批使用“允许本次 / 拒绝”按钮，普通消息不会被解析成授权。按钮标识在宿主内映射到动作，并绑定用户、聊天、消息与当前请求，重复、转发、重启前或已结束请求的按钮不会再次执行。

普通输入通过检查并准备提交 Turn 时，在原消息上添加 `OnIt`（在做了）表情回复，不再单独发送处理中卡片。首张正文卡片成功发送后移除该表情；提交失败、Turn 结束、取消、接管或解除附着时也清理。表情 API 失败只记录错误，不阻止执行。

助手正文使用 CardKit JSON 2.0 卡片的 Markdown 组件。首个可见 `assistant_delta` 到来即创建并发送卡片实体，后续增量按约一秒的节奏向同一组件发送累计全文，`assistant_message` 到达时同步最终正文并关闭流式模式；推理增量与 Tool 原始输出不推送到聊天。每张卡片独立维护递增的 `sequence`，更新次数不再触发拆消息。飞书卡片体积上限为 30 KB，正文按 JSON 转义后的 UTF-8 字节计量，每段最多 24000 字节，为卡片结构预留空间，仅超过体积预算时续发卡片。卡片开启超过九分钟后的下一次文本更新先重新开启流式模式，避免平台十分钟自动关闭影响长回答。取消时保留已收到的正文，接管与解除附着时停止更新并关闭卡片。

正文 API 失败记录错误并继续消费事件，不新发普通消息替代；未成功投递的审批与提问仍由后续 Runtime 快照和通道恢复补绘。格式与限制以[流式更新卡片](https://open.feishu.cn/document/cardkit-v1/streaming-updates-openapi-overview)和[流式更新文本](https://open.feishu.cn/document/cardkit-v1/card-element/content)为准。

## 验证

```powershell
python -m pytest tests/test_application_host.py tests/test_mobile.py tests/test_mobile_streaming.py tests/test_gui.py tests/test_bridge_multiplex.py
```

测试覆盖初始绑定、持久化、授权身份隔离、过期与重复按钮、ask user、审批、steer、取消、忙碌状态、跨端接管与快照恢复。平台配置完成后还需在飞书中验证真实消息、卡片按钮与卡片更新。

其他参考：[长连接](https://open.feishu.cn/document/server-docs/event-subscription-guide/event-subscription-configure-/request-url-configuration-case)、[Node SDK](https://github.com/larksuite/node-sdk)、[卡片回传交互](https://open.feishu.cn/document/uAjLw4CM/ukzMukzMukzM/feishu-cards/card-callback-communication)。
