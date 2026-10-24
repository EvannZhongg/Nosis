# Public Bridge protocol, major 1

This is the local client API for third-party frontends. Install `nosis-agent`,
start **`nosis-bridge`** as a child process, and exchange UTF-8 newline-delimited
JSON on stdin/stdout. The executable is installed beside the Python environment's
other console scripts; use its absolute path when that directory is not on PATH.
No Node, browser, HTTP server, GUI, or repository working directory is required.
The executable uses the same Runtime as Nosis's first-party frontends.

Each nonblank line is one JSON object with a string `type`. Lines end in `\n`
(`\r\n` is accepted). Chunk boundaries are arbitrary, including inside UTF-8
characters. Flush each command; stdout is flushed per event. Stdout contains
protocol messages only; stderr contains diagnostics and must be drained separately.
There is no fixed small line limit: checkpoints can contain the full transcript.

## Stability and scope

Every `session_ready` advertises `protocol_version: 1`. Clients must check this
before using the session. A breaking change to documented public fields or
semantics increments this major and is recorded in this document. Additive fields
and event kinds may appear within a major; ignore unknown fields/events. This is
version identification, without version negotiation or a compatibility layer.
The initial public release introduces major 1.

The normative field definitions are `Incoming`, `Outgoing` and their referenced
types in [`@nosis/protocol`](../interfaces/protocol/src/protocol.ts). Directions
are from the client's perspective: Outgoing commands, Incoming events. Optional
fields can be absent; explicit `null` is distinct from absence. IDs are opaque,
case-sensitive strings. Allocate a new nonempty `turn_id` for each submission and
a new `steer_id` per steering input; IDs are not retry/idempotency keys.

The following are **internal**, with no public stability promise:

- `@nosis/protocol/internal`, all `settings_*` messages, their configuration
  editor schemas, Python classes/module layout and session journal storage format.
- GUI Host `open_session` fields `attach_only`, `after_event`, `attachment_id`,
  `takeover`, and GUI events `attachment_replaced` / `replayed`.
- `resume_after` delivery metadata, GUI HTTP/WebSocket routes, attachment
  ownership and host event caches.
- Concrete Tool argument/output schemas, Job `details`, error `details` keys,
  provider-specific errors and diagnostic text. Their envelopes remain public.

These extra fields may be visible on the wire; their presence does not make them
public. Stdio clients own a single Bridge process and do not attach to a GUI-owned
Bridge. Stdio has no `after_event` replay command or reconnect-to-process API.

## Session initialization and Runtime synchronization

Send the opening command first:

```json
{"type":"open_session","workspace":"/absolute/workspace","session_id":null}
```

`session_id: null` creates a session; an existing ID resumes a stored session.
Use an existing workspace directory accessible to the Bridge. Prefer absolute
paths; relative paths resolve in the Bridge's cwd. A resumed session's stored
workspace takes precedence. Optional `provider` selects a configured provider;
otherwise the stored provider or configured default is used. `provider` is a
configuration ID, not an arbitrary model name or credential.

Successful open emits, in order:

1. `session_ready`: protocol major, actual `session_id`, `workspace`, `provider`,
   `model`, `resumed`, `message_count`, and `permission_preset`. Its `transcript`
   contains the restored conversation.
2. `runtime_state`: the initial authoritative snapshot, initially `inactive`.

`session_ready` confirms the control plane only. Apply the first standalone
`runtime_state` before dispatching follow-up commands. Provider/Tool/MCP/Agent
construction is lazy and happens on the first turn or an explicit refresh. First-use startup installs
missing default configuration, prompts, Skills and Plugins under `~/.nosis`
through agent_runtime; it preserves existing files. A session can open without
provider credentials. Configure credentials in the installed provider config or
its referenced environment variables before a model turn; this API does not
prompt interactively or invent credentials.

