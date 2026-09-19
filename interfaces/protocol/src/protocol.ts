// Wire protocol shared with interfaces/bridge. Keep in sync with
// interfaces/bridge/protocol.py.

export type JSONValue =
  | string
  | number
  | boolean
  | null
  | readonly JSONValue[]
  | { readonly [key: string]: JSONValue };

export type ToolCall = {
  id: string;
  name: string;
  arguments: { readonly [key: string]: JSONValue };
};

export type ImageAttachment = { type: 'image'; path: string; mime_type: string };

export type ProtocolError = {
  type: string;
  message: string;
};

export type Usage = {
  input_tokens: number | null;
  output_tokens: number | null;
  total_tokens: number | null;
};

export type ContextWindow = {
  input_tokens: number;
  max_input_tokens: number;
  max_context_tokens: number;
  output_reserve_tokens: number;
  compression_threshold: number;
  compression_count: number;
};

export type PlanStepStatus = 'pending' | 'in_progress' | 'completed' | 'blocked';

export type PlanStep = {
  id: string;
  title: string;
  status: PlanStepStatus;
  outcome?: string;
};

export type PlanSnapshot = {
  plan_id: string;
  goal: string;
  revision: number;
  steps: PlanStep[];
};

export type PermissionPreset = 'ask_for_approval' | 'full_access';
export type RuntimePhase =
  | 'inactive'
  | 'starting'
  | 'idle'
  | 'running'
  | 'waiting_approval'
  | 'waiting_user'
  | 'failed';

export function runtimeIsActive(phase: RuntimePhase): boolean {
  return phase === 'starting'
    || phase === 'running'
    || phase === 'waiting_approval'
    || phase === 'waiting_user';
}

export type SessionSummary = {
  session_id: string;
  title: string;
};

export type SessionContentPart =
  | { type: 'text'; text: string }
  | { type: 'image'; path: string; mime_type: string };

/**
 * One stored conversation item, as the Session journal holds it.
 *
 * A turn is several items: the user's message, an assistant step per model
 * call, and one item per Tool result answering that step's calls.
 */
export type SessionItem = {
  role: 'user' | 'assistant' | 'tool';
  content: string | SessionContentPart[] | null;
  timestamp_utc?: string;
  tool_calls?: ToolCall[];
  tool_call_id?: string;
  reasoning?: string | null;
  // "tool_media" marks a user-role item the runtime synthesized to carry
  // images a Tool loaded; the person never wrote it.
  origin?: 'conversation' | 'tool_media' | 'job_result';
};

export type UserQuestionOption = {
  id: string;
  label: string;
  description?: string;
  recommended?: boolean;
};

export type UserQuestion = {
  type: 'user_question';
  turn_id: string | null;
  request_id: string;
  question: string;
  options: UserQuestionOption[];
  allow_free_text: boolean;
};

