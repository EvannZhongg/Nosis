import type {
  Incoming,
  ContextWindow,
  PermissionPreset,
  PlanSnapshot,
  ProtocolError,
  SessionItem,
  SessionSummary,
  SettingsSnapshot,
  Usage,
  UserQuestionOption,
} from '@nosis/protocol';

export type Status =
  | 'opening'
  | 'starting'
  | 'idle'
  | 'streaming'
  | 'running'
  | 'awaiting_approval'
  | 'awaiting_user'
  | 'cancelling'
  | 'runtime_failed'
  | 'fatal';

export type Entry =
  | { kind: 'user'; id: string; text: string }
  | { kind: 'assistant'; id: string; text: string; settled: boolean; timestamp_utc?: string }
  | { kind: 'reasoning'; id: string; text: string; settled: boolean }
  | {
      kind: 'tool';
      id: string;
      name: string;
      args: unknown;
      index: number;
      count: number;
      state: 'running' | 'ok' | 'error';
      error?: ProtocolError;
    }
  | { kind: 'notice'; id: string; level: 'info' | 'warning' | 'error'; text: string };

export type ApprovalChoice = 'deny' | 'allow';

export type UserQuestionState = {
  requestId: string;
  question: string;
  options: UserQuestionOption[];
  allowFreeText: boolean;
  selectedIndex: number;
};

export type State = {
  status: Status;
  sessionId: string | null;
  workspace: string;
  model: string;
  provider: string;
  permissionPreset: PermissionPreset;
  entries: Entry[];
  /** Latest MCP startup status; cleared once Runtime initialization finishes. */
  mcpStatus: string | null;
  approval: {
    requestId: string;
    command: string;
    kind?: 'shell' | 'mcp';
    server?: string;
    toolName?: string;
    choice: ApprovalChoice;
  } | null;
  question: UserQuestionState | null;
  /** Open session picker, filled once the bridge answers `list_sessions`. */
  sessions: { list: SessionSummary[]; selectedIndex: number } | null;
  settings: SettingsSnapshot | null;
  turnId: string | null;
  usage: Usage | null;
  contextWindow: ContextWindow | null;
  plan: PlanSnapshot | null;
  pendingSteers: number;
};

export type Action =
  | { type: 'message'; message: Incoming }
  | { type: 'submit'; turnId: string; text: string }
  | { type: 'steer_submitted' }
  | { type: 'approval_choice'; choice: ApprovalChoice }
  | { type: 'approval_resolved' }
  | { type: 'question_choice'; selectedIndex: number }
  | { type: 'question_resolved' }
  | { type: 'sessions_opened' }
  | { type: 'sessions_choice'; selectedIndex: number }
  | { type: 'sessions_closed' }
  | { type: 'settings_closed' }
  /** Drops everything the previous runtime reported, for a new Session. */
  | { type: 'restart' }
  | { type: 'cancelling' }
  | { type: 'exited'; code: number | null };

export const initialState: State = {
  status: 'opening',
  sessionId: null,
  workspace: '',
  model: '',
  provider: '',
  permissionPreset: 'ask_for_approval',
  entries: [],
  mcpStatus: null,
  approval: null,
  question: null,
  sessions: null,
  settings: null,
  turnId: null,
  usage: null,
  contextWindow: null,
  plan: null,
  pendingSteers: 0,
};

let entryCounter = 0;
function nextId(prefix: string): string {
  entryCounter += 1;
  return `${prefix}-${entryCounter}`;
}

/** Appends to the open assistant entry, creating it on the first delta. */
function appendDelta(entries: Entry[], text: string): Entry[] {
  const last = entries[entries.length - 1];
  if (last && last.kind === 'assistant' && !last.settled) {
    return [...entries.slice(0, -1), { ...last, text: last.text + text }];
  }
  return [...entries, { kind: 'assistant', id: nextId('assistant'), text, settled: false }];
}

/**
 * Appends to the open reasoning entry, creating it on the first delta.
 *
 * A model call streams reasoning as many small deltas, so they are merged
 * into one entry the same way assistant text is.
 */
function appendReasoning(entries: Entry[], text: string): Entry[] {
  const last = entries[entries.length - 1];
  if (last && last.kind === 'reasoning' && !last.settled) {
    return [...entries.slice(0, -1), { ...last, text: last.text + text }];
  }
  return [
    ...entries,
    { kind: 'reasoning', id: nextId('reasoning'), text, settled: false },
  ];
}

/**
 * Closes every reasoning entry that is still open.
 *
 * Only one can be open at a time: deltas extend the trailing entry, and any
 * other action closes it. Ink writes the entries before the open one once
 * and never again, so they must stop changing before they leave the live
 * region.
 */