If another Runtime owns the session writer, `session_open_failed` contains
`session_id` and `error: {type: "SessionInUseError", message}`. No ready/sync
pair follows that failure. The process stays connected; `list_sessions` or
another `open_session` is allowed. Other opening errors are fatal. A fresh
`open_session` on an idle Bridge rebinds it and produces a new ready/sync pair;
the process's sequence continues. Do not pipeline a rebind with a turn.

## Explicit Runtime refresh

After opening and synchronizing an idle session, send:

```json
{"type":"runtime_refresh"}
```

This forces a new execution plane even when configuration is unchanged. It
reloads configuration, prompts, Skills and Plugin Agents, reconnects MCP and
discovers tools again. The same Session, journal, permission preset and plan
remain bound. No model request or conversation turn is created.

Clients must disable refresh during an active turn; ordinary commands are
processed serially by the Bridge. Runtime also enforces the idle requirement.
Wait for `runtime_refreshed` (with `provider` and `model`) or
`runtime_refresh_failed` (with a structured `error`). These events carry the
current `runtime` snapshot. Assembly publishes `starting`, then `idle` on
success or `failed` on failure. A failed refresh keeps the Bridge alive so the
client can correct configuration and refresh again. It does not restore resources
already closed. Individual unavailable MCP servers are reported through
`mcp_server_status` and do not necessarily fail the entire assembly.

Normal turns still check configuration changes automatically before executing.

## Runtime state

| phase | Meaning |
| --- | --- |
| `inactive` | Session open; execution plane not yet started or was invalidated. |
| `starting` | Runtime assembly in progress. |
| `idle` | No active turn. |
| `running` | Executing a turn. |
| `waiting_approval` | Waiting for permission response. |
| `waiting_user` | Waiting for structured user response. |
| `failed` | Execution plane startup failed, or a fatal process error occurred. |

The snapshot also includes `turn_id`, `provider`, `permission_preset`,
`context_window`, `cache_usage`, `jobs`, `approval`, `question`, `plan`, optional
`workspace` and `runtime_warnings`. A standalone `runtime_state` or embedded
`runtime` replaces the prior snapshot. Embedded snapshots are complete, not
patches. Render state from these snapshots; do not infer it from model text,
Tool messages, commands sent or terminal events. `idle` alone does not complete a
turn: it is emitted before the terminal event. The process may have queued input.

## Commands and responses

| Command | Payload / response |
| --- | --- |
| `open_session` | Above; `session_ready` then `runtime_state`, or opening failure. |
| `user_turn` | `turn_id`, `text`, optional `attachments`; receipt and execution events below. |
| `user_steer` | Target `turn_id`, `steer_id`, `text`; received/rejected then applied if consumed. |
| `cancel` | Target `turn_id`; matching queued/active turn is cancelled cooperatively. No immediate acknowledgment. |
| `approval_response` | Outstanding `request_id`, boolean `approved`. |
| `user_question_response` | Outstanding `request_id`, `option_id` or free-form `text`. |
| `permission_set` | `preset`; `permission_changed` with complete `runtime`. Can run during a turn. |
| `provider_set` | Configured `provider`; `provider_changed` with `provider`, `model`, `runtime`. |
| `workspace_set` | `workspace`; `workspace_changed` with actual path and `runtime`. |
| `list_sessions` | `sessions_listed.sessions`: current workspace session summaries. |
| `load_session` | `session_items.items`: currently bound conversation. |
| `shutdown` | Routes cancellation and closes Runtime; process exit is completion. |

Permission presets are `ask_for_approval`, `workspace_access`, `full_access`.
Responses confirm the Runtime's actual changes. Changing provider/workspace may
also emit `runtime_state: inactive`. Send these changes while idle. Commands other
than steer/cancel/interactions/permission/shutdown are serialized behind an active
turn. There is no generic RPC, universal request ID, or concurrent request/reply
multiplexer. Serialize listing/history requests if matching responses matters.
Use the opening checkpoint to display initial history; `load_session` is an
explicit refresh, not a stream replay or cursor acknowledgment.
The session listing contains stored conversations; a newly opened empty Session
can be absent from that list.

