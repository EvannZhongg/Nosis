<div align="center">
  <img src="docs/nosis-banner.png" alt="Nosis" width="560">
  <br><br>
  <b>English</b> &middot; <a href="README.zh-CN.md">简体中文</a>
  <br><br>
  <a href="https://deepwiki.com/EvannZhongg/Nosis"><img src="https://img.shields.io/badge/Ask_DeepWiki-007ec6" alt="Ask DeepWiki"></a>
</div>

<!-- yarkr:badges:start -->
[![Indexed on Yarkr](https://api.yarkr.com/functions/v1/api/api/v1/badges/repos/EvannZhongg/Nosis/version.svg)](https://yarkr.com/arks/evannzhongg/nosis)
<!-- yarkr:badges:end -->

Nosis is an open-source, lightweight personal agent application. It provides local workspace operations, human approval, session persistence, context management, long-term memory, plans, subagents, scheduled tasks, and Skill / Plugin / MCP extensibility — with a TUI, a GUI, and Feishu messaging access.

```text
TUI ──────────────────────────┐
GUI ── HTTP / WebSocket ──────┼── Bridge ── agent_runtime ── agent_core
Feishu ── IM adapter ─────────┘
```

- `agent_core` provides frontend-agnostic agent mechanisms.
- `agent_runtime` reads application configuration and assembles the Core into a runnable instance.
- Bridge runs as a standalone process and is the only channel through which a frontend drives the Runtime.
- The TUI, GUI, and messaging hosts share execution state and interaction semantics through the Bridge protocol; the Bridge subprocess channel uses newline-delimited JSON.

## Key capabilities

- **Controlled local execution**: A session supports three permission presets — `Ask for approval`, `Workspace Access`, and `Full Access`. Shell enters the workspace sandbox or the host environment according to scope, while file tools are always confined to the current workspace.
- **Desktop automation**: The main agent can observe and operate the visible desktop through `computer_screenshot` and `computer_action`, covering screenshots, clicks, drags, scrolling, key presses, and text input. Both tools are on by default and visible only to the main agent, and each call is confirmed according to the current permission preset. Actions return a screenshot by default and can skip it with `screenshot: false`. On Windows the tools also support visible-window crops and window-relative input. Pass the latest screenshot path as `observation` and use its `hwnd:<decimal>` for a window-scoped action.
- **Web research**: `web_fetch` reads public pages directly or via DeepSeek; `web_search`, when enabled, queries DuckDuckGo by default and can switch to Exa or DeepSeek.
- **A complete agent loop**: streaming responses, tool batches, context compaction, steer, cancel, plans, and background jobs.
- **Resumable sessions**: conversation and execution events are written to an append-only JSONL journal, and the TUI and GUI can read the same kind of session.
- **Memory and scheduled tasks**: global and workspace-scoped long-term memory, plus one-shot, interval, and cron tasks.
- **Models and media**: OpenAI-compatible endpoints use the native OpenAI SDK; other routes and image generation use LiteLLM. Providers can be routed independently for the main agent, vision, image generation, auxiliary model calls, and each subagent role.
- **Extension system**: standalone skills, skills / agents / MCP / [command hooks](docs/plugin-hooks.md) provided by plugins, and standalone MCP servers.
- **Multiple frontends**: the TUI suits terminal workflows; the GUI offers multi-session management, attachments, a file tree, settings, memory, and scheduled tasks; Feishu supports workspace and session selection, streaming text, questions, and approval cards.

## Quick start

Requirements: Python >= 3.11, Node.js >= 22, and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/EvannZhongg/Nosis.git
cd Nosis
npm install
npm run build
uv tool install --editable ".[server]"
```

To enable HTTP / WebSocket access in the project environment, install the server extra:

```bash
uv sync --extra server
```

Start the TUI once in the current directory:

```bash
nosis
```

The first launch initializes `~/.nosis/`. After quitting the TUI, create `~/.nosis/.env` and fill in the environment variables required by the default provider, for example:

```dotenv
OPENAI_KEY=your-api-key
```

Then restart the TUI:

```bash
nosis
```

Or start the shared GUI / IM server:

```bash
nosis-server
```

The GUI listens on <http://127.0.0.1:8737> by default.

Feishu messaging access is configured in `~/.nosis/im_channels.json`; see [Mobile / IM setup](interfaces/mobile/README.md). `nosis-server` runs HTTP and IM adapters alongside a shared Session Host and Bridge. Use `nosis-server --no-http` to run without HTTP dependencies or a local listening port. A configuration-directory process lock prevents duplicate servers; a second launch reports that the server is already running.

<!-- yarkr:install:start -->
## Install with Yarkr

This repo is indexed on [Yarkr](https://yarkr.com/arks/evannzhongg/nosis) as the ark [evannzhongg/nosis](https://yarkr.com/arks/evannzhongg/nosis), so its skills install into Claude Code, Codex, Cursor and other agent harnesses with one command.

### CLI

Install the whole ark, which Yarkr keeps equal to this repo:

```bash
npx yarkr-cli sync evannzhongg/nosis
```

Or add individual skills:

```bash
npx yarkr-cli add skill:evannzhongg/nosis/code-review
npx yarkr-cli add skill:evannzhongg/nosis/plugin-creator
npx yarkr-cli add skill:evannzhongg/nosis/skill-creator
```

No Node.js? `curl -fsSL https://yarkr.com/install.sh | sh` (macOS, Linux) or `irm https://yarkr.com/install.ps1 | iex` (Windows PowerShell) installs `yarkr` as a standalone binary; then type `yarkr` in place of `npx yarkr-cli`.

### MCP

Connect your agent to the ark over MCP:

```bash
npx yarkr-cli mcp add evannzhongg/nosis
```

Or point any MCP client at `https://api.yarkr.com/functions/v1/api/mcp/o/evannzhongg/nosis`.
<!-- yarkr:install:end -->

## Module documentation

Third-party frontends can launch the installed `nosis-bridge` command and use the
[public stdio protocol](docs/bridge-protocol.md) or the lightweight
[`@nosis/client` TypeScript SDK](interfaces/client/README.md).

All documentation is indexed in [docs/README.md](docs/README.md), including the
public contracts, the two runtime layers, and every frontend / SDK document.

## Configuration and data

`agent_runtime` creates the configuration directory on first launch and is responsible for reading and updating the application configuration inside it:

| Path | Contents |
| --- | --- |
| `~/.nosis/provider_config.json` | Providers, models, and role routing, including the optional `fast_model` |
| `~/.nosis/agent_config.json` | Tools, context, memory, skill paths, subagents, MCP, workspace instructions, and scratch workspace settings |
| `~/.nosis/im_channels.json` | Messaging channels, credentials, and authorized Feishu users |
| `~/.nosis/tool_config.json` | Web Search and Web Fetch provider selection and API keys |
| `~/.nosis/.env` | The secrets and environment variables referenced by the configuration |
| `~/.nosis/prompts/` | Main agent, subagent, compaction, and memory prompts |
| `~/.nosis/skills/` | Standalone skills |
| `~/.nosis/plugins/` | Plugin capability packages |
| `~/.nosis/AGENTS.md` | User-level global workspace instruction |
| `~/.nosis/MEMORY.md` | Global long-term memory |
| `~/.nosis/sessions/` | Session journals, workspace memory, artifacts, and subagent records |
| `~/.nosis/schedule.jsonl` | Scheduled tasks and run records |
| `<workspace>/.nosis/attachments/` | Uploaded attachments and agent-generated images |

The default configuration and built-in resources live in [`agent_runtime/defaults/`](agent_runtime/defaults). See [`agent_runtime/README.md`](agent_runtime/README.md) for how assembly works.

`default_permission_preset` in `agent_config.json` sets initial permissions for new sessions: `ask_for_approval`, `workspace_access`, or `full_access`. It ships as `ask_for_approval`, falls back to it when the field is absent, and can also be changed under Settings → Agent → 默认权限 in the GUI. Changes apply to new sessions; existing sessions retain their saved permissions.

## Development and testing

Python tests use a mock provider and need no real API key:

```bash
python -m venv .venv
source .venv/bin/activate
rustup toolchain install stable
cargo build --release --manifest-path nosis-sandbox/Cargo.toml
python -m pip install -e ".[server,test]"
python -m pytest --durations=20
```

On Windows, activate the environment with `.venv\Scripts\Activate.ps1`. Installing from a source checkout requires the Rust stable toolchain; release wheels contain the matching `nosis-sandbox` executable for Windows, macOS, or Linux.

Pytest distributes test classes across four isolated worker processes. Use `-n 0` for a serial run or `-n 2` on smaller machines. The full suite includes package installation and real local MCP/sandbox processes; `--ignore=tests/test_bridge_client.py` omits the package installation check during iteration. The standalone plugin testkit has its own command in [docs/plugin-interop.md](docs/plugin-interop.md).

The server extra includes FastAPI, Uvicorn, the WebSocket backend, and multipart support; Windows, macOS, and Linux use the same installation. The TUI and `nosis-server --no-http` do not require this extra.

Frontend build, type checking, and tests:

```bash
npm run build
npm run typecheck
npm test
```

The Linux sandbox integration tests require a working bubblewrap and user namespaces; when the runtime conditions are not met, the tests explicitly skip.

Run the native helper and sandbox permission tests with `cargo test --manifest-path nosis-sandbox/Cargo.toml`. Platform wheels are built in the `build-platform-wheels` workflow.

The wheel matrix covers Windows x64, macOS 11+ on Apple Silicon, and Linux with glibc 2.28+ on x86-64 and aarch64. Each `py3-none-<platform>` wheel supports Python 3.11+ and includes the native helper and the built TUI, GUI, and IM assets. The workflow installs each wheel and executes its helper to check the package and protocol.

The internal helper protocol is version 2: a JSON `protocol_version` greeting, followed by one newline-terminated JSON request and one JSON result carrying the same version. Python validates the greeting before sending a command; the helper validates the request version before decoding execution fields or initializing the sandbox. Keep `HELPER_PROTOCOL_VERSION` in `agent_core/execution/process.py` and `tests/smoke_wheel.py`, and `PROTOCOL_VERSION` in `nosis-sandbox/src/main.rs` aligned, and increment them together when changing the contract. After the request, stdin is a control channel: byte `0` or EOF cancels the command; byte `1` signals a timeout and preserves captured output in the result. Keep it open until execution finishes. Version 1 helpers are rejected before any command is sent.

On Windows the shell tool uses PowerShell 7, so PowerShell 7 has to be installed separately.

## Contributors

- [AKArrok](https://github.com/AKArrok): reported and verified the append-only journal tail-append issue ([#2](https://github.com/EvannZhongg/Nosis/pull/2)).
- [George Pickett](https://github.com/georgeatparallel): contributed the Parallel Search MCP integration ([#4](https://github.com/EvannZhongg/Nosis/pull/4)).
- [Pocket99](https://github.com/Pocket99): contributed the initial GUI, with sessions, workspace browsing, and model switching ([#1](https://github.com/EvannZhongg/Nosis/pull/1)).
- [Guillermo Prieto Rodriguez](https://github.com/guillermop2002): reported the file-tool path race that let a sandboxed shell redirect workspace reads and writes outside the workspace.

## License

[MIT](LICENSE)
