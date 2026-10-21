# Plugin command hooks

Hooks are an additional Plugin component. Runtime discovers and assembles their
declarations; Core owns checkpoints, decisions, command execution and Journal facts.
Bridge only translates structured execution events. GUI, TUI, scheduled turns and
sub-agents use the same mechanisms.

## Manifest

```json
{
  "name": "project-rules",
  "capabilities": ["hooks"],
  "components": {"hooks": ["hooks/hooks.json"]}
}
```

Paths follow the existing component rules: relative to the Plugin root, explicitly
declared and contained within that root. `capabilities` describes the package and
does not grant execution authority. Disabled Plugins contribute no hooks.

## Hook declaration, schema version 1

```json
{
  "schema_version": 1,
  "hooks": {
    "PreToolUse": [{
      "name": "check-writes",
      "tools": ["write_file", "edit_file", "apply_patch"],
      "command": "python",
      "args": ["-B", "${PLUGIN_ROOT}/hooks/check.py"],
      "scope": "workspace",
      "timeout_seconds": 10
    }]
  }
}
```

Supported events are `InputSubmitted`, `PreToolUse`, `PostToolUse` and `Stop`.
Names are unique within a Plugin and register as `<plugin>:<name>`.
`command` is a non-empty executable name or path. `args` is a string array;
arguments are passed directly, without Shell interpolation. Nosis expands the
fixed `${PLUGIN_ROOT}` placeholder in the executable and arguments.
The working directory is the bound Workspace.

`tools` is an optional unique array of exact model-facing names, including qualified
MCP names. Omitted matches every tool; an empty array matches none. It is valid only
for tool events. The Core does not interpret Plugin-specific rule conditions.

`scope` is `workspace` by default or `host`. `timeout_seconds` defaults to 10 and
must be an integer from 1 to 86400. Unknown fields and unsupported schema versions
are errors. Manifest errors skip the Plugin; invalid Hook components are skipped
with warnings while other components remain available. Duplicate names keep the
first declaration and report both sources.

Matching hooks run in discovery, component-file and declaration order. All matching
checks run; denials take precedence over passing decisions and feedback is combined.
Duplicate names across component files keep the first declaration and report both
sources; duplicate names inside one file are invalid. Different concurrent Tool calls
retain independent Hook invocations.

## Invocation

Each command receives one UTF-8 JSON object on its own stdin, ending at EOF:

```json
{
  "schema_version": 1,
  "event": "PreToolUse",
  "hook_id": "project-rules:check-writes",
  "session_id": "session-id",
  "turn_id": "turn-id",
  "workspace": "/workspace",
  "agent": {"role": "main", "parent_session_id": null},
  "tool_call": {
    "id": "call-id",
    "name": "write_file",
    "arguments": {"path": "sample.txt", "content": "hello"}
  }
}
```

Event-specific data:

| Event | Fields and checkpoint |
| --- | --- |
| `InputSubmitted` | `input.text` and `input.source`: `user`, `steering`, `scheduled` or `delegation`; before accepting the input into model context |
| `PreToolUse` | `tool_call`; after Tool lookup and JSON-object decoding, before routing, approval and execution; each Tool still validates its own argument schema |
| `PostToolUse` | `tool_call`, `tool_result.ok` and `tool_result.content`; after the actual Tool invocation and normalized result commit, including Tool execution errors |
| `Stop` | `response`, `correction_attempt`, `executions.tools`, `executions.jobs`; after pending Jobs/results and steering, before atomically closing Turn control |

`tool_result.content` and each execution's `result` contain the existing normalized
JSON reading view. Large outputs are represented by Session Artifact references;
Hooks do not receive unbounded raw results or binary attachments. Tool entries carry
`id`, `name`, `arguments`, `status`, `result`. Job entries carry `id`, `kind`, `status`,
`details`, `result_summary`. The summary includes only the current Turn.

Invalid calls, pre-hook denials and approval denials do not trigger `PostToolUse`.
A background Tool submission does trigger it, but its returned handle is not the
background operation's final result. Final Job facts are available at `Stop`.
Question/approval answers and synthetic feedback do not trigger `InputSubmitted`.
Child Agents use their own Session and role metadata while inheriting the same Hook
definitions and authority. Their events travel on the owning Bridge turn stream.

## Command response

Exit successfully and write a single JSON object to stdout. Send diagnostics to
stderr. Allowed fields are `decision` (default `pass`), optional non-empty `reason`
and optional non-empty `feedback`.

| Event | Decisions |
| --- | --- |
| Input and pre-tool | `pass`; `deny` requires `reason` |
| Post-tool | `pass`, optionally with `feedback`; already committed side effects cannot be revoked |
| Stop | `pass`; `continue` requires `feedback` |

`{}` is a valid passing response. `feedback` enters model context with origin
`hook_feedback`; it is not a human input, user anchor or another Hook trigger.
Tool Hook feedback is appended only after the complete Tool Batch's result/media
sequence. Provider context projection and compression preserve that sequence.

An input denial fails the active Turn and records the original rejected input in
the Journal without accepting it into future model context. A pre-tool denial
returns a `HookDenied` ToolResult paired with the original call ID, so the model
can correct its action. At Stop, one correction round is allowed per Turn; another
`continue` fails with `HookRejectedError`. Cancellation and failures never pass
through Stop. Incoming steering remains accepted while Stop checks execute.
The candidate answer may already have streamed before the completion check.

Nonzero command exits, timeouts, invalid JSON, invalid decisions and responses over
16000 characters raise `HookExecutionError`; they never silently permit the action.
Existing Tool facts and side effects remain committed when a post-hook fails.
Diagnostics use the shared Tool output budget and Session Artifact machinery.

## Authority and lifecycle

Hook commands use an internal, model-invisible ToolSet and the same ExecutionRouter,
PermissionController, executors, cancellation and process-tree cleanup as ordinary
commands. Their approval is independent from the original Tool's approval. This
internal invocation cannot recursively trigger command hooks. Scope cannot exceed
the Runtime authority limit; unattended workers cannot obtain interactive approval.

Workspace execution makes the Plugin directory readable inside the existing Linux
and macOS sandbox. Executables and dependencies must also be accessible within that
sandbox. Windows uses the existing write-restricted token and inherited host read
view, with the same limitations as ordinary workspace commands. Host scope retains
the ordinary host command environment and authority semantics.

ExecutionPlane owns immutable Hook definitions; Session/Turn state, rejection counts
and feedback are not retained in the shared runner. `hook_started` and `hook_finished`
Journal records retain invocation identity, source, outcome, timing and diagnostic
references. Bridge publishes `hook_completed` with structured identity and outcome.
Recovering a Session replays facts and feedback, not Hook side effects.

Use `/refresh` while idle to reload Hook declarations, installation or enablement.
There is no new file watcher or global configuration switch. A Plugin may independently
read its own project-rule files at each invocation, as the native `tests/nosis-plugin-testkit`
fixture does.

## Validation

`plugin-creator/scripts/validate_plugin.py` validates declarations without importing
Nosis or starting commands. Contract tests compare its acceptance with Runtime.
`tests/test_hooks.py` covers Core decisions, sequence integrity, cancellation,
approval, diagnostics and Windows stdin. `tests/test_hook_interop.py` covers Plugin
assembly, Skill/Agent composition, refresh, Session reuse and scheduled execution.
