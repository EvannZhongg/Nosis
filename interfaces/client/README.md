# @nosis/client

A small Node.js (22+) ESM client for the [public stdio Bridge API](../../docs/bridge-protocol.md).
It reuses the TUI's process transport and `@nosis/protocol` framing. It owns the
child process, dispatches messages in wire order and keeps bounded stderr
diagnostics. It holds no Runtime, Session, transcript or permission state.

Install `nosis-agent` in a Python environment to obtain `nosis-bridge`. Install
`@nosis/client` and `@nosis/protocol` package artifacts in the Node application.
These packages can be built/packed locally; registry publication is separate:

```sh
npm install
npm run build --workspace @nosis/protocol
npm run build --workspace @nosis/client
npm pack --workspace @nosis/protocol
npm pack --workspace @nosis/client
# In the external application, install both generated tarballs together.
npm install /path/to/nosis-protocol-0.1.0.tgz /path/to/nosis-client-0.1.0.tgz
```

Run [`examples/chat.mjs`](examples/chat.mjs) with a workspace and prompt. The example
uses the installed command on PATH, checks the advertised protocol major, waits
for the initial Runtime snapshot, denies Tool approval and cancels unanswered
questions. Real frontends should display those interactions and send the user's
answer. Model turns require configured provider credentials.

```sh
node node_modules/@nosis/client/examples/chat.mjs /path/to/workspace "Hello"
```

`new BridgeClient(options)` starts the process. Options:

- `command` (default `nosis-bridge`), optional `args`, `cwd`, `env`: explicit
  process inputs. An absolute executable path can select a Python environment.
  Supplying `env` replaces the child environment; spread `process.env` to extend it.
- `onMessage(message, channelId)`: decoded channel events, including unknown additive session events.
- `onReady()`: process warmup completed and the protocol major was checked.
- `onChannelClosed(channelId)`: the runtime and its Journal writer have closed.
- `onProtocolError`: invalid JSON/envelope or output truncated before newline;
  the SDK initiates shutdown. Unknown event kinds are delivered unchanged.
- `onProcessError`: spawn or stdin errors. These are not Agent failures.
- `onExit(code, signal)`: called after stdout/stderr drain, following any final messages.

`send(Outgoing, channelId)` wraps one channel command and writes it without synchronization, retries or synthetic
responses. It throws after shutdown/input close; asynchronous pipe errors go to
`onProcessError`. `cancel(turnId, channelId)` sends cancellation only to that runtime.
`closeChannel(channelId)` closes one runtime; completion is `onChannelClosed`.
One client/process can host multiple concurrent channels. Callers keep session
snapshots and event cursors separately for each channel.
The channel ID is required; there is no implicit default session. Revisit an open
runtime with `send({type: 'session_snapshot'}, channelId)`: this returns its
`session_ready` and authoritative `runtime_state` without rebinding the Session,
reconstructing the Agent or disconnecting MCP.

```js
client.send({ type: 'open_session', workspace: '/project/a', session_id: null }, 'chat-a');
client.send({ type: 'open_session', workspace: '/project/b', session_id: null }, 'chat-b');
// After each channel's initial runtime_state:
client.send({ type: 'user_turn', turn_id: 'turn-1', text: 'Inspect A' }, 'chat-a');
client.send({ type: 'user_turn', turn_id: 'turn-1', text: 'Inspect B' }, 'chat-b');
client.cancel('turn-1', 'chat-a');
client.closeChannel('chat-a'); // chat-b and the process continue.
```

`shutdown()` is idempotent and returns the same completion promise as `closed`:
`{code, signal}` after process close. It sends shutdown, ends stdin and enforces
the 2-second cleanup bound. `diagnostics()` returns the last 50 stderr chunks.
Callbacks must not throw. Channel `fatal` is an event; top-level process failures
are reported to `onProtocolError` and followed by process shutdown.

The generic receive/send type parameters permit first-party transport consumers
to use the internal message superset without another process implementation;
their default types are the public protocol. They add no Runtime behavior.

This SDK does not implement GUI Host WebSocket attachment/replay, command buffering,
automatic rebind/restart, RPC or credential management. Apply snapshots/checkpoints
and synchronize commands in your own frontend according to the protocol reference.

To verify an external integration, install Python dependencies plus pip and
`setuptools>=68`, run `npm install` at the repository root, then:

```sh
python -m unittest tests.test_bridge_client -v
```

This builds a wheel and both npm tarballs, installs them into temporary directories
outside the repository, and runs the CLI and an ESM application with an isolated
home. A loopback provider fixture supplies model responses for actual Runtime
turns; the tests do not need provider credentials or a paid model service.
