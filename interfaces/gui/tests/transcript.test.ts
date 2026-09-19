import { describe, expect, it } from "vitest";
import type { Incoming } from "@nosis/protocol";
import { applyMessage, isTurnActivity, toMessages, turnProcessPartIndexes, type TranscriptItem } from "../src/transcript";

const TOOL_CALL = { id: "call-1", name: "shell", arguments: { command: "ls" } };

describe("isTurnActivity", () => {
  it.each([
    { type: "assistant_delta", turn_id: "t1", text: "hi", model_call_index: 1 },
    { type: "reasoning_delta", turn_id: "t1", text: "thinking", model_call_index: 1 },
    { type: "tool_batch_started", turn_id: "t1", model_call_index: 1, tool_calls: [] },
    { type: "assistant_message", turn_id: "t1", content: "hi", timestamp_utc: "2026-09-16T00:00:00Z", model_call_index: 1 },
    { type: "job_status", turn_id: "t1", job_id: "job-1", kind: "shell", status: "running" },
  ] satisfies Incoming[])("recognizes $type", (message) => {
    expect(isTurnActivity(message)).toBe(true);
  });

  it("ignores lifecycle messages", () => {
    expect(isTurnActivity({
      type: "turn_completed",
      turn_id: "t1",
      usage: null,
    })).toBe(false);
  });

  it("keeps background job status out of global feedback", () => {
    const result = applyMessage([], {
      type: "job_status",
      turn_id: "t1",
      job_id: "job-1",
      kind: "subagent",
      status: "completed",
    });
    expect(result.feedback).toBeUndefined();
  });
});

/** Folds a sequence of bridge messages, as the socket callback does. */
function fold(messages: Incoming[], initial: TranscriptItem[] = []) {
  return messages.reduce(
    (state, message) => {
      const applied = applyMessage(state.items, message);
      return {
        items: applied.items,
        feedback: applied.feedback ?? state.feedback,
        approval:
          applied.approval !== undefined ? applied.approval : state.approval,
        question:
          applied.question !== undefined ? applied.question : state.question,
        finished: applied.finished ?? state.finished,
      };
    },
    {
      items: initial,
      feedback: undefined,
      approval: null,
      question: null,
      finished: false,
    } as ReturnType<typeof applyMessage> & { approval: unknown },
  );
}

