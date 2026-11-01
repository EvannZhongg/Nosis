# @nosis/protocol

Message types, connection-state helpers and a framing decoder shared by Nosis's
first-party frontends, packaged so an external Node client can consume the same
definitions instead of restating them.

The normative protocol reference is the
[Bridge Public Client API](../../docs/bridge-protocol.md). Transport framing, command
and event semantics, runtime phases, transcript checkpoints and error rules live
there and are not repeated here. The public/internal boundary is described in
[the design note](../../docs/bridge-api-design.md).

## Exports

| Entry | Contents | Stability |
| --- | --- | --- |
| `@nosis/protocol` | `Incoming` / `Outgoing`, `PROTOCOL_VERSION`, `BridgeRequest` / `BridgeEvent`, data types, state helpers and `MessageDecoder` | Public |
| `@nosis/protocol/internal` | Settings-editing types, GUI attachment and replay extensions used by first-party frontends | Internal, no stability promise |

The public data types cover attachments, tool calls, token usage, permission
presets, plans, context window, session items and structured runtime failures.
`PROTOCOL_VERSION` is the single source for the protocol major; the reference
document states what that major means.

`initialRuntimeState()` mirrors Python's `inactive_runtime_state()` so both sides
start from identical placeholders, and each call returns independent arrays. These
local values never replace the authoritative `runtime_state` from the Bridge and do
not lift the synchronization barrier. `runtimeIsActive()` gives both frontends one
definition of an active runtime; `cacheHitPercent()` derives the session hit rate
from a `CacheUsage` snapshot without accumulating locally; `formatArguments()`
renders tool arguments for display.

`MessageDecoder` accepts a string that has already been UTF-8 decoded, validates
that each line is a JSON object with a `type`, and calls `finish()` at EOF to detect
a truncated final line. It does not validate business state.

## Build and pack

```sh
npm run build --workspace @nosis/protocol
npm pack --workspace @nosis/protocol   # prepack runs the build first
```

The package ships ESM JavaScript and type declarations built from `src/`. The main
entry is [`src/protocol.ts`](src/protocol.ts), the internal entry is
[`src/internal.ts`](src/internal.ts), and Node process communication lives in
[`@nosis/client`](../client/README.md).

The Python side constructs these messages in
[`interfaces/bridge/protocol.py`](../bridge/protocol.py), with the remaining
session-bound messages emitted inline by `bridge.py` and `__main__.py`.

## Changing the protocol

A change must cover, together:

1. `src/protocol.ts` (and `src/internal.ts` when the change is first-party only).
2. `interfaces/bridge/protocol.py` and the sending paths in `bridge.py` / `__main__.py`.
3. TUI and GUI message handling.
4. Python and frontend protocol tests.

Public field or semantic changes that break clients increment the major; additive
fields and event kinds do not. See the reference document for the versioning rule.
