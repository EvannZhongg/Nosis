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

export type Incoming =
  | {
      type: 'runtime_state';
      running: boolean;
      provider: string | null;
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
    }
  | { type: 'attachment_replaced'; running: boolean }
  | {
      type: 'ready';
      session_id: string;
      workspace: string;
      model: string;
      resumed: boolean;
      message_count: number;
      skill_warnings?: string[];
    }
  | { type: 'assistant_delta'; turn_id: string; text: string; model_call_index: number }
  | { type: 'reasoning_delta'; turn_id: string; text: string; model_call_index: number }
  | { type: 'context_archived'; turn_id: string; checkpoint_number: number }
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
      type: 'approval_request';
      turn_id: string | null;
      request_id: string;
      command: string;
      kind?: 'shell' | 'mcp';
      server?: string;
      tool_name?: string;
    }
  | UserQuestion
  | { type: 'mcp_server_status'; server: string; status: string; tool_count?: number; error?: string }
  | { type: 'turn_completed'; turn_id: string; usage: Usage | null }
  | { type: 'turn_cancelled'; turn_id: string }
  | { type: 'turn_failed'; turn_id: string; error: ProtocolError }
  | { type: 'fatal'; error: ProtocolError };

export type Outgoing =
  | {
      type: 'start';
      workspace: string;
      session_id: string | null;
      provider_config_path: string;
      agent_config_path: string;
      attach_only?: boolean;
      after_event?: number;
      attachment_id?: string;
      takeover?: boolean;
      // Omitted to use the provider selected in the configuration file.
      provider?: string;
    }
  | { type: 'user_turn'; turn_id: string; text: string; attachments?: ImageAttachment[] }
  | { type: 'approval_response'; request_id: string; approved: boolean }
  | { type: 'user_question_response'; request_id: string; option_id?: string; text?: string }
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
