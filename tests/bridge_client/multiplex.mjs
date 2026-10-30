// Installed SDK + real Bridge: one process, independent runtime channels.
import assert from 'node:assert/strict';
import { resolve } from 'node:path';
import { BridgeClient } from '@nosis/client';

const queues = new Map();
const logs = new Map();
const waiters = new Map();
const errors = [];
let processReady;
let processEnded = false;
const ready = new Promise(resolve => { processReady = resolve; });
const client = new BridgeClient({
  command: process.env.NOSIS_TEST_BRIDGE,
  cwd: process.cwd(),
  onReady: () => processReady(),
  onMessage: (event, channelId) => deliver(channelId, event),
  onChannelClosed: channelId => deliver(channelId, { type: 'channel_closed' }),
  onProtocolError: error => errors.push(error.message),
  onProcessError: error => errors.push(error.message),
  onExit: () => {
    processEnded = true;
    for (const waiter of waiters.values()) waiter.reject(new Error('Bridge exited before the expected event'));
  },
});

function deliver(channelId, event) {
  if (!logs.has(channelId)) logs.set(channelId, []);
  logs.get(channelId).push(event);
  const waiter = waiters.get(channelId);
  if (waiter) {
    waiters.delete(channelId);
    waiter.resolve(event);
  } else {
    if (!queues.has(channelId)) queues.set(channelId, []);
    queues.get(channelId).push(event);
  }
}

async function until(channelId, kind) {
  for (;;) {
    const queued = queues.get(channelId);
    const event = queued?.length ? queued.shift() : await new Promise((resolve, reject) => {
      assert.equal(processEnded, false);
      waiters.set(channelId, { resolve, reject });
    });
    assert.notEqual(event.type, 'fatal', JSON.stringify(event));
    assert.notEqual(event.type, 'turn_failed', JSON.stringify(event));
    assert.notEqual(event.type, 'session_open_failed', JSON.stringify(event));
    if (event.type === kind) return event;
  }
}

function open(channelId) {
  client.send({
    type: 'open_session', workspace: resolve(channelId), session_id: `sdk-${channelId}`, provider: 'fixture',
  }, channelId);
}

const timeout = setTimeout(() => { void client.shutdown(); }, 45000);
try {
  await ready;
  open('one');
  open('two');
  await Promise.all([until('one', 'runtime_state'), until('two', 'runtime_state')]);
  client.send({ type: 'permission_set', preset: 'full_access' }, 'one');
  await until('one', 'permission_changed');
  client.send({ type: 'user_turn', turn_id: 'same', text: 'blocked' }, 'one');
  assert.equal((await until('one', 'assistant_delta')).text, 'waiting');
  client.send({ type: 'user_turn', turn_id: 'same', text: 'second' }, 'two');
  const second = await until('two', 'turn_completed');
  assert.equal(second.runtime.permission_preset, 'ask_for_approval');
  assert.ok(second.transcript.items.some(item => item.content === 'reply:second'));
  client.cancel('same', 'one');
  const cancelled = await until('one', 'turn_cancelled');
  assert.equal(cancelled.runtime.permission_preset, 'full_access');
  client.send({ type: 'session_snapshot' }, 'one');
  const original = await until('one', 'session_ready');
  const snapshot = await until('one', 'runtime_state');
  assert.equal(original.session_id, 'sdk-one');
  assert.equal(snapshot.phase, 'idle');
  assert.deepEqual(snapshot.context_window, cancelled.runtime.context_window);
  client.closeChannel('one');
  await until('one', 'channel_closed');
  assert.equal(processEnded, false);
  client.send({ type: 'user_turn', turn_id: 'next', text: 'after-close' }, 'two');
  const continued = await until('two', 'turn_completed');
  assert.ok(continued.transcript.items.some(item => item.content === 'reply:after-close'));
  for (const channelId of ['one', 'two']) {
    const events = logs.get(channelId).filter(event => event.event_sequence !== undefined);
    assert.deepEqual(events.map(event => event.event_sequence), events.map((_, index) => index + 1));
  }
  open('one');
  const reopened = await until('one', 'session_ready');
  assert.equal(reopened.permission_preset, 'full_access');
  assert.ok(reopened.transcript.items.some(item => item.role === 'user' && item.content === 'blocked'));
  assert.equal((await until('one', 'runtime_state')).phase, 'inactive');
  assert.equal((await client.shutdown()).code, 0);
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ channels: 2, isolated: true, reused_process: true }));
} finally {
  clearTimeout(timeout);
  await client.shutdown();
}
