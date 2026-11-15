# Bridge public API: surfaces and decisions

Which Bridge messages third-party clients may depend on, who owns each of them,
and the rules that keep the public surface stable. Transport details, command
semantics and event ordering are documented in the
[protocol reference](bridge-protocol.md).

## Call chain

`TUI / GUI / IM → resident Session Host → BridgeServer → RuntimeHost → agent_core`.
A standalone TUI can own the Bridge when the resident host is absent. BridgeServer
allocates one worker per Session. Interactive and scheduled turns use its Runtime
queue, and the Bridge projects Runtime callbacks onto attached channels.

| Surface | Current owner | Lifecycle / decision |
| --- | --- | --- |
| `bridge_ready`, `channel_command`, `channel_event`, `channel_closed`, `close_channel`, `shutdown` | BridgeServer | Process startup initializes configuration and warms the default provider, then advertises the protocol major. Every session command and event names its channel; a channel ID is reused only after `channel_closed`. `shutdown` or stdin EOF cancels all channels and exits. Public. |
| `open_session`, `session_ready`, `session_open_failed` | Channel Bridge + SessionRuntimeController | Open a lightweight control plane, acquire the journal writer, restore workspace/provider/permissions, publish ready then the initial runtime snapshot. Writer conflicts come back as recoverable `SessionInUseError`. Public. |
| `session_snapshot` | Channel Bridge | Re-publish an already open channel's ready/snapshot pair without rebinding the Session, rebuilding the Agent or disconnecting MCP. Public. |
| `runtime_state`, embedded `runtime` | Channel Bridge projection of Runtime callbacks | Authoritative state; the initial standalone snapshot is the synchronization barrier. Public. |
| `user_turn`, `user_turn_received`, `scheduled_turn_started`, model/tool/context/plan/job events, terminal events | Channel Bridge + TurnRunner/Core | Each Session queue serializes accepted interactive and scheduled turns in arrival order. Public. |
| `session_channel` | Managed Bridge and Session Host | Register a Session opened by Scheduler. Internal. |
| `idle_only`, `channel_busy` | BridgeServer and resident Session Host | Atomically reject an idle-only release while work is pending. Public. |
| `user_steer`, `cancel`, interaction responses, `permission_set` | Channel Bridge routing → existing Runtime controls | Routed while a turn runs; IDs address turns or outstanding interactions and are unique only within their channel. Public. |
| `provider_set`, `workspace_set`, session listing/history | Channel serve loop → Runtime | Serialized with turns. Public. |
| `settings_*` | Channel Bridge → Runtime SettingsStore | First-party configuration editor schema. Internal; not a stable configuration API. |
| event sequence and transcript checkpoint | Channel Bridge EventStream | Per-channel sequence, authoritative transcript replacement. Public. |
| `resume_after` | Channel Bridge EventStream → GUI ActiveSession | Host delivery cursor; not a stdio reconnect API. Internal metadata. |
| `attach_only`, `after_event`, `attachment_id`, `takeover` | GUI WebSocket route + ActiveSessionRegistry/ActiveSession | Host attachment ownership and cached event replay. Removed before forwarding `open_session` to the channel. Internal. |
| `attachment_replaced`, `replayed` | GUI host | Browser ownership notification / replay annotation. Never emitted by the Bridge process. Internal. |

The transport thread decodes each newline-delimited packet and routes
interactions, permission changes, steer and cancel immediately. Turns and the
remaining commands queue behind the Session's serve loop. Sessions execute
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
   compatibility implementation.
2. `@nosis/protocol` exports the stable message/data surface and decoder;
   `@nosis/protocol/internal` holds first-party settings and GUI extensions.
   Extra on-wire metadata is allowed but does not acquire public stability.
3. `@nosis/client` owns framing, ordered delivery, diagnostics, process failures
   and bounded shutdown, and addresses every command by channel ID. It does not
   cache Runtime/session/approval state or queue commands until readiness; callers
   apply the documented synchronization barrier per channel.
4. Install `nosis-bridge` pointing at the existing entry function. The entry
   claims stdout and selects the owner for `--config-directory`. It relays to the
   resident host when present. A standalone owner holds the execution lock,
   initializes defaults and warms provider code, tokenizer and TLS trust.
5. Publish ESM JavaScript and declarations for both TS packages so an external
   Node client does not need a TS loader or repository paths.
6. Process-wide resources stay in `agent_runtime.RuntimeServices` (configuration,
   environment reload, scheduler, warmed provider code, tokenizer vocabularies,
   TLS trust context); a channel owns only its Runtime, journal writer, event
   sequence and interaction waiters.

## Boundaries

- The resident host's Session registry arbitrates control attachment across GUI,
  IM and local TUI. The local transport uses a named pipe or Unix domain socket,
  including when the HTTP adapter is disabled.
- `BridgeRequest` / `BridgeEvent` are the process envelope; `Incoming` /
  `Outgoing` are the nested channel payloads. Both are defined in
  [`@nosis/protocol`](../interfaces/protocol/src/protocol.ts).
