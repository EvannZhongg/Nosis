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
        setTimeout(() => {
            options.onMessage({
              type: 'session_ready',
              session_id: 'sess-1234',
              workspace: '/w',
              provider: 'test',
              model: 'test/model',
              resumed: false,
              message_count: 0,
              permission_preset: 'ask_for_approval',
            });
            options.onMessage({
              type: 'runtime_state',
              phase: 'inactive',
              turn_id: null,
              provider: 'test',
              permission_preset: 'ask_for_approval',
              context_window: null,
              jobs: [],
              approval: null,
              question: null,
              plan: null,
            });
          }, 10);
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
    />,
  );
}

/** The status bar shows the model once the Session control plane is ready. */
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

  it('renders plan progress from structured protocol state', async () => {
    const { lastFrame } = renderApp();
    await waitForReady(lastFrame);
    emit({
      type: 'plan_updated',
      plan: {
        plan_id: 'plan-1',
        goal: 'Ship plans',
        revision: 1,
        steps: [
          { id: 'inspect', title: 'Inspect architecture', status: 'completed', outcome: 'Architecture mapped.' },
          { id: 'frontend', title: 'Connect frontends', status: 'in_progress' },
          { id: 'verify', title: 'Verify recovery', status: 'pending' },
        ],
      },
    });

    await waitFor(() => {
      expect(lastFrame()).toContain('Plan');
      expect(lastFrame()).toContain('✓ Inspect architecture');
      expect(lastFrame()).toContain('Architecture mapped.');
      expect(lastFrame()).toContain('◉ Connect frontends');
      expect(lastFrame()).toContain('○ Verify recovery');
    });
  });

  it('opens the session with the workspace', async () => {
    renderApp();
    await waitFor(() =>
      expect(sent[0]).toMatchObject({
        type: 'open_session',
        workspace: '/w',
        session_id: null,
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

  it('keeps an opening draft without submitting before runtime synchronization', async () => {
    autoReady = false;
    const { stdin, lastFrame } = renderApp();
    await typeDraft(stdin, lastFrame, 'wait for sync');
    stdin.write('\r');
    await wait(20);
    expect(turns()).toHaveLength(0);
    expect(lastFrame()).toContain('wait for sync');

    emit({
      type: 'session_ready',
      session_id: 'sess-1234',
      workspace: '/w',
      provider: 'test',
      model: 'test/model',
      resumed: false,
      message_count: 0,
      permission_preset: 'ask_for_approval',
    });
    emit({
      type: 'runtime_state',
      phase: 'inactive',
      turn_id: null,
      provider: 'test',
      permission_preset: 'ask_for_approval',
      context_window: null,
      jobs: [],
      approval: null,
      question: null,
      plan: null,
    });
    await waitForReady(lastFrame);
    stdin.write('\r');
    await waitFor(() => expect(turns()[0]).toMatchObject({ text: 'wait for sync' }));
  });

  it('opens /permissions and sends the selected preset', async () => {
    const { stdin, lastFrame } = renderApp();
    await waitForReady(lastFrame);
    await typeDraft(stdin, lastFrame, '/permissions');
    stdin.write('\r');
    await waitFor(() => expect(lastFrame()).toContain('Ask for approval'));

    stdin.write('\u001b[B');
    await waitFor(() => expect(lastFrame()).toContain('❯ Workspace Access'));
    stdin.write('\u001b[B');
    await waitFor(() => expect(lastFrame()).toContain('❯ Full Access'));
    stdin.write('\r');

    await waitFor(() => expect(sent).toContainEqual({
      type: 'permission_set',
      preset: 'full_access',
    }));
    expect(turns()).toHaveLength(0);
  });

  it('opens /model and switches through provider_set', async () => {
    const { stdin, lastFrame } = renderApp();
    await waitForReady(lastFrame);
    await typeDraft(stdin, lastFrame, '/model');
    stdin.write('\r');
    await waitFor(() => expect(sent).toContainEqual({ type: 'settings_get' }));
    emit({
      type: 'settings_snapshot',
      settings: {
        revision: 'r1',
        config_directory: '/config',
        default_provider: 'test',
        providers: [
          { id: 'test', model: 'test/model', url: null, max_context_tokens: null, credential: { source: 'none', env_name: null, configured: false } },
          { id: 'second', model: 'test/second', url: null, max_context_tokens: null, credential: { source: 'none', env_name: null, configured: false } },
        ],
        routing: { main_agent: 'test', vision_provider: null, subagent: null, subagent_vision_provider: null, roles: {} },
        agent: { max_same_tool_calls: 5, output_reserve_tokens: 100, max_generation_tokens: null, workspace_instruction_files: ['AGENTS.md'], tools: {}, context: { compression: { enabled: true, trigger_ratio: null, keep_recent_units: 4 } }, memory: { enabled: true, global_max_tokens: 2000, workspace_max_tokens: 3000 }, subagent_roles: {}, mcp_enabled: false },
        skills: [], plugins: [], mcp_servers: [], plugin_agents: [], warnings: [],
      },
    });
    await waitFor(() => expect(lastFrame()).toContain('❯ test · test/model'));
    stdin.write('\u001b[B');
    await waitFor(() => expect(lastFrame()).toContain('❯ second · test/second'));
    stdin.write('\r');
    await waitFor(() => expect(sent).toContainEqual({ type: 'provider_set', provider: 'second' }));
    expect(turns()).toHaveLength(0);
  });

  it('shows /provider configuration without sending a turn', async () => {
    const { stdin, lastFrame } = renderApp();
    await waitForReady(lastFrame);
    await typeDraft(stdin, lastFrame, '/provider');
    stdin.write('\r');
    emit({
      type: 'settings_snapshot',
      settings: {
        revision: 'r1', config_directory: '/config', default_provider: 'test',
        providers: [{ id: 'test', model: 'test/model', url: null, max_context_tokens: null, credential: { source: 'none', env_name: null, configured: false } }],
        routing: { main_agent: 'test', vision_provider: null, subagent: null, subagent_vision_provider: null, roles: {} },
        agent: { max_same_tool_calls: 5, output_reserve_tokens: 100, max_generation_tokens: null, workspace_instruction_files: ['AGENTS.md'], tools: {}, context: { compression: { enabled: true, trigger_ratio: null, keep_recent_units: 4 } }, memory: { enabled: true, global_max_tokens: 2000, workspace_max_tokens: 3000 }, subagent_roles: {}, mcp_enabled: false },
        skills: [], plugins: [], mcp_servers: [], plugin_agents: [], warnings: [],
      },
    });
    await waitFor(() => expect(lastFrame()).toContain('Configuration · /config'));
    expect(turns()).toHaveLength(0);
  });

  it('lists the commands after a slash and runs the highlighted one', async () => {
    const { stdin, lastFrame } = renderApp();
    await waitForReady(lastFrame);

    stdin.write('/');
    await waitFor(() => expect(lastFrame()).toContain('❯ /permissions'));

    const frame = lastFrame() ?? '';
    const menuLine = lineContaining(frame, '❯ /permissions');
    const ruleLine = frame.split('\n').findIndex((line) => line.includes('─'));
    expect(menuLine).toBeGreaterThanOrEqual(0);
    expect(menuLine).toBeLessThan(ruleLine);

    stdin.write('\r');
    await waitFor(() => expect(lastFrame()).toContain('Ask for approval'));
    // The command opened the permission card instead of becoming a turn.
    expect(turns()).toHaveLength(0);
  });

  it('switches to another conversation and replays it', async () => {
    const { stdin, lastFrame } = renderApp();
    await waitForReady(lastFrame);

    await typeDraft(stdin, lastFrame, '/sessions');
    stdin.write('\r');
    await waitFor(() => expect(sent.find((m) => m.type === 'list_sessions')).toBeDefined());
    await waitFor(() => expect(lastFrame()).toContain('Sessions'));

    emit({
      type: 'sessions_listed',
      sessions: [
        { session_id: 'newer', title: 'about the parser' },
        { session_id: 'older', title: 'about the tests' },
      ],
    });
    await waitFor(() => expect(lastFrame()).toContain('❯ about the parser'));

    stdin.write('\u001b[B');
    await waitFor(() => expect(lastFrame()).toContain('❯ about the tests'));
    stdin.write('\r');

    // The runtime is replaced rather than re-pointed, so a second bridge
    // starts on the chosen session and the picker closes.
    await waitFor(() => expect(sent.filter((m) => m.type === 'open_session')).toHaveLength(2));
    expect(sent.filter((m) => m.type === 'open_session')[1]).toMatchObject({
      session_id: 'older',
    });
    expect(lastFrame()).not.toContain('❯ about the tests');

    emit({
      type: 'session_items',
      items: [
        { role: 'user', content: 'earlier question' },
        {
          role: 'assistant',
          content: 'earlier answer',
          timestamp_utc: '2026-09-16T10:00:00Z',
        },
      ],
    });
    await waitFor(() => expect(lastFrame()).toContain('earlier question'));
    expect(lastFrame()).toContain('earlier answer');
    expect(turns()).toHaveLength(0);
  });

  it('replays a long conversation without walking into the render limit', async () => {
    const { stdin, lastFrame } = renderApp();
    await waitForReady(lastFrame);

    await typeDraft(stdin, lastFrame, '/sessions');
    stdin.write('\r');
    await waitFor(() => expect(lastFrame()).toContain('Sessions'));
    emit({
      type: 'sessions_listed',
      sessions: [{ session_id: 'older', title: 'a long conversation' }],
    });
    await waitFor(() => expect(lastFrame()).toContain('a long conversation'));
    stdin.write('\r');
    await waitFor(() => expect(sent.filter((m) => m.type === 'open_session')).toHaveLength(2));

    // The whole conversation arrives in one message, so the transcript has
    // to commit it in one step: committing item by item from a single
    // update exhausts React's nested-update limit.
    emit({
      type: 'session_items',
      items: Array.from({ length: 60 }, (_, index) => ({
        role: index % 2 === 0 ? 'user' : 'assistant',
        content: `replayed line ${index + 1}`,
      })),
    });

    await waitFor(() => expect(lastFrame()).toContain('replayed line 60'));
    expect(lastFrame()).toContain('ask anything…');
  });

  it('closes the session picker on escape without switching', async () => {
    const { stdin, lastFrame } = renderApp();
    await waitForReady(lastFrame);

    await typeDraft(stdin, lastFrame, '/sessions');
    stdin.write('\r');
    await waitFor(() => expect(lastFrame()).toContain('Sessions'));

    stdin.write('\u001b');
    await waitFor(() => expect(lastFrame()).not.toContain('Loading…'));
    expect(sent.filter((m) => m.type === 'open_session')).toHaveLength(1);
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
    await waitFor(() => expect(lastFrame()).toContain('opening session…'));

    expect(
      lineContaining(lastFrame(), 'opening session…') -
        lineContaining(lastFrame(), 'steer the current turn…'),
    ).toBe(2);

    emit({
      type: 'session_ready',
      session_id: 'sess-1234',
      workspace: '/w',
      provider: 'test',
      model: 'test/model',
      resumed: false,
      message_count: 0,
      permission_preset: 'ask_for_approval',
    });
    emit({
      type: 'runtime_state',
      phase: 'inactive',
      turn_id: null,
      provider: 'test',
      permission_preset: 'ask_for_approval',
      context_window: null,
      jobs: [],
      approval: null,
      question: null,
      plan: null,
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