Automatic naming may emit `session_title_updated` after the first completed
assistant/Tool unit, independently of turn completion. It carries `session_id`,
`title` and `title_source` (`initial`, `generated`, or `manual`). Apply it to the
matching list row without changing turn state; subsequent listings return the
persisted title.

Attachments are local descriptors `{type: "image" | "file", path, filename,
mime_type, size_bytes}`. Paths reference files readable by the Runtime under its
existing workspace/permission rules; there is no binary upload transport. Runtime
validates actual files and normalizes media metadata. Tool media events reference
local images by the same convention.

## Turn and interaction lifecycle

```text
client → user_turn(turn_id)
       ← user_turn_received(turn_id)
       ← runtime_state(starting/running), context/model/tool/... events
       ← [runtime_state(waiting_approval), approval_request(request_id)]
client → [approval_response(request_id, approved)]
       ← [runtime_state(running)]
       ← runtime_state(idle or failed)
       ← turn_completed | turn_cancelled | turn_failed  + transcript
```

`user_turn_received` acknowledges enqueueing, not model startup or persistence.
Clients should submit one turn at a time and use `user_steer` for new input during
execution. The transport does not convert `user_turn` into steering. Steering
receipt is `user_steer_received` or `user_steer_rejected`, identified by both IDs;
only `user_steer_applied` means Core consumed it. Accepted steering can remain
unapplied when the turn ends. Stale cancellation/interaction IDs have no effect.
Do not resubmit after an ambiguous disconnect as though IDs made it idempotent.

Approval requests include `request_id`, `turn_id`, human-readable `command`, and
optional `kind` (`shell`, `file`, `computer`, `mcp`), `server` and `tool_name` (some optional
values may be `null`). Display the request and submit the user's boolean decision.
The same request is in `runtime.approval`; avoid rendering it twice. Questions
include `question`, `options: [{id, label, description?, recommended?}]` and
`allow_free_text`. Submit a listed `option_id`, or nonblank `text` only when
allowed. An invalid answer leaves the question outstanding; there is no validation
error response. Interactions are serialized by Bridge. Cancellation and shutdown
unblock them through existing Runtime cancellation.

Workspace file writes (`apply_patch`, `edit_file`, `write_file`) request approval
under `ask_for_approval`; `workspace_access` and `full_access` allow them without
a prompt. File tools stay confined to the current workspace under every preset.
Tool errors can include optional structured `details`. An `apply_patch` commit
failure reports `completed`, `failed`, `pending`, and `cause` there; completed
operations remain applied. The same details are retained in Session tool results.

`cancel` addresses exactly the supplied queued/active turn. It can race completion;
a completed terminal outcome remains valid. It has no separate response. On an
ordinary continuing connection each processed turn produces one terminal outcome:
`turn_completed` with last-call `usage`, `turn_cancelled`, or `turn_failed` with a
structured `error`. During shutdown/EOF or process failure a terminal event is not
guaranteed; observe process exit and resume stored history if needed.

## Events, order and transcript checkpoints

All Bridge events have a positive `event_sequence`, strictly increasing in stdout
emission order for the lifetime of that process. It starts at 1 in a new process;
it is not a journal offset or globally unique Session cursor. Do not reuse it after
restarting Bridge. Nested Runtime snapshots need not have a sequence of their own.