describe("applyMessage", () => {
  it("keeps update_plan out of the ordinary tool transcript", () => {
    const messages = toMessages([
      {
        role: "assistant",
        content: null,
        tool_calls: [{ id: "plan-call", name: "update_plan", arguments: { goal: "Ship", steps: [] } }],
      },
      {
        role: "tool",
        tool_call_id: "plan-call",
        content: JSON.stringify({ ok: true, output: { revision: 1 } }),
      },
    ]);

    expect(messages[0]?.content).toEqual([]);
  });

  it("filters live update_plan calls and results from the transcript", () => {
    const { items } = fold([
      {
        type: "tool_batch_started",
        turn_id: "t1",
        model_call_index: 1,
        tool_calls: [
          { id: "plan-call", name: "update_plan", arguments: { goal: "Ship", steps: [] } },
        ],
      },
      {
        type: "tool_result",
        turn_id: "t1",
        tool_call_id: "plan-call",
        name: "update_plan",
        ok: true,
        error: null,
        tool_index: 1,
        tool_count: 1,
      },
    ]);

    expect(items).toEqual([]);
  });

  it("reports permission state from session readiness and later changes", () => {
    const ready = applyMessage([], {
      type: "session_ready",
      session_id: "s1",
      workspace: "/tmp/work",
      provider: "test",
      model: "test/model",
      resumed: false,
      message_count: 0,
      permission_preset: "ask_for_approval",
    });
    expect(ready.permissionPreset).toBe("ask_for_approval");

    const changed = applyMessage([], {
      type: "permission_changed",
      preset: "full_access",
    });
    expect(changed.permissionPreset).toBe("full_access");
  });

  it("appends steering only when the runtime applies it", () => {
    const applied = applyMessage([], {
      type: "user_steer_applied",
      turn_id: "t1",
      steer_id: "s1",
      text: "check tests first",
      timestamp_utc: "2026-09-16T10:00:00Z",
    });

    expect(applied.items).toEqual([
      {
        role: "user",
        content: "check tests first",
        timestamp_utc: "2026-09-16T10:00:00Z",
      },
    ]);
  });
  it("shows warnings from runtime initialization", () => {
    const { feedback } = applyMessage([], {
      type: "runtime_state",
      phase: "running",
      turn_id: "t1",
      provider: "test",
      permission_preset: "ask_for_approval",
      approval: null,
      question: null,
      jobs: [],
      context_window: {
        input_tokens: 10,
        max_input_tokens: 900,
        max_context_tokens: 1000,
        output_reserve_tokens: 100,
        compression_threshold: 720,
        compression_count: 0,
      },
      runtime_warnings: ["Skipping invalid skill."],
      plan: null,
    });

    expect(feedback).toEqual({
      kind: "alert",
      id: "runtime-warnings",
      level: "warning",
      text: "Skipping invalid skill.",
    });
  });

  it("only persists MCP failures as alerts", () => {
    expect(applyMessage([], {
      type: "mcp_server_status",
      server: "search",
      status: "ready",
      tool_count: 2,
    }).feedback).toBeUndefined();

    expect(applyMessage([], {
      type: "mcp_server_status",
      server: "search",
      status: "unavailable",
      error: "connection refused",
    }).feedback).toEqual({
      kind: "alert",
      id: "mcp:search",
      level: "error",
      text: "MCP search 不可用：connection refused",
    });
  });

  it("streams deltas into a single assistant item", () => {
    const { items } = fold([
      { type: "assistant_delta", turn_id: "t1", text: "he", model_call_index: 1 },
      { type: "assistant_delta", turn_id: "t1", text: "llo", model_call_index: 1 },
    ]);

    expect(items).toEqual([
      { role: "assistant", content: "hello", streaming: true },
    ]);
  });

  it("reports context window state separately from feedback", () => {
    const applied = applyMessage([], {
      type: "context_window",
      turn_id: "t1",
      input_tokens: 120,
      max_input_tokens: 900,
      max_context_tokens: 1000,
      output_reserve_tokens: 100,
      compression_threshold: 720,
      compression_count: 2,
    });

    expect(applied.contextWindow?.compression_count).toBe(2);
    expect(applied.feedback).toBeUndefined();
  });

  it("settles the streamed item with its runtime timestamp", () => {
    const { items } = fold([
      { type: "assistant_delta", turn_id: "t1", text: "hi", model_call_index: 1 },
      {
        type: "assistant_message",
        turn_id: "t1",
        content: "hi there",
        timestamp_utc: "2026-09-09T00:00:00.000000Z",
        model_call_index: 1,
      },
    ]);

    expect(items).toEqual([
      {
        role: "assistant",
        content: "hi there",
        timestamp_utc: "2026-09-09T00:00:00.000000Z",
        streaming: false,
      },
    ]);
  });

  it("records a tool batch and its outcome", () => {
    const { items } = fold([
      {
        type: "tool_batch_started",
        turn_id: "t1",
        model_call_index: 1,
        tool_calls: [TOOL_CALL],
      },
      {
        type: "tool_result",
        turn_id: "t1",
        tool_call_id: "call-1",
        name: "shell",
        ok: true,
        error: null,
        tool_index: 1,
        tool_count: 1,
      },
    ]);

    expect(items[0]).toEqual({
      role: "assistant",
      content: null,
      tool_calls: [TOOL_CALL],
    });
    // The protocol carries no output, only the status.
    expect(JSON.parse(items[1].content as string)).toEqual({ ok: true });
  });

  it("applies a later tool result before earlier calls finish", () => {
    const calls = [
      { id: "call-a", name: "subagent", arguments: { task: "A" } },
      { id: "call-b", name: "subagent", arguments: { task: "B" } },
      { id: "call-c", name: "subagent", arguments: { task: "C" } },
    ];
    const { items } = fold([
      {
        type: "tool_batch_started",
        turn_id: "t1",
        model_call_index: 1,
        tool_calls: calls,
      },
      {
        type: "tool_result",
        turn_id: "t1",
        tool_call_id: "call-b",
        name: "subagent",
        ok: true,
        error: null,
        tool_index: 2,
        tool_count: 3,
      },
    ]);

    const toolParts = (toMessages(items)[0].content as any[]).filter(
      (part) => part.type === "tool-call",
    );
    expect(toolParts.map((part) => part.toolCallId)).toEqual([
      "call-a",
      "call-b",
      "call-c",
    ]);
    expect(toolParts.map((part) => part.result)).toEqual([
      undefined,
      { ok: true },
      undefined,
    ]);
  });

  it("keeps the error from a failed tool", () => {
    const { items } = fold([
      {
        type: "tool_result",
        turn_id: "t1",
        tool_call_id: "call-1",
        name: "shell",
        ok: false,
        error: { type: "PermissionError", message: "denied" },
        tool_index: 1,
        tool_count: 1,
      },
    ]);

    expect(JSON.parse(items[0].content as string)).toEqual({
      ok: false,
      error: { type: "PermissionError", message: "denied" },
    });
  });

  it("raises and then clears an approval request", () => {
    const requested = fold([
      {
        type: "approval_request",
        turn_id: "t1",
        request_id: "t1:1",
        command: "rm -rf build",
      },
    ]);
    expect(requested.approval).toEqual({
      requestId: "t1:1",
      command: "rm -rf build",
    });
    expect(requested.question).toBeNull();

    const completed = applyMessage(requested.items, {
      type: "turn_completed",
      turn_id: "t1",
      usage: null,
    });
    expect(completed.approval).toBeNull();
    expect(completed.finished).toBe(true);
  });

  it("raises and then clears a user question", () => {
    const question = {
      type: "user_question" as const,
      turn_id: "t1",
      request_id: "t1:1",
      question: "Which cache?",
      options: [{ id: "sqlite", label: "SQLite", recommended: true }],
      allow_free_text: true,
    };
    const requested = applyMessage([], question);
    expect(requested.question).toEqual(question);
    expect(requested.approval).toBeNull();

    const completed = applyMessage([], {
      type: "turn_completed",
      turn_id: "t1",
      usage: null,
    });
    expect(completed.question).toBeNull();
  });

  it("accumulates reasoning deltas and keeps them before the answer", () => {
    const { items } = fold([
      { type: "reasoning_delta", turn_id: "t1", text: "weighing ", model_call_index: 1 },
      { type: "reasoning_delta", turn_id: "t1", text: "the options", model_call_index: 1 },
      { type: "assistant_delta", turn_id: "t1", text: "Pushing", model_call_index: 1 },
      {
        type: "assistant_message",
        turn_id: "t1",
        content: "Pushing now.",
        timestamp_utc: "2026-09-09T00:00:00.000000Z",
        model_call_index: 1,
      },
    ]);

    expect(items).toEqual([
      {
        role: "assistant",
        content: "Pushing now.",
        reasoning: "weighing the options",
        timestamp_utc: "2026-09-09T00:00:00.000000Z",
        streaming: false,
      },
    ]);

    const messages = toMessages(items);
    const parts = messages[0].content as { type: string; text?: string }[];
    expect(parts).toEqual([
      { type: "reasoning", text: "weighing the options" },
      { type: "text", text: "Pushing now." },
    ]);
  });

  it("settles open text and reports a cancelled turn", () => {
    const { items, feedback, finished } = fold([
      { type: "assistant_delta", turn_id: "t1", text: "partial", model_call_index: 1 },
      { type: "turn_cancelled", turn_id: "t1" },
    ]);

    expect(items).toEqual([
      { role: "assistant", content: "partial", streaming: false },
    ]);
    expect(feedback).toEqual({ kind: "toast", level: "info", text: "已取消。" });
    expect(finished).toBe(true);
  });

  it("reports a failed turn as a persistent alert", () => {
    const { feedback, finished } = fold([
      {
        type: "turn_failed",
        turn_id: "t1",
        error: { type: "ContextWindowExceededError", message: "too long" },
      },
    ]);

    expect(feedback).toEqual({
      kind: "alert",
      id: "turn-error",
      level: "error",
      text: "ContextWindowExceededError: too long",
    });
    expect(finished).toBe(true);
  });

  it("ignores messages that carry no transcript change", () => {
    const items: TranscriptItem[] = [{ role: "user", content: "hi" }];
    for (const message of [
      {
        type: "session_ready" as const,
        session_id: "s1",
        workspace: "/tmp",
        provider: "test",
        model: "m",
        resumed: false,
        message_count: 0,
        permission_preset: "ask_for_approval" as const,
      },
      {
        type: "tool_call" as const,
        turn_id: "t1",
        tool_call: TOOL_CALL,
        tool_index: 1,
        tool_count: 1,
      },
    ]) {
      expect(applyMessage(items, message).items).toBe(items);
    }
  });
});

