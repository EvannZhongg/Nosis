# Bridge Public Client API: review and design

## Existing implementation

The call chain is `TUI BridgeClient / GUI BridgeProcess → interfaces.bridge.BridgeServer
→ interfaces.bridge.Bridge (one per channel) → agent_runtime.RuntimeHost → agent_core`.
`BridgeServer` owns the stdio transport and multiplexing, warms the process-wide
`RuntimeServices` once, and starts one worker thread per channel. Each `Bridge`
routes that channel's commands and projects its Runtime callbacks. `RuntimeHost`
alone assembles execution planes; no new execution path is needed.

| Surface | Current owner | Lifecycle / decision |
| --- | --- | --- |
| `bridge_ready`, `channel_command`, `channel_event`, `channel_closed`, `close_channel`, `shutdown` | BridgeServer | Process startup initializes configuration and warms the default provider, then advertises the protocol major. Every session command and event names its channel; a channel ID is reused only after `channel_closed`. `shutdown` or stdin EOF cancels all channels and exits. Public. |
| `open_session`, `session_ready`, `session_open_failed` | Channel Bridge + SessionRuntimeController | Open a lightweight control plane, acquire the journal writer, restore workspace/provider/permissions, publish ready then the initial runtime snapshot. Writer conflicts come back as recoverable `SessionInUseError`. Public. |
| `session_snapshot` | Channel Bridge | Re-publish an already open channel's ready/snapshot pair without rebinding the Session, rebuilding the Agent or disconnecting MCP. Public. |
| `runtime_state`, embedded `runtime` | Channel Bridge projection of Runtime callbacks | Authoritative state; the initial standalone snapshot is the synchronization barrier. Public. |
| `user_turn`, `user_turn_received`, model/tool/context/plan/job events, terminal events | Channel Bridge + TurnRunner/Core | The transport acknowledges and enqueues; the channel thread runs turns sequentially. Public; execution stays in the Runtime/Core. |
| `user_steer`, `cancel`, interaction responses, `permission_set` | Channel Bridge routing → existing Runtime controls | Routed while a turn runs; IDs address turns or outstanding interactions and are unique only within their channel. Public. |
| `provider_set`, `workspace_set`, session listing/history | Channel serve loop → Runtime | Serialized with turns. Public. |
| `settings_*` | Channel Bridge → Runtime SettingsStore | First-party configuration editor schema. Internal; not a stable configuration API. |
| event sequence and transcript checkpoint | Channel Bridge EventStream | Per-channel sequence, authoritative transcript replacement. Public. |
| `resume_after` | Channel Bridge EventStream → GUI ActiveSession | Host delivery cursor; not a stdio reconnect API. Internal metadata. |
| `attach_only`, `after_event`, `attachment_id`, `takeover` | GUI WebSocket route + ActiveSessionRegistry/ActiveSession | Host attachment ownership and cached event replay. Removed before forwarding `open_session` to the channel. Internal. |
| `attachment_replaced`, `replayed` | GUI host | Browser ownership notification / replay annotation. Never emitted by the Bridge process. Internal. |

The transport thread decodes each newline-delimited packet and routes
interactions, permission changes, steer and cancel immediately; turns and the
remaining commands queue behind the channel's serve loop. Channels execute
concurrently, so one channel waiting on HTTP, MCP startup or user input does not
block another. `user_turn_received` is receipt, not successful startup, and
`idle` may precede the terminal event; clients must not reconstruct completion
from phase alone.

A channel `fatal` ends only that Runtime and closes its journal writer while peer
channels continue. A process-level `fatal` — malformed envelope, unknown
transport message, failed startup — ends the process with exit code 1. Closing
stdin routes the same Runtime shutdown as an explicit `shutdown`. There is no
shutdown acknowledgment, and transport exit can happen without a terminal event.

## Contract decisions

1. Publish a stdio protocol major, advertised on `bridge_ready` and on every
   `session_ready`. Additive fields/events can be ignored; breaking public changes
   increment the major and are documented. There is no negotiation or
   compatibility implementation. Major 3 replaced the single-session stream with
   channel envelopes and dropped earlier wire formats.
2. `@nosis/protocol` exports the stable message/data surface and decoder;
   `@nosis/protocol/internal` holds first-party settings and GUI extensions.
   Extra on-wire metadata is allowed but does not acquire public stability.
3. `@nosis/client` owns framing, ordered delivery, diagnostics, process failures
   and bounded shutdown, and addresses every command by channel ID. It does not
   cache Runtime/session/approval state or queue commands until readiness; callers
   apply the documented synchronization barrier per channel.
4. Install `nosis-bridge` pointing at the existing entry function. The entry
   claims stdout, initializes defaults for `--config-directory` (or the default
   directory), warms provider code, tokenizer and TLS trust, then serves channels.
   Hosts keep one resident process and open a channel per Runtime.
5. Publish ESM JavaScript and declarations for both TS packages so an external
   Node client does not need a TS loader or repository paths. Nothing is published
   to a registry as part of this change.
6. Process-wide resources stay in `agent_runtime.RuntimeServices` (configuration,
   environment reload, scheduler, warmed provider code, tokenizer vocabularies,
   TLS trust context); a channel owns only its Runtime, journal writer, event
   sequence and interaction waiters.

## Remaining gaps

- `BridgeRequest` / `BridgeEvent` describe the process envelope, but
  `@nosis/client` writes it from object literals instead of the shared type.
- Both TS packages and the Python wheel are built locally for integration tests;
  nothing is published to a registry.
- A channel belongs to the host process that opened it. Another host cannot attach
  to a running Runtime; `attach_only` / `takeover` only reattach browser pages
  inside one GUI server.

## Delivered stages

1. **Public protocol**: protocol reference, public/internal TS exports, protocol
   major, installed CLI and first-use configuration initialization; envelopes are
   validated at the transport boundary without changing execution rules.
2. **SDK**: `@nosis/client` extracted from the TUI transport, package builds and
   examples, caller-owned Session synchronization and interaction decisions.
3. **External integration tests**: install/build artifacts, launch from a foreign
   cwd with an isolated home, exercise real stdio and the packaged SDK, and use a
   local provider fixture for turn/interaction/cancel flows without paid model
   calls.
4. **Resident channels**: `BridgeServer` multiplexing, `RuntimeServices`, channel
   addressing in the SDK, GUI/TUI adoption, `session_snapshot`, provider warmup and
   the shared TLS context.