function settleReasoning(state: State): State {
  const open = state.entries.some((entry) => entry.kind === 'reasoning' && !entry.settled);
  if (!open) return state;
  return {
    ...state,
    entries: state.entries.map((entry) =>
      entry.kind === 'reasoning' && !entry.settled ? { ...entry, settled: true } : entry,
    ),
  };
}

function settleAssistant(entries: Entry[], content: string, timestamp_utc?: string): Entry[] {
  const last = entries[entries.length - 1];
  if (last && last.kind === 'assistant' && !last.settled) {
    return [...entries.slice(0, -1), { ...last, text: content, timestamp_utc, settled: true }];
  }
  return [...entries, { kind: 'assistant', id: nextId('assistant'), text: content, timestamp_utc, settled: true }];
}

function resolveTool(
  entries: Entry[],
  toolCallId: string,
  state: 'ok' | 'error',
  error: ProtocolError | null,
): Entry[] {
  return entries.map((entry) =>
    entry.kind === 'tool' && entry.id === toolCallId
      ? { ...entry, state, ...(error ? { error } : {}) }
      : entry,
  );
}

type ToolOutcome = { ok: boolean; error?: ProtocolError };

/** Stored Tool results keyed by the call each one answers. */
function toolOutcomes(items: SessionItem[]): Map<string, ToolOutcome> {
  const outcomes = new Map<string, ToolOutcome>();
  for (const item of items) {
    if (
      item.role !== 'tool'
      || item.tool_call_id === undefined
      || typeof item.content !== 'string'
    ) {
      continue;
    }
    const parsed = JSON.parse(item.content) as { ok?: boolean; error?: ProtocolError };
    outcomes.set(item.tool_call_id, {
      ok: parsed.ok !== false,
      ...(parsed.error ? { error: parsed.error } : {}),
    });
  }
  return outcomes;
}

function itemText(content: SessionItem['content']): string {
  if (typeof content === 'string') return content;
  if (content === null) return '';
  const texts: string[] = [];
  for (const part of content) {
    if (part.type === 'text') texts.push(part.text);
  }
  return texts.join('\n');
}

function itemImagePaths(content: SessionItem['content']): string[] {
  if (content === null || typeof content === 'string') return [];
  const paths: string[] = [];
  for (const part of content) {
    if (part.type === 'image') paths.push(part.path);
  }
  return paths;
}

/**
 * Rebuilds transcript entries from a stored conversation.
 *
 * A resumed Session is replayed from the items the model itself saw, so the
 * entries mirror what the live protocol messages would have produced: one
 * user entry per turn, reasoning ahead of the answer, and one entry per Tool
 * call whose outcome comes from the item answering it.
 */
function historyEntries(items: SessionItem[]): Entry[] {
  const outcomes = toolOutcomes(items);
  const entries: Entry[] = [];

  for (const item of items) {
    // Tool results only carry an outcome; the call entry shows it.
    if (item.role === 'tool') continue;

    if (item.origin === 'tool_media') {
      const paths = itemImagePaths(item.content);
      if (paths.length > 0) {
        entries.push({
          kind: 'notice',
          id: nextId('notice'),
          level: 'info',
          text: `Viewing ${paths.length} image(s): ${paths.join(', ')}`,
        });
      }
      continue;
    }
    if (item.origin === 'job_result') continue;

    if (item.reasoning) {
      entries.push({
        kind: 'reasoning',
        id: nextId('reasoning'),
        text: item.reasoning,
        settled: true,
      });
    }

    const text = itemText(item.content);
    if (item.role === 'user') {
      if (text) entries.push({ kind: 'user', id: nextId('user'), text });
      continue;
    }

    if (text) {
      entries.push({
        kind: 'assistant',
        id: nextId('assistant'),
        text,
        settled: true,
        ...(item.timestamp_utc ? { timestamp_utc: item.timestamp_utc } : {}),
      });
    }

    const calls = item.tool_calls ?? [];
    calls.forEach((call, index) => {
      if (call.name === 'update_plan') return;
      const outcome = outcomes.get(call.id);
      entries.push({
        kind: 'tool',
        id: call.id,
        name: call.name,
        args: call.arguments,
        index: index + 1,
        count: calls.length,
        // A call the journal never settled was interrupted, and nothing
        // in a replay is still running.
        state: outcome === undefined || !outcome.ok ? 'error' : 'ok',
        ...(outcome?.error ? { error: outcome.error } : {}),
      });
    });
  }

  return entries;
}

export function reducer(state: State, action: Action): State {
  const next = reduceAction(state, action);
  // Reasoning deltas extend the open entry; any other action means the model
  // moved on to content, a tool call, or the end of the turn.
  const streaming =
    action.type === 'message' && action.message.type === 'reasoning_delta';
  return streaming ? next : settleReasoning(next);
}

