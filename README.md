# Nosis

个人 AI Agent，提供 TUI（Ink + React）和 GUI（React + FastAPI）。两个前端通过 Bridge 共用同一套 Agent Runtime、Tool、授权和 Session 行为。

```text
TUI ──────────┐
              ├── Bridge ── Agent Core
GUI ─ FastAPI ┘
```

## 快速开始

环境要求：Python >= 3.11、Node.js >= 22 和 [uv](https://docs.astral.sh/uv/)。

```bash
git clone https://github.com/EvannZhongg/Nosis.git
cd Nosis
npm install
npm run build
uv tool install --editable ".[gui]"
```

首次启动会在 `~/.nosis/` 生成配置和内置 Skill。要使用 OpenAI 兼容模型，可将
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

启动 TUI：

```bash
cd ~/projects/my-project
nosis
```

启动 GUI：

```bash
nosis-gui --workspace ~/projects/my-project
```

GUI 默认监听 <http://127.0.0.1:8737>。Provider、Tool、MCP 和子 Agent 配置见 [Agent Core](agent_core/README.md)。

## 项目结构

| 目录 | 职责 |
| --- | --- |
| [`agent_core`](agent_core/README.md) | Agent Runtime、Tool、Provider、Session |
| [`interfaces/bridge`](interfaces/bridge/README.md) | Runtime 装配与前端协议适配 |
| [`interfaces/protocol`](interfaces/protocol/README.md) | TUI/GUI 共用的 TypeScript 协议类型 |
| [`interfaces/tui`](interfaces/tui/README.md) | 终端界面 |
| [`interfaces/gui`](interfaces/gui/README.md) | Web 界面与 FastAPI 服务 |
| `tests` | Python Runtime 与接口测试 |

## 开发与测试

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[gui]"
python -m unittest discover -s tests -v

npm run typecheck
npm test
```

Windows 用户需要安装 [Git for Windows](https://git-scm.com/download/win)，`shell` Tool 使用其中的 Git Bash。
