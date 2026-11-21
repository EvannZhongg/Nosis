# Documentation

Index of Nosis documentation. Install and quick-start instructions live in the
[root README](../README.md).

## Public contracts

| Document | Scope |
| --- | --- |
| [`docs/bridge-protocol`](bridge-protocol.md) | Stdio transport, commands, events, runtime phases, transcript checkpoints, and error rules for third-party clients |
| [`docs/bridge-api-design`](bridge-api-design.md) | Which messages are public or internal, who owns them, and the versioning decisions |
| [`docs/plugin-hooks`](plugin-hooks.md) | Command hook declarations, checkpoints, invocation payloads, and decisions |
| [`docs/plugin-interop`](plugin-interop.md) | Assembly, static validation, error isolation, and re-assembly of plugin components |

## Runtime layers

| Document | Scope |
| --- | --- |
| [`agent_core`](../agent_core/README.md) | Agent loop, context, session, tools, execution, permissions, memory, plans, jobs, scheduler, providers, skills, and MCP mechanisms |
| [`agent_runtime`](../agent_runtime/README.md) | Configuration, prompts, plugin discovery, runtime assembly, execution plane, session binding, and turn entry |

## Frontends and SDK

| Document | Scope |
| --- | --- |
| [`interfaces`](../interfaces/README.md) | Shared host modules, launch entries, Bridge process and channel attachment, and the allowed Core reuse boundary |
| [`interfaces/bridge`](../interfaces/bridge/README.md) | The standalone Bridge process, protocol routing, event translation, and frontend interaction forwarding |
| [`interfaces/protocol`](../interfaces/protocol/README.md) | The TypeScript protocol package: exports, build, and change checklist |
| [`interfaces/client`](../interfaces/client/README.md) | Public Node/TypeScript stdio client, process lifecycle and integration example |
| [`interfaces/tui`](../interfaces/tui/README.md) | The Ink terminal interface, commands, input state, and Bridge subprocess attachment |
| [`interfaces/gui`](../interfaces/gui/README.md) | The FastAPI service, React interface, multi-session connections, HTTP / WebSocket API, and media access |
| [`interfaces/mobile`](../interfaces/mobile/README.md) | Feishu transport, workspace and session selection, streaming text, questions, approvals, and shared-host setup |
