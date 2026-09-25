<div align="center"><img src="docs/nosis-banner.png" alt="Nosis" width="560"></div>
Nosis 是一个开源的轻量级个人 Agent 应用。它提供本地 Workspace 操作、人工授权、会话持久化、上下文管理、长期记忆、计划、子 Agent、定时任务，以及 Skill、Plugin、MCP 扩展能力，并同时提供 TUI 和 GUI。

```text
TUI ────────────┐
                ├── Bridge ── agent_runtime ── agent_core
GUI ── FastAPI ─┘
```

- `agent_core` 提供与前端无关的 Agent 机制。
- `agent_runtime` 读取应用配置并把 Core 组装成可运行实例。
- Bridge 作为独立进程，是前端驱动 Runtime 的唯一通道。
- TUI 与 GUI 宿主通过 Bridge 协议共享执行状态和交互语义；Bridge 子进程通道使用 newline-delimited JSON。

## 主要能力

- **受控本地执行**：Session 支持 `Ask for approval`、`Workspace Access` 和 `Full Access` 三种权限 preset；Shell 根据 scope 进入 Workspace 沙箱或宿主环境，文件 Tool 始终限制在当前 Workspace。
- **完整 Agent Loop**：支持流式响应、Tool 批次、上下文压缩、steer、取消、计划和后台 Job。
- **可恢复 Session**：对话和执行事件写入 append-only JSONL Journal，TUI 与 GUI 可读取同一类 Session。
- **记忆与定时任务**：维护全局及 Workspace 长期记忆，并可创建 one-shot、interval 和 cron 任务。
- **模型与媒体**：通过 LiteLLM 接入模型，可为主 Agent、视觉、图片生成及不同子 Agent 角色独立路由 Provider。
- **扩展系统**：支持 standalone Skill、Plugin 提供的 Skill / Agent / MCP 组件，以及独立 MCP Server。
- **两个前端**：TUI 适合终端工作流；GUI 提供多会话、附件、文件树、设置、记忆和定时任务管理。

## 快速开始

环境要求：Python >= 3.11、Node.js >= 22 和 [uv](https://docs.astral.sh/uv/)。

```bash
git clone https://github.com/EvannZhongg/Nosis.git
cd Nosis
npm install
npm run build
uv tool install --editable ".[gui]"
```

先在当前目录启动一次 TUI：

```bash
nosis
```

首次启动会初始化 `~/.nosis/`。退出 TUI 后，创建 `~/.nosis/.env` 并填入默认 Provider 所需的环境变量，例如：

```dotenv
OPENAI_KEY=your-api-key
```

配置完成后重新启动TUI：

```bash
nosis
```

或启动 GUI：

```bash
nosis-gui
```

GUI 默认监听 <http://127.0.0.1:8737>。

## 模块文档

| 文档 | 负责范围 |
| --- | --- |
| [`agent_core`](agent_core/README.md) | Agent Loop、上下文、Session、Tool、执行、权限、记忆、计划、Job、Scheduler、Provider、Skill 与 MCP 机制 |
| [`agent_runtime`](agent_runtime/README.md) | 配置、Prompt、Plugin 发现、Runtime 装配、Execution Plane、Session 绑定和 Turn 入口 |
| [`interfaces/bridge`](interfaces/bridge/README.md) | 独立 Bridge 进程、协议路由、事件转换和前端交互转发 |
| [`interfaces/protocol`](interfaces/protocol/README.md) | TUI 与 GUI 共用的 TypeScript 协议类型和连接状态约定 |
| [`interfaces/tui`](interfaces/tui/README.md) | Ink 终端界面、命令、输入状态和 Bridge 子进程接入 |
| [`interfaces/gui`](interfaces/gui/README.md) | FastAPI 服务、React 界面、多会话连接、HTTP / WebSocket API 与媒体访问 |

## 配置与数据

`agent_runtime` 首次启动时创建配置目录，并负责读取与更新其中的应用配置：

| 路径 | 内容 |
| --- | --- |
| `~/.nosis/provider_config.json` | Provider、模型和角色路由 |
| `~/.nosis/agent_config.json` | Tool、上下文、记忆、子 Agent、MCP、Workspace Instructions 与 scratch Workspace 设置 |
| `~/.nosis/.env` | 配置引用的密钥和环境变量 |
| `~/.nosis/prompts/` | 主 Agent、子 Agent、压缩与记忆 Prompt |
| `~/.nosis/skills/` | standalone Skill |
| `~/.nosis/plugins/` | Plugin capability package |
| `~/.nosis/AGENTS.md` | 用户级全局 Workspace Instruction |
| `~/.nosis/MEMORY.md` | 全局长期记忆 |
| `~/.nosis/sessions/` | Session Journal、Workspace 记忆、Artifact 和子 Agent 记录 |
| `~/.nosis/schedule.jsonl` | 定时任务及运行记录 |
| `<workspace>/.nosis/attachments/` | 上传附件和 Agent 生成的图片 |

默认配置与内置资源位于 [`agent_runtime/defaults/`](agent_runtime/defaults)。具体装配方式见 [`agent_runtime/README.md`](agent_runtime/README.md)。

## 开发与测试

Python 测试使用 Mock Provider，不需要真实 API Key：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[gui]"
python -m unittest discover -s tests -v
```

前端构建、类型检查和测试：

```bash
npm run build
npm run typecheck
npm test
```

Linux 沙箱集成测试需要可用的 bubblewrap 和 user namespace；未满足运行条件时测试会明确 skip。

Windows 上 Shell Tool 使用 PowerShell 7，因此需要另外安装 PowerShell 7。

## Contributors

- [AKArrok](https://github.com/AKArrok)：提出并验证 Session Journal 尾记录追加问题。
- [Pocket99](https://github.com/Pocket99)（ZIRUI QIU）：贡献最初的 GUI，含会话、Workspace 浏览与模型切换（[#1](https://github.com/EvannZhongg/Nosis/pull/1)）。

## 许可

[MIT](LICENSE)