| Event family | Meaning / correlation |
| --- | --- |
| `assistant_delta`, `reasoning_delta` | Streamed `text`, keyed by `turn_id` and `model_call_index`. |
| `assistant_message` | Complete assistant `content`, timestamp and model-call index; replaces that call's displayed deltas. |
| `model_completed` | Per-call `usage` and Session cumulative `cache_usage`. |
| `compression_completed` | Session cumulative cache usage after a compression attempt. |
| `context_window` | Current token limits and compression metrics. |
| `tool_batch_started` | Tool declarations for a model call, with IDs/names/arguments. |
| `tool_call`, `tool_result` | Call execution/status keyed by Tool call ID; indices are 1-based. Result has `ok` and nullable structured `error`. |
| `tool_media` | Image attachment descriptors. |
| `plan_updated` | Full plan snapshot and revision. |
| `job_status` | Background job ID, kind, status, opaque details. |
| `hook_completed` | Hook invocation ID, Plugin-qualified hook ID, checkpoint, Session ID, outcome, decision, reason, timing and optional Tool call ID. See [Plugin Hooks](plugin-hooks.md). |
| `mcp_server_status` | Startup/status diagnostics for a named server. |
| `user_steer_*`, interaction requests, terminal events | Lifecycle described above. |

Model deltas precede the complete message for that call; Tool calls can execute
concurrently and their results can interleave. Preserve actual wire order and
correlate IDs, without imposing a fixed order between unrelated event families.
`model_completed` can precede Tool execution; it does not end the turn.
`cache_usage` is a complete Session total; replace it instead of summing repeated
snapshots. Unknown token counts are `null`. `turn_completed.usage` is the last
model call, not the sum of the turn.

`transcript: {items: SessionItem[], event_sequence: N}` is an authoritative
checkpoint replacing the entire conversation through event N. It is attached to
`session_ready`, the first context-window event after accepting turn input (when
one is emitted), and terminal events. Apply the event's non-transcript state, then
replace transcript with the checkpoint; do not append that event's conversation
content again. Apply subsequent conversation events only after N. Session items
contain normalized user/assistant/tool content and Artifact references; they do
not expose the append-only journal format or provider payloads. Tool-result live
events deliberately omit output; read normalized content from checkpoints.

The GUI Host separately caches Bridge events and supplies `after_event` replay,
`replayed`, `resume_after` and attachment ownership. Those features do not change
the public event order/checkpoint rules and are not available over this stdio API.

## Errors and close

`turn_failed` and `fatal` contain `error: {type, message, details}`. Error class
names and detail keys are diagnostic; branch on the event kind. `turn_failed`
ends a turn and permits further valid commands; a startup failure has phase
`failed`, while an execution failure can leave phase `idle`. `fatal` makes the
connection unusable and the CLI exits with code 1. It may occur before any session
opens or before a protocol major is available. Malformed JSON/envelopes, unknown
command kinds and out-of-order Session commands produce fatal `ProtocolError`;
invalid command arguments can produce other fatal error types. Interaction
responses, steer and cancel require an opened Session; sending them before opening
has no error-response guarantee. Only the documented commands are supported.

`session_open_failed` is the recoverable writer-conflict exception described
above. SDK process errors (missing executable, broken pipe), framing errors,
stderr and abnormal exit are transport diagnostics, not synthetic Agent events.
An abrupt exit may occur without `fatal`; do not assume terminal events will arrive.

To close, send `shutdown`, close stdin and continue draining stdout/stderr until
process close. EOF also requests shutdown and cancels active work; do not close
stdin immediately after submitting a turn you intend to finish. There is no
`shutdown_complete` event. The SDK allows 2 seconds of cooperative cleanup before
terminating the process group/tree; `await client.shutdown()` resolves on drained
process close. Any events still emitted during cleanup are delivered normally.
Forced termination cannot guarantee completed persistence.

## Minimal client sequence

```text
spawn nosis-bridge (pipes)
send open_session
receive session_ready; check protocol_version == 1; apply checkpoint
receive runtime_state; apply snapshot
send user_turn with a fresh turn_id
receive/render events; answer interactions by request_id
receive terminal event; apply checkpoint
send shutdown; close stdin; drain output; await process close
```

See the [TypeScript client](../interfaces/client/README.md) and
[implementation review](bridge-api-design.md).
