# 移动端 / IM 接入

```text
飞书应用机器人 ─ 官方 Node SDK 收发进程 ─ MobileController ─┐
                                                       ├─ Session Host ─ Bridge ─ agent_runtime ─ agent_core
GUI HTTP / WebSocket ────────────────────────────────────┘
TUI ─ @nosis/client ─ Bridge ─ agent_runtime ─ agent_core
```

`interfaces/mobile` 是交互层，不装配 Runtime，不执行 Agent 或 Tool，也不直接修改 Session Journal。命令和按钮最终只发送既有 Bridge 命令。`interfaces/session_host.py` 从 GUI 提取，复用后台 Session、事件快照、断线附着和显式接管规则，不依赖 FastAPI。

飞书官方 Node SDK 在独立进程中管理平台长连接、回调 ACK 和消息 API。Python 宿主用 JSONL 与该收发进程通信；凭证通过 stdin 传入，不放在进程参数中。此进程不持有 Runtime 或会话。不同 IM 平台实现 `views.Transport`，复用通道无关的交互控制器；平台卡片格式只存在于适配器中。

## 配置与启动

安装共享宿主依赖并构建收发进程：

```powershell
npm install
npm run build --workspace @nosis/mobile
uv sync --extra gui
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

密钥可以直接填写，也可用 `${ENV_NAME}` 引用进程环境变量或同目录 `.env`；`.env` 优先，文件内的值不做变量插值。`allowed_users` 使用当前应用下的 `open_id`，空数组拒绝所有用户；被拒绝用户的 open_id 会记录到本地日志，便于配置。第一版仅接收人工用户与机器人的单聊文本，不开放群聊或自动授权。

启动 GUI 时会自动加载已启用通道，与 GUI 共用同一 Session Host 和 Bridge：

```powershell
nosis-gui --workspace F:/Nosis
```

也可以从移动端入口启动同一个应用宿主：

```powershell
python -m interfaces.mobile --workspace F:/Nosis
# 或已安装的 nosis-mobile；可用 --config-directory 指定配置目录。
```

`nosis-gui` 和 `nosis-mobile` 是同一常驻应用宿主的两个入口。任意一个先启动，另一个都会识别并复用本机 `127.0.0.1:8737` 上的已有宿主；GUI 和飞书可同时使用，共享一份活动 Session 注册表、Bridge 和飞书连接。后启动的入口显示已有地址后返回，不重建 Runtime。宿主在启动 Bridge / IM 前预先绑定监听 socket，避免两个启动入口同时创建飞书连接。相同端口上的其他程序或不同配置目录会明确报错。

首次启动宿主的进程管理其生命周期，退出它会关闭 GUI HTTP 和移动通道。配置在宿主启动时读取；后启动入口不会修改现有默认 Workspace。TUI 仍拥有自己的 Bridge，已有 Journal 写锁继续阻止不同 Runtime 同时写同一 Session。

通道由 `interfaces/application_host.py` 装配并管理生命周期；GUI 不依赖移动端实现。IM 配置错误或收发进程启动失败时，宿主记录错误并释放移动端资源，GUI 仍可使用；修正后需重启宿主。未配置文件时不启动 IM 进程。构建产物随 Python wheel 的 `interfaces.mobile/dist/feishu.cjs` 分发。

## 飞书开放平台

使用企业自建应用，开启机器人能力。最小应用身份权限：

- `im:message.p2p_msg:readonly`：接收用户发给机器人的单聊消息。
- `im:message:readonly`：获取消息中的资源文件，下载用户上传的附件（只有单聊事件权限不足以调用资源下载接口）。
- `im:message:send_as_bot`：以机器人身份发送和更新消息卡片。
- `im:resource`：下载用户消息中的图片、文件，以及上传机器人回传的附件。

在“事件与回调”中分别配置事件和回调为长连接，并订阅：

- 事件 `im.message.receive_v1`。
- 回调 `card.action.trigger`（新版卡片回传交互）。

保存长连接方式时本地收发进程必须已连接。随后发布应用版本，设置所需可用范围。无需公网监听端口或内网穿透。

## 交互与会话绑定

首次普通消息不会创建隐式 Session，也不会自动选择最近会话。先 `/workspaces` 选择宿主工作区或已保存会话的工作区，再 `/sessions` 选择已有会话，或 `/new` 新建。工作区和会话菜单每页 8 项，提供下一页按钮。

| 输入 | 行为 |
| --- | --- |
| `/` | 显示可点击的命令菜单 |
| `/start`、`/help` | 显示使用方式 |
| `/workspaces` | 选择工作区 |
| `/sessions` | 恢复当前工作区的会话 |
| `/new` | 在当前工作区新建 Session |
| `/permissions` | 查看与切换当前 Session 的三种权限 preset |
| `/model` | 从已配置的 Provider 中选择当前会话模型 |
| `/provider` | 查看模型、Skill、Plugin 和 MCP 配置概览 |
| `/refresh` | 空闲时重载运行环境，保留会话、计划与权限 |
| `/status` | 查看工作区、会话、运行状态、模型与权限 |
| `/cancel` | 发送当前 Turn 的取消命令 |
| `/takeover` | 显式替换另一端的控制附着 |
| 普通文本 | 空闲时开始 Turn；执行期间发送 steer；允许自由输入的提问期间提交答案 |
| 图片、文件、图文消息 | 空闲时下载附件并开始 Turn；运行、提问或审批期间提示任务结束后重发 |

绑定按通道、单聊、用户隔离，保存在 `im_bindings.json`，不进入 Session Journal。该文件还保存最近 1000 条已接收消息的身份，阻止平台重投重复启动命令或 Turn；消息先登记再提交，因此宿主在这两步之间异常退出时需要用户重发。宿主重启后按需恢复同一 Session。新建时使用 Runtime 默认模型，恢复时由 Runtime 恢复会话模型、权限和计划。

附件先经过身份、消息去重、Session 绑定和控制权检查，再通过官方 SDK 的[获取消息中的资源文件](https://open.feishu.cn/document/server-docs/im-v1/message/get-2)接口下载到临时目录，交给 `agent_runtime.attachments.store_attachment` 保存到当前工作区的 `.nosis/attachments/`。普通文件保留原始文件名，图片按真实字节确定类型和扩展名；图文消息中的文字和多张图片合并为同一个 `user_turn.attachments`。纯附件消息也可发起 Turn。Bridge 继续使用 Runtime 的附件解析，Core 负责模型图片输入、文件提示、视觉降级和 Journal；适配层不复制这些执行语义。失败的下载会清理临时文件和本条消息已保存的附件。

Agent 的 `generate_image`、`read_image` 等现有图片输出和新增 `present_file` 文件交付，均沿用结构化 `tool_media` 事件。飞书端将事件附件通过 SDK 上传后发送为图片或文件消息；`present_file` 保存交付快照，避免后续编辑改变已交付文件。GUI 同样展示文件下载入口。单个附件发送失败会显示错误，继续处理后续附件和正文。平台权限的生效与版本发布要求以开发者后台提示为准；本地代码或 bundle 更新后需重启常驻宿主。

一个 Session 只有一个控制附着。GUI 或飞书接管后，原端停止控制；待处理提问 / 审批从 Runtime 快照在新端重绘。运行期间不能切换工作区或新建替换当前会话，先取消或等待结束。

ask user 使用选项按钮；支持自由文本时可直接发送回复。审批使用“允许本次 / 拒绝”按钮，不把普通消息解析成授权。按钮的随机标识仅在宿主内映射到动作，并绑定用户、聊天、消息及当前请求；重复、转发、重启前和已结束请求的按钮不会再次执行。

助手正文通过富文本 `post` 消息的 `md` 标签渲染 Markdown：支持标题、加粗、列表、引用、代码块、链接、表格和任务列表。首个 `assistant_delta` 到来即发送正文，后续增量按约一秒的节奏合并编辑同一消息；`assistant_message` 到来时立即同步最终正文。推理增量与 Tool 原始输出不推送到聊天。每次模型调用独立输出，不会在最终消息到来时重复发送全文。

飞书消息最多可编辑 20 次，达到次数或每段 6000 字符上限后，已发布的前缀固定，后续内容在新富文本消息续写。各条消息独立渲染，跨消息的 Markdown 结构不会连贯解析；流式生成期间未闭合的格式会随正文更新。部分 Markdown 样式需要新版飞书客户端；不支持任意 HTML、Setext 标题、缩进代码块和邮箱自动链接。图片需要使用飞书上传后获得的 image key。格式说明见[发送消息内容结构](https://open.feishu.cn/document/server-docs/im-v1/message-content-description/create_json?lang=zh-CN)。

取消或失败时保留已产生的正文；控制附着被替换时停止原端的后续文本更新。菜单、提问、审批与处理状态继续使用交互卡片；审批保留实际操作内容，供用户判断。平台约束见[编辑消息](https://open.feishu.cn/document/server-docs/im-v1/message/update)。

## 验证

```powershell
python -m pytest tests/test_application_host.py tests/test_mobile.py tests/test_mobile_streaming.py tests/test_gui.py tests/test_bridge_multiplex.py
```

移动端测试覆盖初始绑定、持久化、授权身份隔离、过期与重复按钮、ask user、审批、steer、取消、忙碌状态、跨端接管与快照恢复。平台配置完成后，还需在飞书中验证真实消息、卡片按钮和卡片更新。

官方参考：[长连接](https://open.feishu.cn/document/server-docs/event-subscription-guide/event-subscription-configure-/request-url-configuration-case)、[Node SDK](https://github.com/larksuite/node-sdk)、[卡片回传交互](https://open.feishu.cn/document/uAjLw4CM/ukzMukzMukzM/feishu-cards/card-callback-communication)。
