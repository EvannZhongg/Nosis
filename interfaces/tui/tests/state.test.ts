import { describe, expect, it } from 'vitest';
import { MessageDecoder, formatArguments } from '@nosis/protocol';
import { initialState, reducer, type State } from '../src/state.js';

const READY = {
  type: 'ready' as const,
  session_id: 'abc12345-0000',
  workspace: '/tmp/work',
  model: 'deepseek/deepseek-chat',
  resumed: false,
  message_count: 0,
};

function ready(): State {
  return reducer(initialState, { type: 'message', message: READY });
}

describe('MessageDecoder', () => {
  it('emits complete lines only', () => {
    const decoder = new MessageDecoder();
    expect(decoder.push('{"type":"shut')).toEqual([]);
    expect(decoder.push('down"}\n')).toEqual([{ type: 'shutdown' }]);
  });

  it('handles several messages in one chunk', () => {
    const decoder = new MessageDecoder();
    const messages = decoder.push('{"type":"shutdown"}\n{"type":"shutdown"}\n');
    expect(messages).toHaveLength(2);
  });

  it('skips blank lines', () => {
    const decoder = new MessageDecoder();
    expect(decoder.push('\n\n{"type":"shutdown"}\n')).toHaveLength(1);
  });

  it('throws on malformed json', () => {
    const decoder = new MessageDecoder();
    expect(() => decoder.push('not json\n')).toThrow();
  });
});

describe('helpers', () => {
  it('formats tool arguments compactly', () => {
    expect(formatArguments({ command: 'ls -la' })).toBe('command=ls -la');
    expect(formatArguments({})).toBe('');
  });
});