describe("toMessages", () => {
  /** Reads the parts of a message; toMessages never produces plain text. */
  function parts(message: { content: unknown }) {
    return message.content as {
      type: string;
      text?: string;
      isError?: boolean;
      result?: unknown;
    }[];
  }

  it("renders stored reasoning before the answer of the same item", () => {
    const messages = toMessages([
      { role: "assistant", content: null, reasoning: "weighing the options", tool_calls: [TOOL_CALL] },
    ]);

    expect(parts(messages[0]).map((part) => part.type)).toEqual([
      "reasoning",
      "tool-call",
    ]);
  });

  it("folds a turn's assistant items into one message", () => {
    // The shape of a stored turn: the model narrates, calls a tool, then
    // narrates again before its final answer.
    const messages = toMessages([
      { role: "user", content: "run it" },
      { role: "assistant", content: "Working on it.", tool_calls: [TOOL_CALL] },
      { role: "tool", tool_call_id: "call-1", content: '{"ok":true}' },
      { role: "assistant", content: "Done." },
    ]);

    expect(messages).toHaveLength(2);
    expect(messages[0].role).toBe("user");
    expect(messages[1].role).toBe("assistant");
    expect(parts(messages[1]).map((part) => part.type)).toEqual([
      "text",
      "tool-call",
      "text",
    ]);
  });

  it("identifies reasoning and intermediate work before the final answer", () => {
    const messages = toMessages([
      { role: "user", content: "fix it" },
      { role: "assistant", content: "Checking.", reasoning: "First inspect it." },
      { role: "assistant", content: null, tool_calls: [TOOL_CALL] },
      { role: "tool", tool_call_id: "call-1", content: '{"ok":true}' },
      { role: "assistant", content: "Fixed." },
    ]);
    const assistantParts = parts(messages[1]);

    expect(turnProcessPartIndexes(assistantParts)).toEqual([0, 1, 2]);
    expect(assistantParts[3]).toMatchObject({ type: "text", text: "Fixed." });
  });

  it("keeps tool-produced media with the process before the final text", () => {
    expect(turnProcessPartIndexes([
      { type: "tool-call" },
      { type: "image" },
      { type: "text" },
    ])).toEqual([0, 1]);
  });

  it("does not treat tool-calling text as a final answer", () => {
    const messages = toMessages([
      { role: "user", content: "run it" },
      { role: "assistant", content: "Running it now.", tool_calls: [TOOL_CALL] },
    ]);

    expect(turnProcessPartIndexes(parts(messages[1]))).toEqual([0, 1]);
  });

  it("streams a possible final answer outside the process group", () => {
    const messages = toMessages([
      { role: "user", content: "fix it" },
      { role: "assistant", content: "Still working." },
    ]);

    expect(turnProcessPartIndexes(parts(messages[1]))).toEqual([]);
  });

  it("keeps process parts grouped while the trailing answer streams", () => {
    expect(turnProcessPartIndexes([
      { type: "reasoning" },
      { type: "tool-call" },
      { type: "text" },
    ])).toEqual([0, 1]);
  });

  it("keeps earlier final answers visible while a new turn is running", () => {
    const messages = toMessages([
      { role: "user", content: "first" },
      { role: "assistant", content: "First answer." },
      { role: "user", content: "continue" },
      { role: "assistant", content: "Still working." },
    ]);

    expect(turnProcessPartIndexes(parts(messages[1]))).toEqual([]);
    expect(turnProcessPartIndexes(parts(messages[3]))).toEqual([]);
  });

  it("starts a new assistant message after a user item", () => {
    const messages = toMessages([
      { role: "assistant", content: "First." },
      { role: "user", content: "again" },
      { role: "assistant", content: "Second." },
    ]);

    expect(messages.map((message) => message.role)).toEqual([
      "assistant",
      "user",
      "assistant",
    ]);
  });

  it("dates the merged message by the turn's last item", () => {
    const messages = toMessages([
      {
        role: "assistant",
        content: "Working.",
        timestamp_utc: "2026-09-09T00:00:00.000000Z",
      },
      { role: "assistant", content: "Done.", timestamp_utc: "2026-09-09T00:01:00.000000Z" },
    ]);

    expect(messages).toHaveLength(1);
    expect(messages[0].createdAt).toEqual(new Date("2026-09-09T00:01:00.000000Z"));
  });

  it("attaches the stored result to its tool call", () => {
    const messages = toMessages([
      { role: "assistant", content: null, tool_calls: [TOOL_CALL] },
      {
        role: "tool",
        tool_call_id: "call-1",
        content: '{"ok":false,"error":{"message":"denied"}}',
      },
    ]);

    const part = parts(messages[0])[0];
    expect(part.type).toBe("tool-call");
    expect(part.isError).toBe(true);
  });

  it("keeps stored message text and timestamp separate", () => {
    const messages = toMessages([
      {
        role: "user",
        content: "hello",
        timestamp_utc: "2026-09-09T00:00:00.000000Z",
      },
    ]);

    expect(messages[0].content).toEqual([{ type: "text", text: "hello" }]);
    expect(messages[0].createdAt).toEqual(new Date("2026-09-09T00:00:00.000000Z"));
  });

  it("skips a tool item whose output was never stored", () => {
    const messages = toMessages([
      { role: "assistant", content: null, tool_calls: [TOOL_CALL] },
      { role: "tool", tool_call_id: "call-1", content: null },
    ]);

    const part = parts(messages[0])[0];
    expect(part.result).toBeUndefined();
  });
});