export type Incoming = (
  | {
      /**
       * Authoritative execution snapshot. The first snapshot after
       * open_session is the synchronization barrier before commands may be
       * dispatched; session_ready only describes the Session control plane.
       */
      type: 'runtime_state';
      phase: RuntimePhase;
      turn_id: string | null;
      provider: string | null;
      permission_preset: PermissionPreset;
      context_window: ContextWindow | null;
      event_sequence?: number;
      jobs: {
        job_id: string;
        kind: string;
        status: 'submitted' | 'running' | 'completed' | 'failed' | 'cancelled';
      }[];
      approval: {
        type: 'approval_request';
        turn_id: string | null;
        request_id: string;
        command: string;
        kind?: 'shell' | 'mcp';
        server?: string;
        tool_name?: string;
      } | null;
      question: UserQuestion | null;
      runtime_warnings?: string[];
      plan: PlanSnapshot | null;
    }
  | { type: 'attachment_replaced'; phase: RuntimePhase }
  | {
      type: 'session_ready';
      session_id: string;
      workspace: string;
      provider: string;
      model: string;
      resumed: boolean;
      message_count: number;
      permission_preset: PermissionPreset;
    }
  | { type: 'assistant_delta'; turn_id: string; text: string; model_call_index: number }
  | { type: 'reasoning_delta'; turn_id: string; text: string; model_call_index: number }
  | ({ type: 'context_window'; turn_id: string | null } & ContextWindow)
  | {
      type: 'assistant_message';
      turn_id: string;
      content: string;
      timestamp_utc: string;
      model_call_index: number;
    }
  | {
      type: 'tool_batch_started';
      turn_id: string;
      model_call_index: number;
      tool_calls: ToolCall[];
    }
  | {
      type: 'tool_call';
      turn_id: string;
      tool_call: ToolCall;
      tool_index: number;
      tool_count: number;
    }
  | {
      type: 'tool_result';
      turn_id: string;
      tool_call_id: string;
      name: string;
      ok: boolean;
      error: ProtocolError | null;
      tool_index: number;
      tool_count: number;
    }
  | {
      type: 'tool_media';
      turn_id: string;
      attachments: ImageAttachment[];
    }
  | {
      type: 'job_status';
      turn_id: string;
      job_id: string;
      kind: string;
      status: 'submitted' | 'running' | 'completed' | 'failed' | 'cancelled';
    }
  | { type: 'user_steer_received'; turn_id: string | null; steer_id: string; text: string }
  | { type: 'user_steer_rejected'; turn_id: string | null; steer_id: string; text: string }
  | {
      type: 'user_steer_applied';
      turn_id: string;
      steer_id: string;
      text: string;
      timestamp_utc: string;
    }
  | {
      type: 'approval_request';
      turn_id: string | null;
      request_id: string;
      command: string;
      kind?: 'shell' | 'mcp';
      server?: string;
      tool_name?: string;
    }
  | { type: 'permission_changed'; preset: PermissionPreset }
  | { type: 'provider_changed'; provider: string; model: string }
  | { type: 'workspace_changed'; workspace: string }
  | { type: 'sessions_listed'; sessions: SessionSummary[] }
  | { type: 'session_items'; items: SessionItem[] }
  | { type: 'plan_updated'; plan: PlanSnapshot }
  | UserQuestion
  | { type: 'mcp_server_status'; server: string; status: string; tool_count?: number; error?: string }
  | { type: 'turn_completed'; turn_id: string; usage: Usage | null }
  | { type: 'turn_cancelled'; turn_id: string }
  | { type: 'turn_failed'; turn_id: string; error: ProtocolError }
  | { type: 'fatal'; error: ProtocolError }
) & { event_sequence?: number };

export type Outgoing =
  | {
      type: 'open_session';
      workspace: string;
      session_id: string | null;
      attach_only?: boolean;
      after_event?: number;
      attachment_id?: string;
      takeover?: boolean;
      // Omitted to use the provider selected in the configuration file.
      provider?: string;
    }
  | { type: 'user_turn'; turn_id: string; text: string; attachments?: ImageAttachment[] }
  | { type: 'user_steer'; turn_id: string; steer_id: string; text: string }
  | { type: 'approval_response'; request_id: string; approved: boolean }
  | { type: 'permission_set'; preset: PermissionPreset }
  | { type: 'provider_set'; provider: string }
  | { type: 'workspace_set'; workspace: string }
  | { type: 'cancel'; turn_id: string }
  | { type: 'user_question_response'; request_id: string; option_id?: string; text?: string }
  // Ask for the Workspace's stored Sessions, and for the one just started.
  | { type: 'list_sessions' }
  | { type: 'load_session' }
  | { type: 'shutdown' };

/**
 * Splits a byte stream into protocol messages.
 *
 * Chunk boundaries do not respect line boundaries, so a partial line is
 * held back until its newline arrives.
 */
export class MessageDecoder {
  private buffer = '';

  push(chunk: string): Incoming[] {
    this.buffer += chunk;
    const lines = this.buffer.split('\n');
    this.buffer = lines.pop() ?? '';

    const messages: Incoming[] = [];
    for (const line of lines) {
      const trimmed = line.trim();
      if (trimmed === '') continue;
      messages.push(JSON.parse(trimmed) as Incoming);
    }
    return messages;
  }
}

export function formatArguments(value: unknown): string {
  if (value === null || value === undefined) return '';
  if (typeof value !== 'object') return String(value);
  const entries = Object.entries(value as Record<string, unknown>);
  if (entries.length === 0) return '';
  return entries
    .map(([key, item]) => `${key}=${typeof item === 'string' ? item : JSON.stringify(item)}`)
    .join(' ');
}