/** The state transition for a single action. */
function reduceAction(state: State, action: Action): State {
  switch (action.type) {
    case 'submit':
      return {
        ...state,
        status: 'streaming',
        turnId: action.turnId,
        usage: null,
        entries: [
          ...state.entries,
          { kind: 'user', id: nextId('user'), text: action.text },
        ],
      };

    case 'steer_submitted':
      return { ...state, pendingSteers: state.pendingSteers + 1 };

    case 'approval_choice':
      return state.approval === null
        ? state
        : { ...state, approval: { ...state.approval, choice: action.choice } };

    case 'approval_resolved':
      // The runtime resumes the turn once the answer is sent.
      return { ...state, status: 'running', approval: null };

    case 'question_choice':
      return state.question === null
        ? state
        : {
            ...state,
            question: { ...state.question, selectedIndex: action.selectedIndex },
          };

    case 'question_resolved':
      return { ...state, status: 'running', question: null };

    case 'sessions_opened':
      return { ...state, sessions: { list: [], selectedIndex: 0 } };

    case 'sessions_choice':
      return state.sessions === null
        ? state
        : {
            ...state,
            sessions: { ...state.sessions, selectedIndex: action.selectedIndex },
          };

    case 'sessions_closed':
      return { ...state, sessions: null };

    case 'settings_closed':
      return { ...state, settings: null };

    case 'restart':
      // The next runtime reports its own workspace, model and preset.
      return initialState;

    case 'cancelling':
      return state.status === 'idle' || state.status === 'fatal'
        ? state
        : { ...state, status: 'cancelling', approval: null, question: null };

    case 'exited':
      return {
        ...state,
        status: 'fatal',
        entries: [
          ...state.entries,
          {
            kind: 'notice',
            id: nextId('notice'),
            level: 'error',
            text: `Agent process exited (code ${action.code ?? 'unknown'}).`,
          },
        ],
      };

    case 'message':
      return applyMessage(state, action.message);
  }
}

