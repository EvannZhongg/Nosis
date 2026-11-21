<div align="center">
  <img src="docs/nosis-banner.png" alt="Nosis" width="560">
  <br><br>
  <a href="README.md">English</a> &middot; <b>简体中文</b>
  <br><br>
  <a href="https://deepwiki.com/EvannZhongg/Nosis"><img src="https://img.shields.io/badge/Ask_DeepWiki-007ec6" alt="Ask DeepWiki"></a>
</div>

Nosis 是一个开源的轻量级个人 Agent 应用。它提供本地 Workspace 操作、人工授权、会话持久化、上下文管理、长期记忆、计划、子 Agent、定时任务，以及 Skill、Plugin、MCP 扩展能力，并提供 TUI、GUI 和飞书移动端接入。

```text
TUI ──────────────────────────┐
GUI ── HTTP / WebSocket ──────┼── Bridge ── agent_runtime ── agent_core
飞书 ── IM 适配器 ─────────────┘
```

- `agent_core` 提供与前端无关的 Agent 机制。
- `agent_runtime` 读取应用配置并把 Core 组装成可运行实例。
- Bridge 作为独立进程，是前端驱动 Runtime 的唯一通道。
- TUI、GUI 与移动端宿主通过 Bridge 协议共享执行状态和交互语义；Bridge 子进程通道使用 newline-delimited JSON。

## 主要能力

- **受控本地执行**：Session 支持 `Ask for approval`、`Workspace Access` 和 `Full Access` 三种权限 preset；Shell 根据 scope 进入 Workspace 沙箱或宿主环境，文件 Tool 始终限制在当前 Workspace。
- **桌面自动化**：主 Agent 可通过 `computer_screenshot` 与 `computer_action` 观察并操作可见桌面，覆盖截图、点击、拖拽、滚动、按键与文本输入；两项 Tool 默认开启且只对主 Agent 可见，调用时按当前权限 preset 确认。动作默认返回截图，可通过 `screenshot: false` 跳过。Windows 还支持裁剪窗口范围内的可见画面和窗口相对坐标。执行窗口动作时，将最近一次截图路径作为 `observation`，并使用该窗口的 `hwnd:<十进制>` 作为目标。
- **Web 检索**：`web_fetch` 直接或通过 DeepSeek 读取公开网页；启用 `web_search` 后默认使用 DuckDuckGo 检索，也可切换为 Exa 或 DeepSeek。
- **完整 Agent Loop**：支持流式响应、Tool 批次、上下文压缩、steer、取消、计划和后台 Job。
- **可恢复 Session**：对话和执行事件写入 append-only JSONL Journal，TUI 与 GUI 可读取同一类 Session。
- **记忆与定时任务**：维护全局及 Workspace 长期记忆，并可创建 one-shot、interval 和 cron 任务。
- **模型与媒体**：OpenAI-compatible 端点通过原生 OpenAI SDK 接入，其他路由及图片生成使用 LiteLLM；可为主 Agent、视觉、图片生成、辅助模型调用及不同子 Agent 角色独立路由 Provider。
- **扩展系统**：支持 standalone Skill、Plugin 提供的 Skill / Agent / MCP / [Hook](docs/plugin-hooks.md) 组件，以及独立 MCP Server。
- **多种前端**：TUI 适合终端工作流；GUI 提供多会话、附件、文件树、设置、记忆和定时任务管理；飞书支持工作区与会话选择、文本流式对话、提问和审批卡片。

## 快速开始

