import type { ThreadMessageLike } from "@assistant-ui/react";
import type { Incoming, Usage } from "@nosis/protocol";
import { attachmentUrl, type SessionItem } from "./api";

/** A transcript item, plus the streaming state the live turn needs. */
export type TranscriptItem = SessionItem & { streaming?: boolean };

export type Notice = { level: "info" | "error"; text: string };

/** What a protocol message changes about the view. */
export type Applied = {
  items: TranscriptItem[];
  notice?: Notice;
  approval?: {
    requestId: string;
    command: string;
    kind?: 'shell' | 'mcp';
    server?: string;
    toolName?: string;
  } | null;
  /** Tokens the finished turn used, or null when the model reported none. */
  usage?: Usage | null;
  /** Set once the turn ended, so the caller can reload the session. */
  finished?: boolean;
};

type ToolOutcome = { ok: boolean; error?: { type: string; message: string } };

/** Messages that prove the model has started handling the persisted turn. */
export function isTurnActivity(message: Incoming): boolean {
  return message.type === "assistant_delta"
    || message.type === "reasoning_delta"
    || message.type === "tool_batch_started"
    || message.type === "assistant_message";
}

/**
 * Folds a bridge message into the transcript.
 *
 * Mirrors interfaces/tui/src/state.ts: text arrives as deltas and is
 * settled by the matching assistant_message.
 */
export function applyMessage(
  items: TranscriptItem[],
  message: Incoming,
): Applied {
  switch (message.type) {
    case "mcp_server_status":
      return {
        items,
        notice: {
          level: "info",
          text: `MCP ${message.server}: ${message.status}${message.tool_count === undefined ? "" : ` (${message.tool_count} tools)`}${message.error === undefined ? "" : ` — ${message.error}`}`,
        },
      };

    case "assistant_delta":
      return { items: appendDelta(items, message.text) };
    case "reasoning_delta":
      return { items: appendReasoning(items, message.text) };

    case "assistant_message":
      return { items: settleAssistant(items, message.content, message.timestamp_utc) };

    case "context_archived":
      return { items, notice: { level: "info", text: `上下文已压缩（checkpoint ${message.checkpoint_number}）。` } };

    case "tool_batch_started":
      return {
        items: [
          ...items,
          { role: "assistant", content: null, tool_calls: message.tool_calls },
        ],
      };

    case "tool_result":
      return {
        items: [
          ...items,
          {
            role: "tool",
            tool_call_id: message.tool_call_id,
            // The protocol omits tool output; it is filled in from the
            // stored session once the turn completes.
            content: JSON.stringify(
              message.ok
                ? ({ ok: true } satisfies ToolOutcome)
                : ({ ok: false, error: message.error ?? undefined }),
            ),
          },
        ],
      };

    case "approval_request":
      return {
        items,
        approval: {
          requestId: message.request_id,
          command: message.command,
          kind: message.kind,
          server: message.server,
          toolName: message.tool_name,
        },
      };

    case "turn_completed":
      return { items, approval: null, finished: true, usage: message.usage };

    case "turn_cancelled":
      return {
        items: settle(items),
        approval: null,
        finished: true,
        notice: {
          level: "info",
          text: "已取消。",
        },
      };

    case "turn_failed":
    case "fatal":
      return {
        items: settle(items),
        approval: null,
        finished: true,
        notice: {
          level: "error",
          text: `${message.error.type}: ${message.error.message}`,
        },
      };

    // 'ready' and 'tool_call' carry no transcript change: the session id
    // is read from the REST API, and the batch already listed the calls.
    default:
      return { items };
  }
}

function appendDelta(items: TranscriptItem[], text: string): TranscriptItem[] {
  const last = items[items.length - 1];
  if (last?.role === "assistant" && last.streaming) {
    return [
      ...items.slice(0, -1),
      { ...last, content: (last.content ?? "") + text },
    ];
  }
  return [...items, { role: "assistant", content: text, streaming: true }];
}

function appendReasoning(items: TranscriptItem[], text: string): TranscriptItem[] {
  const last = items[items.length - 1];
  if (last?.role === "assistant" && last.streaming && last.reasoning !== undefined) {
    return [...items.slice(0, -1), { ...last, reasoning: (last.reasoning ?? "") + text }];
  }
  return [...items, { role: "assistant", content: null, reasoning: text, streaming: true }];
}