describe('reducer', () => {
  it('becomes idle when the runtime is ready', () => {
    const state = ready();
    expect(state.status).toBe('idle');
    expect(state.model).toBe('deepseek/deepseek-chat');
  });

  it('keeps MCP startup status out of the persistent transcript', () => {
    const state = reducer(initialState, {
      type: 'message',
      message: { type: 'mcp_server_status', server: 'mineru', status: 'ready', tool_count: 2 },
    });
    expect(state.mcpStatus).toBe('MCP mineru: ready (2 tools)');
    expect(state.entries).toEqual([]);
    expect(reducer(state, { type: 'message', message: READY }).mcpStatus).toBeNull();
  });

  it('notes resumed sessions', () => {
    const state = reducer(initialState, {
      type: 'message',
      message: { ...READY, resumed: true, message_count: 4 },
    });
    expect(state.entries[0]).toMatchObject({ kind: 'notice', level: 'info' });
  });

  it('shows skill discovery warnings without blocking startup', () => {
    const state = reducer(initialState, {
      type: 'message',
      message: { ...READY, skill_warnings: ['Skipping invalid skill.'] },
    });
    expect(state.status).toBe('idle');
    expect(state.entries[0]).toMatchObject({
      kind: 'notice',
      level: 'info',
      text: 'Skipping invalid skill.',
    });
  });

  it('accumulates deltas into one assistant entry', () => {
    let state = ready();
    state = reducer(state, { type: 'submit', turnId: 't1', text: 'hi' });
    for (const text of ['He', 'llo', '!']) {
      state = reducer(state, {
        type: 'message',
        message: { type: 'assistant_delta', turn_id: 't1', text, model_call_index: 1 },
      });
    }
    const assistants = state.entries.filter((entry) => entry.kind === 'assistant');
    expect(assistants).toHaveLength(1);
    expect(assistants[0]).toMatchObject({ text: 'Hello!', settled: false });
    expect(state.status).toBe('streaming');
  });

  it('settles the streamed entry instead of duplicating it', () => {
    let state = ready();
    state = reducer(state, { type: 'submit', turnId: 't1', text: 'hi' });
    state = reducer(state, {
      type: 'message',
      message: { type: 'assistant_delta', turn_id: 't1', text: 'Hel', model_call_index: 1 },
    });
    state = reducer(state, {
      type: 'message',
      message: {
        type: 'assistant_message',
        turn_id: 't1',
        content: 'Hello!',
        timestamp_utc: '2026-09-09T08:00:00.000000Z',
        model_call_index: 1,
      },
    });
    const assistants = state.entries.filter((entry) => entry.kind === 'assistant');
    expect(assistants).toHaveLength(1);
    expect(assistants[0]).toMatchObject({ text: 'Hello!', settled: true });
  });

  it('accumulates reasoning deltas into one entry', () => {
    let state = ready();
    state = reducer(state, { type: 'submit', turnId: 't1', text: 'hi' });
    for (const text of ['Let me ', 'weigh ', 'the options.']) {
      state = reducer(state, {
        type: 'message',
        message: { type: 'reasoning_delta', turn_id: 't1', text, model_call_index: 1 },
      });
    }
    const reasoning = state.entries.filter((entry) => entry.kind === 'reasoning');
    expect(reasoning).toHaveLength(1);
    expect(reasoning[0]).toMatchObject({
      text: 'Let me weigh the options.',
      settled: false,
    });
  });

  it('closes the reasoning entry once the answer is settled', () => {
    let state = ready();
    state = reducer(state, { type: 'submit', turnId: 't1', text: 'hi' });
    state = reducer(state, {
      type: 'message',
      message: { type: 'reasoning_delta', turn_id: 't1', text: 'weighing', model_call_index: 1 },
    });
    state = reducer(state, {
      type: 'message',
      message: {
        type: 'assistant_message',
        turn_id: 't1',
        content: 'Hello!',
        timestamp_utc: '2026-09-09T08:00:00.000000Z',
        model_call_index: 1,
      },
    });

    expect(state.entries.map((entry) => entry.kind)).toEqual([
      'user',
      'reasoning',
      'assistant',
    ]);
    expect(state.entries[1]).toMatchObject({ text: 'weighing', settled: true });
  });

  it('closes reasoning at the end of a turn and opens a new entry later', () => {
    let state = ready();
    state = reducer(state, { type: 'submit', turnId: 't1', text: 'hi' });
    state = reducer(state, {
      type: 'message',
      message: { type: 'reasoning_delta', turn_id: 't1', text: 'first', model_call_index: 1 },
    });
    state = reducer(state, {
      type: 'message',
      message: { type: 'turn_completed', turn_id: 't1', usage: null },
    });
    expect(state.entries.at(-1)).toMatchObject({ kind: 'reasoning', settled: true });

    state = reducer(state, { type: 'submit', turnId: 't2', text: 'again' });
    state = reducer(state, {
      type: 'message',
      message: { type: 'reasoning_delta', turn_id: 't2', text: 'second', model_call_index: 1 },
    });
    const reasoning = state.entries.filter((entry) => entry.kind === 'reasoning');
    expect(reasoning).toHaveLength(2);
    expect(reasoning[0]).toMatchObject({ text: 'first', settled: true });
    expect(reasoning[1]).toMatchObject({ text: 'second', settled: false });
  });

  it('resolves a tool entry in place', () => {
    let state = ready();
    state = reducer(state, {
      type: 'message',
      message: {
        type: 'tool_call',
        turn_id: 't1',
        tool_call: { id: 'call-1', name: 'shell', arguments: { command: 'ls' } },
        tool_index: 1,
        tool_count: 1,
      },
    });
    expect(state.entries.filter((entry) => entry.kind === 'tool')).toHaveLength(1);

    state = reducer(state, {
      type: 'message',
      message: {
        type: 'tool_result',
        turn_id: 't1',
        tool_call_id: 'call-1',
        name: 'shell',
        ok: false,
        error: { type: 'PermissionError', message: 'denied' },
        tool_index: 1,
        tool_count: 1,
      },
    });
    const tools = state.entries.filter((entry) => entry.kind === 'tool');
    expect(tools).toHaveLength(1);
    expect(tools[0]).toMatchObject({ state: 'error' });
  });

  it('tracks an approval request and clears it once answered', () => {
    let state = ready();
    state = reducer(state, {
      type: 'message',
      message: {
        type: 'approval_request',
        turn_id: 't1',
        request_id: 't1:1',
        command: 'rm -rf build',
      },
    });
    expect(state.status).toBe('awaiting_approval');
    expect(state.approval).toEqual({
      requestId: 't1:1',
      command: 'rm -rf build',
      choice: 'allow',
    });

    state = reducer(state, { type: 'approval_choice', choice: 'deny' });
    expect(state.approval?.choice).toBe('deny');

    state = reducer(state, { type: 'approval_resolved' });
    expect(state.approval).toBeNull();
    expect(state.status).toBe('running');
  });

  it('records usage when a turn completes', () => {
    let state = ready();
    state = reducer(state, {
      type: 'message',
      message: {
        type: 'turn_completed',
        turn_id: 't1',
        usage: { input_tokens: 10, output_tokens: 2, total_tokens: 12 },
      },
    });
    expect(state.status).toBe('idle');
    expect(state.usage?.total_tokens).toBe(12);
  });

  it('reports a cancelled turn', () => {
    let state = ready();
    state = reducer(state, {
      type: 'message',
      message: { type: 'turn_cancelled', turn_id: 't1' },
    });
    expect(state.status).toBe('idle');
    expect(state.entries.at(-1)).toMatchObject({ text: 'Cancelled.' });
  });

  it('keeps the session usable after a failed turn', () => {
    let state = ready();
    state = reducer(state, {
      type: 'message',
      message: {
        type: 'turn_failed',
        turn_id: 't1',
        error: { type: 'ValueError', message: 'boom' },
      },
    });
    expect(state.status).toBe('idle');
    expect(state.entries.at(-1)).toMatchObject({ level: 'error' });
  });

  it('ignores cancellation while idle', () => {
    const state = ready();
    expect(reducer(state, { type: 'cancelling' })).toBe(state);
  });
});
