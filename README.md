# Nosis

Nosis 是一个轻量级、开源、可自托管的个人 Agent 框架。它在终端与浏览器中运行，把工具调用、会话持久化、MCP 集成、模型路由与多代理委托整合在一起。

核心是一个不依赖 Agent 框架的 Python Runtime：TUI 与 Web GUI 共用同一套 Runtime、Tool、授权与 Session。

```text
TUI ──────────┐
              ├── Bridge ── Agent Core
GUI ─ FastAPI ┘
```

Bridge 以独立进程运行，是前端驱动 Agent 的唯一通道，两端用 newline-delimited JSON 交换协议消息。

## 能力

- **本地执行，人工确认**：Shell 与需要确认的 MCP Tool 在执行前请求授权；Session 可在 `Ask for approval` 与 `Full Access` 之间切换，选择随 Session 持久化。文件类 Tool 只作用于当前 Workspace。
- **Tool 与扩展**：内置文件读写、搜索、Shell、Web Search、图片读取、子 Agent 等 Tool；可接入 MCP Server，并可用 Skill 扩展工作流。
- **模型路由**：主 Agent、子 Agent 与每个角色都可以单独选择 Provider（LiteLLM 支持多家模型），图片分析可另配 vision Provider。
- **子 Agent 与计划**：按角色派发子 Agent（默认 `researcher` 只读），主 Agent 维护跨轮次的执行计划，两个前端都展示进度。
- **会话持久化**：对话与执行事件以 append-only JSONL Journal 保存，重启后可以恢复；TUI 用 `/sessions` 切换历史会话，GUI 从侧边栏打开并默认回到上次的会话。
- **上下文管理**：接近预算时自动压缩较早的历史并保留最近若干轮，另有硬上限；窗口占用在 GUI 中可见。
- **一个 Runtime，两个前端**：驱动 Agent、执行 Tool、授权与取消只实现一次，TUI 与 GUI 只渲染状态并采集输入。

## 快速开始

环境要求：Python >= 3.11、Node.js >= 22 和 [uv](https://docs.astral.sh/uv/)。

```bash
git clone https://github.com/EvannZhongg/Nosis.git
cd Nosis
npm install
npm run build
uv tool install --editable ".[gui]"
```

首次启动会在 `~/.nosis/` 生成配置、Prompt 模板和内置 Skill。要使用 OpenAI 兼容模型，可将
`~/.nosis/provider_config.json` 精简为下面的最小配置（保留其他 Provider 也可以）：

```json
{
  "main_agent": {"provider": "openai"},
  "providers": {
    "openai": {
      "model": "openai/gpt-5",
      "url": "https://api.openai.com/v1",
      "key": "${OPENAI_KEY}"
    }
  }
}
```

在 `~/.nosis/.env` 中填写密钥即可：

```dotenv
OPENAI_KEY=your-api-key
```

终端界面由 Node 运行，启动前请确认 `node` 在 PATH 中：

```bash
cd ~/projects/my-project
nosis
```

启动 GUI：

```bash
nosis-gui --workspace ~/projects/my-project
```

GUI 默认监听 <http://127.0.0.1:8737>。

## 文档导航

| 文档 | 内容 |
| --- | --- |
| [`agent_core`](agent_core/README.md) | Agent Runtime、Tool、Provider、Session |
| [`interfaces/bridge`](interfaces/bridge/README.md) | Runtime 装配与前端协议适配 |
| [`interfaces/protocol`](interfaces/protocol/README.md) | TUI/GUI 共用的 TypeScript 协议类型 |
| [`interfaces/tui`](interfaces/tui/README.md) | 终端界面、命令行参数与快捷键 |
| [`interfaces/gui`](interfaces/gui/README.md) | Web 界面与 FastAPI 服务 |
| [`tests`](tests) | Python Runtime 与接口测试 |

## 数据与配置

| 路径 | 内容 |
| --- | --- |
| `~/.nosis/provider_config.json` | Provider、模型与角色对应关系 |
| `~/.nosis/agent_config.json` | Tool、子 Agent、MCP 与上下文压缩 |
| `~/.nosis/prompts/` | 主 Agent、子 Agent 与上下文压缩的 Prompt 模板 |
| `~/.nosis/skills/` | Skill 目录，首次启动安装内置 Skill |
| `~/.nosis/AGENTS.md` | 用户级全局 Workspace Instruction |
| `~/.nosis/.env` | 配置中 `${ENV_NAME}` 引用的密钥 |
| `~/.nosis/sessions/<workspace-key>/<session-id>/` | Session 的 Journal、权限 preset、大输出与子 Agent 记录 |
| `<workspace>/.nosis/attachments/` | GUI 上传的图片 |

字段与结构见 [Agent Core](agent_core/README.md)，默认配置见 [`interfaces/bridge/defaults/`](interfaces/bridge/defaults)。

## 开发与测试

Python 测试使用 Mock Provider，不需要真实 API Key：

```bash
python -m venv .venv
source .venv/bin/activate          # Windows PowerShell: .\.venv\Scripts\Activate.ps1
python -m pip install -e ".[gui]"
python -m unittest discover -s tests -v
```

前端测试和类型检查：

```bash
npm run typecheck
npm test
```

## 说明

Windows 用户需要安装 [Git for Windows](https://git-scm.com/download/win)，`shell` Tool 使用其中的 Git Bash。

## 许可

[MIT](LICENSE)。
