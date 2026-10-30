// Runs from an external npm installation; imports only the public packages.
import assert from 'node:assert/strict';
import { BridgeClient } from '@nosis/client';
import { PROTOCOL_VERSION } from '@nosis/protocol';

const messages = [];
const problems = [];
let ready;
let stage = 'open';
let synchronized = false;
const client = new BridgeClient({
  command: process.env.NOSIS_TEST_BRIDGE,
  cwd: process.cwd(),
  onMessage(message) {
    try {
      handleMessage(message);
    } catch (error) {
      problems.push(error.stack);
      void client.shutdown();
    }
  },
  onProtocolError(error) { problems.push(error.message); },
  onProcessError(error) { problems.push(error.message); },
  onExit() {},
});

function handleMessage(message) {
  messages.push(message);
  if (message.type === 'session_ready') {
    assert.equal(message.protocol_version, PROTOCOL_VERSION);
    ready = message;
    assert.deepEqual(message.transcript.items, []);
  }
  if (message.type === 'runtime_state' && ready && !synchronized) {
    synchronized = true;
    stage = 'complete';
    client.send({ type: 'user_turn', turn_id: 'complete', text: 'External 中文 client' }, 'main');
  }
  if (message.type === 'approval_request') {
    assert.equal(message.turn_id, 'complete');
    assert.equal(message.kind, 'shell');
    client.send({ type: 'approval_response', request_id: 'stale', approved: true }, 'main');
    client.send({ type: 'approval_response', request_id: message.request_id, approved: false }, 'main');
  }
  if (message.type === 'user_question') {
    if (stage === 'complete') {
      client.send({ type: 'user_steer', turn_id: 'complete', steer_id: 'steer-1', text: 'Use this answer' }, 'main');
      client.send({ type: 'user_question_response', request_id: message.request_id, option_id: 'yes' }, 'main');
    } else if (stage === 'cancel') client.cancel('cancel', 'main');
  }
  if (message.type === 'turn_completed') {
    assert.equal(stage, 'complete');
    assert.equal(message.turn_id, 'complete');
    assert.ok(message.transcript.items.some(item => item.role === 'assistant' && item.content === '完成🙂'));
    assert.ok(messages.some(item => item.type === 'user_steer_applied' && item.steer_id === 'steer-1'));
    stage = 'cancel';
    client.send({ type: 'user_turn', turn_id: 'cancel', text: 'Wait for cancellation' }, 'main');
  }
  if (message.type === 'turn_cancelled') {
    assert.equal(message.turn_id, 'cancel');
    assert.equal(message.runtime.phase, 'idle');
    assert.ok(message.transcript.items.length > 0);
    stage = 'failure';
    client.send({
      type: 'user_turn', turn_id: 'failure', text: 'Invalid local attachment',
      attachments: [{ type: 'file', path: 'missing.txt', filename: 'missing.txt', mime_type: 'text/plain', size_bytes: 1 }],
    }, 'main');
  }
  if (message.type === 'turn_failed') {
    assert.equal(stage, 'failure');
    assert.equal(message.turn_id, 'failure');
    assert.equal(typeof message.error.message, 'string');
    assert.equal(typeof message.error.details, 'object');
    assert.equal(message.runtime.phase, 'idle');
    stage = 'history';
    client.send({ type: 'load_session' }, 'main');
  }
  if (message.type === 'session_items' && stage === 'history') {
    assert.ok(message.items.some(item => item.role === 'user'));
    stage = 'done';
    void client.shutdown();
  }
  if (message.type === 'fatal' || message.type === 'session_open_failed') {
    problems.push(message);
    void client.shutdown();
  }
}
const timeout = setTimeout(() => {
  console.error(JSON.stringify({ stage, messages, diagnostics: client.diagnostics() }));
  void client.shutdown();
}, 45000);
client.send({ type: 'open_session', workspace: process.cwd(), session_id: null, provider: 'fixture' }, 'main');
const result = await client.closed;
clearTimeout(timeout);
assert.equal(stage, 'done', JSON.stringify({ stage, messages, diagnostics: client.diagnostics() }));
assert.deepEqual(problems, []);
assert.equal(result.code, 0);
assert.equal(messages.filter(message => message.type === 'approval_request').length, 1);
assert.equal(messages.filter(message => message.type === 'user_question').length, 2);
assert.equal(messages.filter(message => message.type === 'model_completed').length, 4);
assert.ok(messages.some(message => message.type === 'assistant_delta' && message.text === '完成🙂'));
assert.ok(messages.some(message => message.type === 'tool_result' && message.name === 'shell' && !message.ok));
for (let i = 0; i < messages.length; i++) assert.equal(messages[i].event_sequence, i + 1);
for (const message of messages) {
  if (message.transcript) assert.equal(message.transcript.event_sequence, message.event_sequence);
  assert.notEqual(message.type, 'attachment_replaced');
  assert.equal(message.replayed, undefined);
}
for (const turn of ['complete', 'cancel', 'failure']) {
  const turnEvents = messages.filter(message => message.turn_id === turn);
  assert.equal(turnEvents[0].type, 'user_turn_received');
  assert.equal(turnEvents.filter(message => ['turn_completed', 'turn_cancelled', 'turn_failed'].includes(message.type)).length, 1);
}
console.log(JSON.stringify({ session_id: ready.session_id, events: messages.length }));