环境要求：Python >= 3.11、Node.js >= 22 和 [uv](https://docs.astral.sh/uv/)。

```bash
git clone https://github.com/EvannZhongg/Nosis.git
cd Nosis
npm install
npm run build
uv tool install --editable ".[server]"
```

需要通过项目环境使用 GUI 的 HTTP / WebSocket 接入时，安装 server extra：

```bash
uv sync --extra server
```

先在当前目录启动一次 TUI：

```bash
nosis
```

首次启动会初始化 `~/.nosis/`。退出 TUI 后，创建 `~/.nosis/.env` 并填入默认 Provider 所需的环境变量，例如：

```dotenv
OPENAI_KEY=your-api-key
```

配置完成后重新启动 TUI：

```bash
nosis
```

或启动 GUI / IM 共享服务：

```bash
nosis-server
```

GUI 默认监听 <http://127.0.0.1:8737>。

飞书移动端支持工作区与会话选择、普通文本流式对话、提问和审批卡片；配置与启动见 [移动端接入](interfaces/mobile/README.md)。`nosis-server` 统一管理并列运行的 HTTP 与 IM 适配器，共享活动 Session 和 Bridge。`nosis-server --no-http` 不加载 HTTP 依赖，也不监听本地端口。同一配置目录通过进程锁保证单实例，重复启动会明确报错。

## 模块文档

第三方前端可通过已安装的 `nosis-bridge` 命令使用
[公开 stdio 协议](docs/bridge-protocol.md)，或使用轻量的
[`@nosis/client` TypeScript SDK](interfaces/client/README.md)。

完整目录见 [文档索引](docs/README.md)，包含公开契约、两个 Runtime 层以及各前端与 SDK 文档。

## 配置与数据

`agent_runtime` 首次启动时创建配置目录，并负责读取与更新其中的应用配置：

| 路径 | 内容 |
| --- | --- |
| `~/.nosis/provider_config.json` | Provider、模型和角色路由（含可选的 `fast_model`） |
| `~/.nosis/agent_config.json` | Tool、上下文、记忆、Skill 路径、子 Agent、MCP、Workspace Instructions 与 scratch Workspace 设置 |
| `~/.nosis/im_channels.json` | IM 通道、凭证与飞书授权用户 |
| `~/.nosis/tool_config.json` | Web Search、Web Fetch 的 provider 选择与 API key |
| `~/.nosis/.env` | 配置引用的密钥和环境变量 |
| `~/.nosis/prompts/` | 主 Agent、子 Agent、压缩与记忆 Prompt |
| `~/.nosis/skills/` | standalone Skill |
| `~/.nosis/plugins/` | Plugin capability package |
| `~/.nosis/AGENTS.md` | 用户级全局 Workspace Instruction |
| `~/.nosis/MEMORY.md` | 全局长期记忆 |
| `~/.nosis/sessions/` | Session Journal、Workspace 记忆、Artifact 与子 Agent 记录 |
| `~/.nosis/schedule.jsonl` | 定时任务及运行记录 |
| `<workspace>/.nosis/attachments/` | 上传附件和 Agent 生成的图片 |

默认配置与内置资源位于 [`agent_runtime/defaults/`](agent_runtime/defaults)。具体装配方式见 [`agent_runtime/README.md`](agent_runtime/README.md)。

`agent_config.json` 中的 `default_permission_preset` 控制新 Session 的初始权限，可选 `ask_for_approval`、`workspace_access`、`full_access`，出厂值为 `ask_for_approval`，字段缺失时同样取该值。也可在 GUI 的 Settings → Agent → 默认权限 中修改；保存后对新建 Session 生效，已有 Session 保留其保存的权限。

## 开发与测试

Python 测试使用 Mock Provider，不需要真实 API Key：

```bash
python -m venv .venv
source .venv/bin/activate
rustup toolchain install stable
cargo build --release --manifest-path nosis-sandbox/Cargo.toml
python -m pip install -e ".[server,test]"
python -m pytest --durations=20
```

在 Windows 上改用 `.venv\Scripts\Activate.ps1` 激活环境。从源码安装需要 Rust stable 工具链；平台 wheel 已包含对应的 `nosis-sandbox` 可执行文件。

Pytest 默认用 4 个独立进程按测试类并行运行；串行调试使用 `-n 0`，资源较少时使用 `-n 2`。完整测试包含安装包验证及真实本地 MCP/沙箱进程；日常迭代可用 `--ignore=tests/test_bridge_client.py` 跳过安装包验证。独立插件测试包的运行方式见 [docs/plugin-interop.md](docs/plugin-interop.md)。

server extra 包含 FastAPI、Uvicorn、WebSocket 后端和 multipart 支持；Windows、macOS 与 Linux 使用相同的安装方式。TUI 与 `nosis-server --no-http` 无需安装该可选依赖。

前端构建、类型检查和测试：

```bash
npm run build
npm run typecheck
npm test
```

Linux 沙箱集成测试需要可用的 bubblewrap 和 user namespace；未满足运行条件时测试会明确 skip。

运行 `cargo test --manifest-path nosis-sandbox/Cargo.toml` 检查原生 helper 协议及沙箱权限。`build-platform-wheels` 工作流覆盖 Windows x64、macOS 11+（Apple Silicon）与 Linux（glibc 2.28+）x86-64 / aarch64。每个 `py3-none-<platform>` wheel 支持 Python 3.11+，包含原生 helper 以及构建后的 TUI、GUI 和 IM 资源，并在安装后实际执行 helper 验证打包与协议。

内部 helper 协议先输出一行 JSON `protocol_version` 问候，再接收一条以换行结束的 JSON 请求，最后返回一条携带相同版本号的 JSON 结果。Python 在发送命令前校验问候；helper 在解析执行字段或初始化沙箱前校验请求版本。修改契约时，需同步更新并递增 `agent_core/execution/process.py` 的 `HELPER_PROTOCOL_VERSION`、`nosis-sandbox/src/main.rs` 的 `PROTOCOL_VERSION` 与 `tests/smoke_wheel.py` 中的请求。请求之后 stdin 是取消通道：任一字节或 EOF 都会取消命令，需保持打开直到执行结束。

Windows 上 Shell Tool 使用 PowerShell 7，因此需要另外安装 PowerShell 7。

## Contributors

- [AKArrok](https://github.com/AKArrok)：提出并验证 Session Journal 尾记录追加问题（[#2](https://github.com/EvannZhongg/Nosis/pull/2)）。
- [George Pickett](https://github.com/georgeatparallel)：贡献 Parallel Search MCP 集成（[#4](https://github.com/EvannZhongg/Nosis/pull/4)）。
- [Pocket99](https://github.com/Pocket99)：贡献最初的 GUI，含会话、Workspace 浏览与模型切换（[#1](https://github.com/EvannZhongg/Nosis/pull/1)）。
- [Guillermo Prieto Rodriguez](https://github.com/guillermop2002)：报告了文件 Tool 的路径竞态问题：沙箱内的 Shell 可借此把 Workspace 的读写重定向到 Workspace 之外。

## 许可

[MIT](LICENSE)
