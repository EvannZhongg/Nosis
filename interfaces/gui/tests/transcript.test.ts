import { describe, expect, it } from "vitest";
import type { Incoming } from "@nosis/protocol";
import { applyMessage, toMessages, type TranscriptItem } from "../src/transcript";

const TOOL_CALL = { id: "call-1", name: "shell", arguments: { command: "ls" } };

/** Folds a sequence of bridge messages, as the socket callback does. */
function fold(messages: Incoming[], initial: TranscriptItem[] = []) {
  return messages.reduce(
    (state, message) => {
      const applied = applyMessage(state.items, message);
      return {
        items: applied.items,
        notice: applied.notice ?? state.notice,
        approval:
          applied.approval !== undefined ? applied.approval : state.approval,
        finished: applied.finished ?? state.finished,
      };
    },
    {
      items: initial,
      notice: undefined,
      approval: null,
      finished: false,
    } as ReturnType<typeof applyMessage> & { approval: unknown },
  );
}

describe("applyMessage", () => {
  it("streams deltas into a single assistant item", () => {
    const { items } = fold([
      { type: "assistant_delta", turn_id: "t1", text: "he", model_call_index: 1 },
      { type: "assistant_delta", turn_id: "t1", text: "llo", model_call_index: 1 },
    ]);

    expect(items).toEqual([
      { role: "assistant", content: "hello", streaming: true },
    ]);
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
    expect(JSON.parse(items[1].content!)).toEqual({ ok: true });
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

    expect(JSON.parse(items[0].content!)).toEqual({
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

    const completed = applyMessage(requested.items, {
      type: "turn_completed",
      turn_id: "t1",
      usage: null,
    });
    expect(completed.approval).toBeNull();
    expect(completed.finished).toBe(true);
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

  it("reports the finished turn's token usage", () => {
    const applied = applyMessage([], {
      type: "turn_completed",
      turn_id: "t1",
      usage: { input_tokens: 900, output_tokens: 100, total_tokens: 1000 },
    });

    expect(applied.usage).toEqual({
      input_tokens: 900,
      output_tokens: 100,
      total_tokens: 1000,
    });
    // Anything else leaves the status area as it was.
    expect(
      applyMessage([], {
        type: "assistant_delta",
        turn_id: "t1",
        text: "hi",
        model_call_index: 1,
      }).usage,
    ).toBeUndefined();
  });

  it("settles open text and reports a cancelled turn", () => {
    const { items, notice, finished } = fold([
      { type: "assistant_delta", turn_id: "t1", text: "partial", model_call_index: 1 },
      { type: "turn_cancelled", turn_id: "t1" },
    ]);

    expect(items).toEqual([
      { role: "assistant", content: "partial", streaming: false },
    ]);
    expect(notice).toEqual({ level: "info", text: "已取消。" });
    expect(finished).toBe(true);
  });

  it("reports a failed turn as an error notice", () => {
    const { notice, finished } = fold([
      {
        type: "turn_failed",
        turn_id: "t1",
        error: { type: "ContextWindowExceededError", message: "too long" },
      },
    ]);

    expect(notice).toEqual({
      level: "error",
      text: "ContextWindowExceededError: too long",
    });
    expect(finished).toBe(true);
  });

  it("ignores messages that carry no transcript change", () => {
    const items: TranscriptItem[] = [{ role: "user", content: "hi" }];
    for (const message of [
      {
        type: "ready" as const,
        session_id: "s1",
        workspace: "/tmp",
        model: "m",
        resumed: false,
        message_count: 0,
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
