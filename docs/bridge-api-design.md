# Bridge Public Client API: review and design

## Existing implementation

The call chain is `TUI BridgeClient / GUI BridgeProcess → interfaces.bridge.Bridge
→ agent_runtime.RuntimeHost → agent_core`. RuntimeHost alone assembles execution
planes; the Bridge routes commands and projects callbacks. No new execution path
is needed.

| Surface | Current owner | Lifecycle / decision |
| --- | --- | --- |
| `open_session`, `session_ready`, `session_open_failed` | Bridge + SessionRuntimeController | Open lightweight control plane, acquire journal writer, restore workspace/provider/permissions. Publish ready then initial runtime snapshot. Public. |
| `runtime_state`, embedded `runtime` | Bridge projection of Runtime callbacks | Authoritative state; initial standalone snapshot is synchronization barrier. Public. |
| `user_turn`, `user_turn_received`, model/tool/context/plan/job events, terminal events | Bridge + TurnRunner/Core | Reader acknowledges/enqueues; serve thread runs sequentially. Public; execution remains in Runtime/Core. |
| `user_steer`, `cancel`, interaction responses, `permission_set` | Bridge input reader → existing Runtime controls | Routed during execution; IDs address turns or outstanding interactions. Public. |
| `provider_set`, `workspace_set`, session listing/history | Bridge serve thread → Runtime | Serialized with turns. Public. |
| `settings_*` | Bridge → Runtime SettingsStore | Existing first-party configuration editor schema. Internal; not a stable configuration API. |
| event sequence and transcript checkpoint | Bridge EventStream | Process-local sequence, authoritative transcript replacement. Public. |
| `resume_after` | Bridge EventStream → GUI ActiveSession | Host delivery cursor; not a stdio reconnect API. Internal metadata. |
| `attach_only`, `after_event`, `attachment_id`, `takeover` | GUI WebSocket route + ActiveSessions/ActiveSession | Host attachment ownership and cached event replay. Removed before forwarding open_session to Bridge. Internal. |
| `attachment_replaced`, `replayed` | GUI host | Browser ownership notification / replay annotation. Never emitted by the Bridge process. Internal. |

The reader handles interactions, permission changes, steer, cancel and shutdown
while the serve thread executes a turn. Other commands wait in its queue. Thus
`user_turn_received` is receipt, not successful startup, and `idle` may precede
the terminal event. Clients must not reconstruct completion from phase alone.
EOF routes the same Runtime shutdown as an explicit shutdown. There is no shutdown
acknowledgment. Transport exit can happen without a terminal event.

## Gaps

- The shared TS union mixes Bridge and GUI host messages and configuration schemas.
- No protocol major is advertised and no installed Bridge command exists.
- Independent Bridge startup misses the first-use configuration initialization
  performed by GUI/TUI launchers.
- TUI owns reusable child-process and NDJSON logic; its spawn/pipe failures,
  truncated output and shutdown completion are not exposed cleanly to callers.
- Existing tests mostly call Python objects or frontend mocks rather than an
  installed CLI and packaged SDK from outside the source checkout.

## Contract decisions

1. Publish stdio protocol major 1, advertised on every `session_ready`. Additive
   fields/events can be ignored; breaking public changes increment the major and
   are documented. There is no negotiation or compatibility implementation.
2. `@nosis/protocol` exports the stable message/data surface and decoder;
   `@nosis/protocol/internal` contains first-party settings and GUI extensions.
   Extra on-wire metadata is allowed but does not acquire public stability.
3. Extract `@nosis/client` from the TUI process client and reuse it in the TUI.
   It handles framing, ordered delivery, diagnostics, process failures and bounded
   shutdown. It does not cache Runtime/session/approval state or queue commands
   until readiness. Callers apply the documented synchronization barrier.
4. Install `nosis-bridge` pointing at the existing entry function. The entry asks
   agent_runtime to initialize defaults after claiming stdout, before constructing
   Bridge. Existing launchers and execution semantics remain unchanged.
5. Publish ESM JavaScript and declarations for both TS packages so an external
   Node client does not need a TS loader or repository paths. Nothing is published
   to a registry as part of this change.

## Implementation stages

1. **Public Protocol**: protocol reference, public/internal TS exports, protocol
   major, CLI and first-use initialization; validate protocol envelopes at the
   transport boundary without changing execution rules.
2. **SDK**: move/reuse TUI transport, package builds and examples; retain explicit
   caller-owned Session synchronization and interaction decisions.
3. **External integration tests**: install/build package artifacts, launch from a
   foreign cwd with an isolated home, exercise real stdio and packaged SDK, and
   use a local provider fixture for turn/interaction/cancel flows without paid
   model calls. Run existing Bridge and frontend regression suites.
