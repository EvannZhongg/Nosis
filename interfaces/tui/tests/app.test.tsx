import { beforeEach, describe, expect, it, vi } from 'vitest';
import React from 'react';
import { render } from 'ink-testing-library';
import type { Incoming } from '@nosis/protocol';

const sent: any[] = [];
let emit: (message: Incoming) => void = () => {};
let autoReady = true;

vi.mock('../src/bridge.js', () => ({
  BridgeClient: class {
    constructor(options: any) {
      emit = options.onMessage;
      if (autoReady) {
        setTimeout(
          () =>
            options.onMessage({
              type: 'ready',
              session_id: 'sess-1234',
              workspace: '/w',
              model: 'test/model',
              resumed: false,
              message_count: 0,
            }),
          10,
        );
      }
    }
    send(message: any) {
      sent.push(message);
    }
    cancel(turnId: string) {
      sent.push({ type: '__cancel', turn_id: turnId });
    }
    shutdown() {}
    diagnostics() {
      return '';
    }
  },
}));

const { App } = await import('../src/app.js');

const wait = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

type Frame = () => string | undefined;

/**
 * Polls an assertion instead of sleeping for a fixed delay, so the suite does
 * not race Ink when the machine is busy. The condition must also survive one
 * event-loop turn before we return: Ink detaches its stdin listener while focus
 * hands over to the approval prompt and only re-attaches it in a passive
 * effect, so a frame can look right slightly before keys are accepted.
 */
async function waitFor(assertion: () => void, timeout = 1500): Promise<void> {
  const deadline = Date.now() + timeout;
  let passes = 0;
  for (;;) {
    try {
      assertion();
      passes += 1;
      if (passes === 2) return;
    } catch (error) {
      passes = 0;
      if (Date.now() >= deadline) throw error;
    }
    await wait(10);
  }
}

function renderApp() {
  return render(
    <App
      python="python3"
      workspace="/w"
      sessionId={null}
      providerConfigPath="/p"
      agentConfigPath="/a"
    />,
  );
}

/** The status bar only shows the model once the runtime reported ready. */
const waitForReady = (lastFrame: Frame) =>
  waitFor(() => expect(lastFrame()).toContain('test/model'));

const approvalResponse = () => sent.find((m) => m.type === 'approval_response');
const questionResponse = () => sent.find((m) => m.type === 'user_question_response');
const turns = () => sent.filter((m) => m.type === 'user_turn');
const steers = () => sent.filter((m) => m.type === 'user_steer');

/** Types text and waits until Ink has parsed it into the draft. */
async function typeDraft(
  stdin: { write: (data: string) => void },
  lastFrame: Frame,
  text: string,
): Promise<void> {
  stdin.write(text);
  await waitFor(() => expect(lastFrame()).toContain(text));
}

const requestApproval = (command: string): void => {
  emit({ type: 'approval_request', turn_id: 't1', request_id: 't1:1', command });
};

const requestQuestion = (allowFreeText = true): void => {
  emit({
    type: 'user_question',
    turn_id: 't1',
    request_id: 't1:1',
    question: 'Which cache?',
    options: [
      { id: 'memory', label: 'Memory', description: 'Fast' },
      { id: 'sqlite', label: 'SQLite', description: 'Persistent', recommended: true },
    ],
    allow_free_text: allowFreeText,
  });
};

function lineContaining(frame: string | undefined, text: string): number {
  return (frame ?? '').split('\n').findIndex((line) => line.includes(text));
}

