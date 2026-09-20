import type { ThreadMessageLike } from "@assistant-ui/react";
import type { ContextWindow, Incoming, PermissionPreset, UserQuestion } from "@nosis/protocol";
import { attachmentUrl, type SessionItem } from "./api";

/** A transcript item, plus the streaming state the live turn needs. */
export type TranscriptItem = SessionItem & { streaming?: boolean };

export type Feedback =
  | { kind: "toast"; level: "info"; text: string }
  | { kind: "alert"; id: string; level: "warning" | "error"; text: string };

/** What a protocol message changes about the view. */
export type Applied = {
  items: TranscriptItem[];
  feedback?: Feedback;
  contextWindow?: ContextWindow;
  approval?: {
    requestId: string;
    command: string;
    kind?: 'shell' | 'mcp';
    server?: string;
    toolName?: string;
  } | null;
  question?: UserQuestion | null;
  permissionPreset?: PermissionPreset;
  /** Set once the turn ended, so the caller can reload the session. */
  finished?: boolean;
};

type ToolOutcome = { ok: boolean; error?: { type: string; message: string } };

export const TURN_PROCESS_GROUP = "group-turn-process";

/** Messages that prove the model has started handling the persisted turn. */
export function isTurnActivity(message: Incoming): boolean {
  return message.type === "assistant_delta"
    || message.type === "reasoning_delta"
    || message.type === "tool_batch_started"
    || message.type === "assistant_message"
    || message.type === "job_status";
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
    case "session_ready":
      return {
        items,
        permissionPreset: message.permission_preset,
      };

    case "runtime_state":
      return {
        items,
        permissionPreset: message.permission_preset,
        contextWindow: message.context_window ?? undefined,
        feedback: message.runtime_warnings?.length
          ? {
              kind: "alert",
              id: "runtime-warnings",
              level: "warning",
              text: message.runtime_warnings.join("\n"),
            }
          : undefined,
      };

    case "mcp_server_status":
      return message.status === "unavailable" || message.error
        ? {
            items,
            feedback: {
              kind: "alert",
              id: `mcp:${message.server}`,
              level: "error",
              text: `MCP ${message.server} 不可用${message.error === undefined ? "" : `：${message.error}`}`,
            },
          }
        : { items };

    case "assistant_delta":
      return { items: appendDelta(items, message.text) };
    case "reasoning_delta":
      return { items: appendReasoning(items, message.text) };

    case "assistant_message":
      return { items: settleAssistant(items, message.content, message.timestamp_utc) };

    case "context_window":
      return { items, contextWindow: message };

    case "tool_batch_started": {
      const visibleCalls = message.tool_calls.filter((call) => call.name !== "update_plan");
      if (visibleCalls.length === 0) return { items };
      return {
        items: [
          ...items,
          { role: "assistant", content: null, tool_calls: visibleCalls },
        ],
      };
    }

    case "tool_result":
      if (message.name === "update_plan") return { items };
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

    case "job_status":
      return { items };

    case "user_steer_applied":
      return {
        items: [
          ...items,
          {
            role: "user",
            content: message.text,
            timestamp_utc: message.timestamp_utc,
          },
        ],
      };

    case "user_steer_received":
      return { items };

    case "user_steer_rejected":
      return {
        items,
        feedback: {
          kind: "toast",
          level: "info",
          text: "这条引导消息到达时本轮已结束，未写入上下文。",
        },
      };

    case "approval_request":
      return {
        items,
        question: null,
        approval: {
          requestId: message.request_id,
          command: message.command,
          kind: message.kind,
          server: message.server,
          toolName: message.tool_name,
        },
      };

    case "permission_changed":
      const permissionLabel = message.preset === "full_access"
        ? "完全访问"
        : message.preset === "workspace_access"
          ? "工作区访问"
          : "请求批准";
      return {
        items,
        permissionPreset: message.preset,
        feedback: {
          kind: "toast",
          level: "info",
          text: `权限模式已切换为${permissionLabel}。`,
        },
      };

    case "provider_changed":
      return {
        items,
        feedback: {
          kind: "toast",
          level: "info",
          text: `模型已切换为 ${message.model}。`,
        },
      };

    case "workspace_changed":
      return {
        items,
        feedback: {
          kind: "toast",
          level: "info",
          text: `工作区已切换为 ${message.workspace}`,
        },
      };

    case "user_question":
      return { items, approval: null, question: message };

    case "turn_completed":
      return { items, approval: null, question: null, finished: true };

    case "turn_cancelled":
      return {
        items: settle(items),
        approval: null,
        question: null,
        finished: true,
        feedback: {
          kind: "toast",
          level: "info",
          text: "已取消。",
        },
      };

    case "turn_failed":
    case "fatal":
      return {
        items: settle(items),
        approval: null,
        question: null,
        finished: true,
        feedback: {
          kind: "alert",
          id: "turn-error",
          level: "error",
          text: `${message.error.type}: ${message.error.message}`,
        },
      };

    // 'tool_call' carries no transcript change: the batch already listed it.
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
    if (call.name === "update_plan") continue;
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

/** Part indices that belong to a turn's collapsible reasoning/tool process. */
export function turnProcessPartIndexes(parts: readonly { type: string }[]): number[] {
  let processEnd = -1;
  for (let index = parts.length - 1; index >= 0; index -= 1) {
    if (parts[index]?.type !== "text") {
      processEnd = index;
      break;
    }
  }
  return parts.slice(0, processEnd + 1).map((_, index) => index);
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
    if (item.role === "user" && item.origin === "job_result") return;

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
