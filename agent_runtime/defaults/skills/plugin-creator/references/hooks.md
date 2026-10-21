# Command hooks

Declare files under `components.hooks`, for example `["hooks/hooks.json"]`.

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

Names are unique within the Plugin and register as `<plugin>:<name>`. `command` is an
executable; `args` are passed directly, without Shell expansion. Nosis replaces
`${PLUGIN_ROOT}`; cwd is the Workspace. `tools` matches exact names, including MCP names:
omitted matches all, `[]` matches none; valid only for tool events.
`scope` defaults to `workspace` and also accepts `host`; timeout defaults to 10 seconds
and accepts integers 1..86400. Scope never bypasses Runtime authority or approval.

## Input and decisions

Commands receive one UTF-8 JSON object on stdin. Common fields are `schema_version`,
`event`, `hook_id`, `session_id`, `turn_id`, `workspace`, and
`agent` (`role`, `parent_session_id`). Write one JSON object to stdout; diagnostics go to stderr.

| Event | Additional input | Decisions |
| --- | --- | --- |
| `InputSubmitted` | `input.text`, `input.source`: user, steering, scheduled or delegation | `pass`, `deny` |
| `PreToolUse` | `tool_call`: id, name, arguments | `pass`, `deny` |
| `PostToolUse` | `tool_call`, `tool_result.ok`, `tool_result.content` | `pass` with optional feedback |
| `Stop` | `response`, `correction_attempt`, `executions.tools`, `executions.jobs` | `pass`, `continue` |

Response fields: `decision` (defaults to `pass`), optional non-empty `reason` and
`feedback`. `deny` requires `reason`; `continue` requires `feedback`. `{}` passes.
For example, `hooks/check.py` can begin with:

```python
import json
import sys

sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")
event = json.load(sys.stdin)
print(json.dumps({"decision": "pass"}))
```

Replace the passing response with the intended checks. Input denial fails the Turn;
pre-tool denial returns a paired `HookDenied` result. Post-tool checks run only for
invoked tools after results commit. Stop permits one correction round; another
`continue` fails. Cancellation never waits for Stop approval.

Feedback is synthetic model context, not human input. Tool results are normalized JSON
strings and may reference Artifacts. Stop summaries contain current-turn Tool/Job facts;
a background handle or transcript mention does not prove successful execution.
Hooks also apply to sub-agents; use `agent.role` when a check should target only the main Agent.

Nonzero exit, timeout, invalid JSON/decisions or stdout over 16000 characters fails the
Hook. Do not hide errors by always returning success. Validate declarations with
`scripts/validate_plugin.py`, then exercise real behavior in Nosis. The executable and
dependencies must be accessible inside the declared scope. Use `/refresh` for declaration changes.