function settleAssistant(
  items: TranscriptItem[],
  content: string,
  timestamp_utc?: string,
): TranscriptItem[] {
  const last = items[items.length - 1];
  if (last?.role === "assistant" && last.streaming) {
    return [...items.slice(0, -1), { ...last, content, timestamp_utc, streaming: false }];
  }
  return [...items, { role: "assistant", content, timestamp_utc }];
}

/** Closes any open assistant text when a turn ends without settling it. */
function settle(items: TranscriptItem[]): TranscriptItem[] {
  const last = items[items.length - 1];
  if (last?.role !== "assistant" || !last.streaming) return items;
  return [...items.slice(0, -1), { ...last, streaming: false }];
}

/** One rendered part of a message. */
type Part = Exclude<ThreadMessageLike["content"], string>[number];

/** A message whose parts are still being appended, item by item. */
type OpenMessage = {
  id: string;
  role: ThreadMessageLike["role"];
  content: Part[];
  createdAt?: Date;
};

/** The parts one transcript item contributes, in the order the model emitted them. */
function itemParts(
  item: TranscriptItem,
  results: Map<string, ToolOutcome>,
  sessionId?: string,
): Part[] {
  const parts: Part[] = [];
  // The model reasons before it answers, so reasoning comes first.
  if (item.reasoning) parts.push({ type: "reasoning", text: item.reasoning });
  if (typeof item.content === "string" && item.content) parts.push({ type: "text", text: item.content });
  if (Array.isArray(item.content)) {
    for (const part of item.content) {
      if (part.type === "text") parts.push({ type: "text", text: part.text });
      else parts.push({ type: "image", image: attachmentUrl(part.path, sessionId) } as unknown as Part);
    }
  }
  for (const call of item.tool_calls ?? []) {
    parts.push({
      type: "tool-call",
      toolCallId: call.id,
      toolName: call.name,
      args: call.arguments,
      argsText: JSON.stringify(call.arguments),
      result: results.get(call.id),
      isError: results.get(call.id)?.ok === false,
    });
  }
  return parts;
}

/**
 * Groups stored items into the messages assistant-ui renders.
 *
 * One turn writes several assistant items — text, tool calls, then more
 * text — and they are a single reply, so consecutive assistant items
 * accumulate into one message and share one avatar. A user item starts
 * the next group.
 */
export function toMessages(items: TranscriptItem[], sessionId?: string): ThreadMessageLike[] {
  const results = new Map(
    items
      .filter(
        (item): item is TranscriptItem & { tool_call_id: string; content: string } =>
        item.role === "tool" && typeof item.content === "string" && Boolean(item.content),
      )
      .map((item) => [item.tool_call_id, JSON.parse(item.content)]),
  );
  const messages: ThreadMessageLike[] = [];
  let open: OpenMessage | null = null;

  items.forEach((item, index) => {
    if (item.role === "tool") return;
    // The session id travels into the URL builder, so an image is
    // addressed against the session's own workspace from the start.
    const parts = itemParts(item, results, sessionId);

    // Images a tool loaded arrive in a user-role item because that is the
    // only message kind that can carry them. They belong to the reply the
    // agent was composing, so they join it instead of opening a bubble
    // that looks like the person spoke. The text beside them only tells
    // the model where they came from, so it is left out here.
    if (item.role === "user" && item.origin === "tool_media") {
      if (open) open.content.push(...parts.filter((part) => (part as any).type === "image"));
      return;
    }

    if (item.role === "assistant" && open) {
      open.content.push(...parts);
      // The timestamp belongs to the finished turn, so the newest item wins.
      if (item.timestamp_utc) open.createdAt = new Date(item.timestamp_utc);
      return;
    }

    const message: OpenMessage = {
      id: String(index),
      role: item.role,
      content: parts,
      createdAt: item.timestamp_utc
        ? new Date(item.timestamp_utc)
        : undefined,
    };
    open = item.role === "assistant" ? message : null;
    messages.push(message);
  });

  return messages;
}