function applyMessage(state: State, message: Incoming): State {
  switch (message.type) {
    case 'runtime_state': {
      const startupNotices: Entry[] = (message.runtime_warnings ?? []).map((text) => ({
        kind: 'notice',
        id: nextId('notice'),
        level: 'warning',
        text,
      }));
      const status: Status = message.phase === 'starting'
        ? 'starting'
        : message.phase === 'running'
          ? 'running'
          : message.phase === 'waiting_approval'
            ? 'awaiting_approval'
            : message.phase === 'waiting_user'
              ? 'awaiting_user'
              : message.phase === 'failed'
                ? 'runtime_failed'
                : 'idle';
      return {
        ...state,
        status,
        turnId: message.turn_id,
        permissionPreset: message.permission_preset,
        contextWindow: message.context_window ?? state.contextWindow,
        plan: message.plan,
        mcpStatus: message.phase === 'starting' ? state.mcpStatus : null,
        entries: [...state.entries, ...startupNotices],
      };
    }

    // Attachment bookkeeping belongs to the GUI server; the bridge
    // never sends it to this frontend.
    case 'attachment_replaced':
      return state;

    case 'mcp_server_status':
      return {
        ...state,
        mcpStatus: `MCP ${message.server}: ${message.status}${message.tool_count === undefined ? '' : ` (${message.tool_count} tools)`}${message.error === undefined ? '' : ` — ${message.error}`}`,
      };

    case 'session_ready':
      return {
        ...state,
        sessionId: message.session_id,
        workspace: message.workspace,
        provider: message.provider,
        model: message.model,
        permissionPreset: message.permission_preset,
        entries: message.resumed
          ? [
              ...state.entries,
              {
                kind: 'notice',
                id: nextId('notice'),
                level: 'info',
                text: `Resumed session with ${message.message_count} message(s).`,
              },
            ]
          : state.entries,
      };

    case 'assistant_delta':
      return {
        ...state,
        status: state.status === 'cancelling' ? state.status : 'streaming',
        entries: appendDelta(state.entries, message.text),
      };
    case 'reasoning_delta':
      return {
        ...state,
        entries: appendReasoning(state.entries, message.text),
      };

    case 'assistant_message':
      return { ...state, entries: settleAssistant(state.entries, message.content, message.timestamp_utc) };

    case 'tool_batch_started':
      return { ...state, status: state.status === 'cancelling' ? state.status : 'running' };

    case 'tool_call':
      if (message.tool_call.name === 'update_plan') return state;
      return {
        ...state,
        status: state.status === 'cancelling' ? state.status : 'running',
        entries: [
          ...state.entries,
          {
            kind: 'tool',
            id: message.tool_call.id,
            name: message.tool_call.name,
            args: message.tool_call.arguments,
            index: message.tool_index,
            count: message.tool_count,
            state: 'running',
          },
        ],
      };

    case 'context_window':
      return { ...state, contextWindow: message };

    case 'tool_result':
      return {
        ...state,
        entries: resolveTool(
          state.entries,
          message.tool_call_id,
          message.ok ? 'ok' : 'error',
          message.error,
        ),
      };

    case 'tool_media':
      // A terminal cannot show the image, so the user is told which
      // ones the agent is now looking at.
      return {
        ...state,
        entries: [
          ...state.entries,
          {
            kind: 'notice',
            id: nextId('notice'),
            level: 'info',
            text: `Viewing ${message.attachments.length} image(s): ${message.attachments
              .map((attachment) => attachment.path)
              .join(', ')}`,
          },
        ],
      };

    case 'job_status':
      return {
        ...state,
        entries: [
          ...state.entries,
          {
            kind: 'notice',
            id: nextId('notice'),
            level: message.status === 'failed' ? 'error' : 'info',
            text: `Background ${message.kind} ${message.job_id}: ${message.status}.`,
          },
        ],
      };

    case 'plan_updated':
      return { ...state, plan: message.plan };

    case 'user_steer_received':
      return state;

    case 'user_steer_applied':
      return {
        ...state,
        pendingSteers: Math.max(0, state.pendingSteers - 1),
        entries: [
          ...state.entries,
          { kind: 'user', id: nextId('user'), text: message.text },
        ],
      };

    case 'user_steer_rejected':
      return {
        ...state,
        pendingSteers: Math.max(0, state.pendingSteers - 1),
        entries: [
          ...state.entries,
          {
            kind: 'notice',
            id: nextId('notice'),
            level: 'info',
            text: 'Steering arrived after the turn finished and was not applied.',
          },
        ],
      };

    case 'approval_request':
      return {
        ...state,
        status: 'awaiting_approval',
        approval: {
          requestId: message.request_id,
          command: message.command,
          kind: message.kind,
          server: message.server,
          toolName: message.tool_name,
          choice: 'allow',
        },
      };

    case 'permission_changed':
      const permissionLabel =
        message.preset === 'full_access'
          ? 'Full Access'
          : message.preset === 'workspace_access'
            ? 'Workspace Access'
            : 'Ask for approval';
      return {
        ...state,
        permissionPreset: message.preset,
        entries: [
          ...state.entries,
          {
            kind: 'notice',
            id: nextId('notice'),
            level: 'info',
            text: `Permissions changed to ${permissionLabel}.`,
          },
        ],
      };

    case 'provider_changed':
      return { ...state, provider: message.provider, model: message.model };

    case 'settings_snapshot':
      return { ...state, settings: message.settings };

    case 'settings_update_failed':
      return {
        ...state,
        entries: [...state.entries, { kind: 'notice', id: nextId('notice'), level: 'error', text: message.error.message }],
      };

    case 'workspace_changed':
      return state;

    case 'sessions_listed':
      return state.sessions === null
        ? state
        : { ...state, sessions: { list: message.sessions, selectedIndex: 0 } };

    case 'session_items':
      // Appended rather than prepended: the restart already emptied the
      // transcript, so the resumed notice stays above the stored turns.
      return { ...state, entries: [...state.entries, ...historyEntries(message.items)] };

    case 'user_question': {
      const recommendedIndex = message.options.findIndex((option) => option.recommended);
      return {
        ...state,
        status: 'awaiting_user',
        question: {
          requestId: message.request_id,
          question: message.question,
          options: message.options,
          allowFreeText: message.allow_free_text,
          selectedIndex: recommendedIndex >= 0 ? recommendedIndex : 0,
        },
      };
    }

    case 'turn_completed':
      return { ...state, status: 'idle', turnId: null, approval: null, question: null, pendingSteers: 0, usage: message.usage };

    case 'turn_cancelled':
      return {
        ...state,
        status: 'idle',
        turnId: null,
        approval: null,
        question: null,
        pendingSteers: 0,
        entries: [
          ...state.entries,
          {
            kind: 'notice',
            id: nextId('notice'),
            level: 'info',
            text: 'Cancelled.',
          },
        ],
      };

    case 'turn_failed':
      return {
        ...state,
        status: 'idle',
        turnId: null,
        approval: null,
        question: null,
        pendingSteers: 0,
        entries: [
          ...state.entries,
          {
            kind: 'notice',
            id: nextId('notice'),
            level: 'error',
            text: `${message.error.type}: ${message.error.message}`,
          },
        ],
      };

    case 'fatal':
      return {
        ...state,
        status: 'fatal',
        entries: [
          ...state.entries,
          {
            kind: 'notice',
            id: nextId('notice'),
            level: 'error',
            text: `${message.error.type}: ${message.error.message}`,
          },
        ],
      };
  }
}