describe('App', () => {
  beforeEach(() => {
    sent.length = 0;
    autoReady = true;
  });

  it('starts the runtime with the workspace and config paths', async () => {
    renderApp();
    await waitFor(() =>
      expect(sent[0]).toMatchObject({
        type: 'start',
        workspace: '/w',
        session_id: null,
        provider_config_path: '/p',
        agent_config_path: '/a',
      }),
    );
  });

  it('submits a typed turn once the runtime is idle', async () => {
    const { stdin, lastFrame } = renderApp();
    await waitForReady(lastFrame);
    await typeDraft(stdin, lastFrame, 'hello there');
    stdin.write('\r');
    await waitFor(() =>
      expect(sent.find((m) => m.type === 'user_turn')).toMatchObject({ text: 'hello there' }),
    );
  });

  it('sends steering while the agent is busy', async () => {
    const { stdin, lastFrame } = renderApp();
    await waitForReady(lastFrame);

    await typeDraft(stdin, lastFrame, 'first');
    stdin.write('\r');
    await waitFor(() => expect(turns()).toHaveLength(1));

    await typeDraft(stdin, lastFrame, 'second');
    stdin.write('\r');
    // The draft clears immediately and the message targets the active turn.
    await waitFor(() => expect(lastFrame()).not.toContain('second'));
    expect(turns()).toHaveLength(1);
    await waitFor(() => expect(steers()).toHaveLength(1));
    expect(steers()[0]).toMatchObject({ turn_id: turns()[0].turn_id, text: 'second' });
  });

  it('keeps the input above the one-line status when a turn starts running', async () => {
    const { stdin, lastFrame } = renderApp();
    await waitForReady(lastFrame);

    await typeDraft(stdin, lastFrame, 'run task');
    stdin.write('\r');
    await waitFor(() => expect(lastFrame()).toContain('esc to cancel'));

    const inputLine = lineContaining(lastFrame(), 'steer the current turn…');
    const statusLine = lineContaining(lastFrame(), 'test/model');
    expect(statusLine).toBe(inputLine + 2);
    expect((lastFrame() ?? '').split('\n')[statusLine]).toContain('esc to cancel');
  });

  it('defaults to allow and confirms with enter', async () => {
    const { stdin, lastFrame } = renderApp();
    await waitForReady(lastFrame);
    requestApproval('ls -la');
    await waitFor(() => expect(lastFrame()).toContain('❯ Allow'));

    stdin.write('\r');
    await waitFor(() =>
      expect(approvalResponse()).toMatchObject({ request_id: 't1:1', approved: true }),
    );
  });

  it('denies after moving the selection with an arrow key', async () => {
    const { stdin, lastFrame } = renderApp();
    await waitForReady(lastFrame);
    requestApproval('rm -rf /');
    await waitFor(() => expect(lastFrame()).toContain('❯ Allow'));

    stdin.write('\u001b[C'); // right arrow
    await waitFor(() => expect(lastFrame()).toContain('❯ Deny'));

    stdin.write('\r');
    await waitFor(() => expect(approvalResponse()).toMatchObject({ approved: false }));
  });

  it('toggles back to allow with the left arrow', async () => {
    const { stdin, lastFrame } = renderApp();
    await waitForReady(lastFrame);
    requestApproval('ls -la');
    await waitFor(() => expect(lastFrame()).toContain('❯ Allow'));

    stdin.write('\u001b[C'); // right arrow
    await waitFor(() => expect(lastFrame()).toContain('❯ Deny'));
    stdin.write('\u001b[D'); // left arrow
    await waitFor(() => expect(lastFrame()).toContain('❯ Allow'));

    stdin.write('\r');
    await waitFor(() => expect(approvalResponse()).toMatchObject({ approved: true }));
  });

  it('denies the command when escape is pressed', async () => {
    const { stdin, lastFrame } = renderApp();
    await waitForReady(lastFrame);
    requestApproval('ls -la');
    await waitFor(() => expect(lastFrame()).toContain('❯ Allow'));

    stdin.write('\u001b');
    await waitFor(() => expect(approvalResponse()).toMatchObject({ approved: false }));
  });

  it('selects and returns a question option', async () => {
    const { stdin, lastFrame } = renderApp();
    await waitForReady(lastFrame);
    requestQuestion(false);
    await waitFor(() => expect(lastFrame()).toContain('❯ SQLite'));

    stdin.write('\u001b[A');
    await waitFor(() => expect(lastFrame()).toContain('❯ Memory'));
    stdin.write('\r');
    await waitFor(() => expect(questionResponse()).toMatchObject({ option_id: 'memory' }));
  });

  it('accepts a free-text question answer', async () => {
    const { stdin, lastFrame } = renderApp();
    await waitForReady(lastFrame);
    requestQuestion(true);
    await waitFor(() => expect(lastFrame()).toContain('Which cache?'));

    stdin.write('\u001b[B');
    await waitFor(() => expect(lastFrame()).toContain('❯ 其他答案'));
    await typeDraft(stdin, lastFrame, 'Redis');
    stdin.write('\r');
    await waitFor(() => expect(questionResponse()).toMatchObject({ text: 'Redis' }));
  });

  it('cancels the active turn on escape', async () => {
    const { stdin, lastFrame } = renderApp();
    await waitForReady(lastFrame);
    await typeDraft(stdin, lastFrame, 'long task');
    stdin.write('\r');
    // Wait for the busy status: escape only cancels once the turn is running.
    await waitFor(() => expect(lastFrame()).toContain('esc to cancel'));

    stdin.write('\u001b');
    await waitFor(() => expect(sent).toContainEqual({ type: '__cancel', turn_id: 'turn-1' }));
  });

  it('renders streamed reasoning before the answer', async () => {
    const { lastFrame } = renderApp();
    await waitForReady(lastFrame);
    emit({
      type: 'reasoning_delta',
      turn_id: 't1',
      text: 'weighing ',
      model_call_index: 1,
    });
    emit({
      type: 'reasoning_delta',
      turn_id: 't1',
      text: 'the options',
      model_call_index: 1,
    });
    // Both deltas land in one live entry, so the frame carries the whole text.
    await waitFor(() => expect(lastFrame()).toContain('weighing the options'));
  });

  it('grows model output downward from the top of the TUI', async () => {
    const { lastFrame } = renderApp();
    await waitForReady(lastFrame);

    emit({
      type: 'assistant_delta',
      turn_id: 't1',
      text: 'first output',
      model_call_index: 1,
    });
    await waitFor(() => expect(lastFrame()).toContain('first output'));

    expect(lineContaining(lastFrame(), 'first output')).toBeLessThan(3);
    expect(lineContaining(lastFrame(), 'first output')).toBeLessThan(
      lineContaining(lastFrame(), 'steer the current turn…'),
    );
  });

  it('keeps short multiline output in the viewport until it fills downward', async () => {
    const { lastFrame } = renderApp();
    await waitForReady(lastFrame);

    emit({
      type: 'assistant_delta',
      turn_id: 't1',
      text: 'line one\nline two\nline three\nline four\nline five',
      model_call_index: 1,
    });
    await waitFor(() => expect(lastFrame()).toContain('line five'));

    const first = lineContaining(lastFrame(), 'line one');
    const fifth = lineContaining(lastFrame(), 'line five');
    const input = lineContaining(lastFrame(), 'steer the current turn…');
    expect(first).toBeLessThan(3);
    expect(fifth).toBe(first + 4);
    expect(input).toBeGreaterThan(fifth + 1);
  });

  it('lets long model output grow through the terminal stream', async () => {
    const { lastFrame } = renderApp();
    await waitForReady(lastFrame);

    emit({
      type: 'assistant_delta',
      turn_id: 't1',
      text: Array.from({ length: 30 }, (_, index) => `output line ${index + 1}`).join('\n'),
      model_call_index: 1,
    });

    await waitFor(() => {
      const frame = lastFrame() ?? '';
      expect(frame).toContain('output line 1');
      expect(frame).toContain('output line 30');
      expect(frame).toContain('steer the current turn…');
      expect(frame).toContain('test/model');
    });
  });

  it('keeps completed output in the transcript viewport', async () => {
    const { lastFrame } = renderApp();
    await waitForReady(lastFrame);

    emit({
      type: 'assistant_delta',
      turn_id: 't1',
      text: 'completed answer',
      model_call_index: 1,
    });
    emit({
      type: 'assistant_message',
      turn_id: 't1',
      content: 'completed answer',
      timestamp_utc: '2026-09-15T12:00:00Z',
    });
    emit({ type: 'turn_completed', turn_id: 't1', usage: null });

    await waitFor(() => {
      expect(lastFrame()).toContain('completed answer');
      expect(lastFrame()).toContain('test/model');
    });
  });

  it('renders streamed text and the approval prompt', async () => {
    const { lastFrame } = renderApp();
    await waitForReady(lastFrame);
    emit({
      type: 'assistant_delta',
      turn_id: 't1',
      text: 'partial answer',
      model_call_index: 1,
    });
    await waitFor(() => expect(lastFrame()).toContain('partial answer'));

    requestApproval('echo hi');
    await waitFor(() => {
      expect(lastFrame()).toContain('Shell command requires approval');
      expect(lastFrame()).toContain('echo hi');
    });
  });

  it('renders approval above the input and model status', async () => {
    const { lastFrame } = renderApp();
    await waitForReady(lastFrame);

    requestApproval('echo hi');
    await waitFor(() => expect(lastFrame()).toContain('Shell command requires approval'));

    const approvalLine = lineContaining(lastFrame(), 'Shell command requires approval');
    const inputLine = lineContaining(lastFrame(), 'steer the current turn…');
    const modelLine = lineContaining(lastFrame(), 'test/model');
    expect(approvalLine).toBeLessThan(inputLine);
    expect(modelLine).toBe(inputLine + 2);
  });

  it('uses one status line before and after startup', async () => {
    autoReady = false;
    const { lastFrame } = renderApp();
    await waitFor(() => expect(lastFrame()).toContain('starting agent…'));

    expect(
      lineContaining(lastFrame(), 'starting agent…') -
        lineContaining(lastFrame(), 'steer the current turn…'),
    ).toBe(2);

    emit({
      type: 'ready',
      session_id: 'sess-1234',
      workspace: '/w',
      model: 'test/model',
      resumed: false,
      message_count: 0,
    });
    await waitForReady(lastFrame);

    expect(
      lineContaining(lastFrame(), 'test/model') -
        lineContaining(lastFrame(), 'ask anything…'),
    ).toBe(2);
  });

  it('updates a later tool when it completes before an earlier call', async () => {
    const { lastFrame } = renderApp();
    await waitForReady(lastFrame);

    emit({
      type: 'tool_call',
      turn_id: 't1',
      tool_call: { id: 'call-a', name: 'subagent', arguments: { task: 'A' } },
      tool_index: 1,
      tool_count: 3,
    });
    emit({
      type: 'tool_call',
      turn_id: 't1',
      tool_call: { id: 'call-b', name: 'subagent', arguments: { task: 'B' } },
      tool_index: 2,
      tool_count: 3,
    });
    emit({
      type: 'tool_call',
      turn_id: 't1',
      tool_call: { id: 'call-c', name: 'subagent', arguments: { task: 'C' } },
      tool_index: 3,
      tool_count: 3,
    });
    await waitFor(() => {
      expect(lastFrame()).toContain('[1/3]');
      expect(lastFrame()).toContain('[2/3]');
      expect(lastFrame()).toContain('[3/3]');
    });

    emit({
      type: 'tool_result',
      turn_id: 't1',
      tool_call_id: 'call-b',
      name: 'subagent',
      ok: true,
      error: null,
      tool_index: 2,
      tool_count: 3,
    });

    await waitFor(() => {
      const frame = lastFrame() ?? '';
      expect(frame).toContain('[1/3]');
      expect(frame).toContain('subagent task=A');
      expect(frame).toContain('[2/3] ✓ subagent task=B');
      expect(frame).toContain('[3/3]');
      expect(frame).toContain('subagent task=C');
    });
  });

  it('shows the index for a single tool call', async () => {
    const { lastFrame } = renderApp();
    await waitForReady(lastFrame);

    emit({
      type: 'tool_call',
      turn_id: 't1',
      tool_call: { id: 'call-1', name: 'shell', arguments: { command: 'ls' } },
      tool_index: 1,
      tool_count: 1,
    });

    await waitFor(() => expect(lastFrame()).toContain('[1/1]'));
  });
});
