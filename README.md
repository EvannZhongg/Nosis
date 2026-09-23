# Nosis

Nosis 是一个开源的轻量级个人 Agent Runtime：执行边界由 Session 自行声明，越界命令在 OS 级沙箱中运行；内置工具集与 Plugin / Skill / MCP 扩展机制齐备，GUI 与 TUI 共享 Agent Runtime，会话、计划与记忆以文件持久化。

核心是一个不依赖 Agent 框架的 Python Runtime：TUI 与 Web GUI 共用同一套 Runtime、Tool、授权与 Session。

```text
TUI ──────────┐
              ├── Bridge ── Agent Core
GUI ─ FastAPI ┘
```

Bridge 以独立进程运行，是前端驱动 Agent 的唯一通道，两端用 newline-delimited JSON 交换协议消息。

## 能力

- **本地执行，人工确认**：Session 支持 `Ask for approval`、`Workspace Access` 与 `Full Access`，选择随 Session 持久化。每个权限模式决定 Shell 的默认执行边界与授权规则：`Ask for approval` 和 `Workspace Access` 默认在沙箱内运行，前者对两种 scope 都请求授权、后者只对越过沙箱的 `host` 请求；`Full Access` 默认直接以 `host` 运行且不再请求授权，仍可显式请求 `workspace`。macOS 使用 Seatbelt、Linux 使用 bubblewrap，二者仅暴露最小只读系统视图并断开网络；Windows 的 RestrictedToken backend 保留宿主文件读取权限、允许网络，并主要把写权限授予 Workspace 与私有临时目录；已对 Everyone 开放写入的位置属于明确例外。无人值守的定时任务最多获得 `Workspace Access`。文件类 Tool 只作用于当前 Workspace。
- **Tool 与扩展**：内置文件读写、搜索、Shell、Web Search、图片读取与生成、子 Agent 等 Tool；可接入 MCP Server，并可用 Skill 扩展工作流。
- **模型路由**：主 Agent、子 Agent 与每个角色都可以单独选择 Provider（LiteLLM 支持多家模型），图片分析与图片生成可以独立配置 Provider。
- **子 Agent 与计划**：按角色派发子 Agent（默认 `researcher` 只读），主 Agent 维护跨轮次的执行计划，两个前端都展示进度。
- **会话持久化**：对话与执行事件以 append-only JSONL Journal 保存，重启后可以恢复；TUI 用 `/sessions` 切换历史会话，GUI 从侧边栏打开并默认回到上次的会话。
- **上下文管理**：接近预算时自动压缩较早的历史并保留最近若干轮，另有硬上限；窗口占用在 GUI 中可见。
- **一个 Runtime，两个前端**：驱动 Agent、执行 Tool、授权与取消只实现一次，TUI 与 GUI 只渲染状态并采集输入。
- **统一设置**：GUI 可编辑 Provider 与 Agent 配置并查看 Skill、Plugin、MCP；TUI 可用 `/model` 切换当前 Session 的 Provider，并用 `/provider` 查看配置概览。

## 快速开始

环境要求：Python >= 3.11、Node.js >= 22 和 [uv](https://docs.astral.sh/uv/)。

```bash
git clone https://github.com/EvannZhongg/Nosis.git
cd Nosis
npm install
npm run build
uv tool install --editable ".[gui]"
```

首次启动会在 `~/.nosis/` 生成配置、Prompt 模板、内置 Skill 与内置 Plugin。要使用 OpenAI 兼容模型，可将
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

不绑定已有项目时，可以创建一个持久保留的独立临时工作区：

```bash
nosis --temporary
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
| `~/.nosis/agent_config.json` | Tool、子 Agent、MCP、上下文压缩与 Scratch Workspace 根路径 |
| `~/.nosis/prompts/` | 主 Agent、子 Agent 与上下文压缩的 Prompt 模板 |
| `~/.nosis/skills/` | Skill 目录，首次启动安装内置 Skill |
| `~/.nosis/plugins/` | Plugin capability package；由 `plugin.json` 声明组件引用，首次启动安装内置 Plugin |
| `~/.nosis/AGENTS.md` | 用户级全局 Workspace Instruction |
| `~/.nosis/MEMORY.md` | 跨 Workspace 的 Global Memory |
| `~/.nosis/.env` | 配置中 `${ENV_NAME}` 引用的密钥 |
| `~/.nosis/workspaces/scratch/<workspace-id>/` | Nosis 创建的临时工作区；根路径由 `scratch_workspace_root` 配置，最后一个使用它的 Session 被删除时一并删除 |
| `~/.nosis/sessions/<workspace-key>/MEMORY.md` | 当前 Workspace 独立的长期记忆 |
| `~/.nosis/sessions/<workspace-key>/<session-id>/` | Session 的 Journal、权限 preset、大输出与子 Agent 记录 |
| `<workspace>/.nosis/attachments/` | GUI 上传的附件与 Agent 生成的图片 |

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

Windows 用户需要安装 [PowerShell 7](https://learn.microsoft.com/powershell/scripting/install/installing-powershell-on-windows)。Windows 上不论权限 scope，`shell` Tool 都使用 PowerShell 7；Shell Tool 只承诺 PowerShell 方言，不承诺直接运行 POSIX `.sh` 脚本。

## 许可

[MIT](LICENSE)。
