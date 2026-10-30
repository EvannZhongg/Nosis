// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { AssistantRuntimeProvider, ThreadPrimitive } from "@assistant-ui/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Incoming, Outgoing } from "@nosis/protocol/internal";
import type { SessionSocketOptions } from "../src/session";
import { Composer } from "../src/chat/Composer";
import { useSessionRuntime } from "../src/chat/useSessionRuntime";

const sent: Outgoing[] = [];
let connection: SessionSocketOptions;
const onTurnEnd = vi.fn();
const onSessionAvailable = vi.fn();
const onTitleChange = vi.fn();
const noop = () => {};
const session = { session_id: "commands", workspace: "/workspace", items: [], permission_preset: "full_access" as const };
const providers = [{ id: "test", model: "test/model" }];

vi.mock("../src/session", () => ({
  SessionSocket: class {
    constructor(options: SessionSocketOptions) { connection = options; }
    send(message: Outgoing) { sent.push(message); }
    async sendUserTurn(message: Outgoing) { sent.push(message); }
    close() {}
  },
}));

function TestComposer() {
  const runtime = useSessionRuntime({
    session, selected: true, contextWindow: null, workspaceOptions: [], backgroundActive: false,
    providers, provider: "test", onProviderChange: noop, onBusyChange: noop,
    onContextWindowChange: noop, onSessionAvailable, onTitleChange, onTurnEnd, onInitialPromptConsumed: noop,
  });
  return <AssistantRuntimeProvider runtime={runtime.assistantRuntime}><ThreadPrimitive.Root className="thread">
    <Composer runtime={runtime} />
  </ThreadPrimitive.Root></AssistantRuntimeProvider>;
}

describe("composer commands", () => {
  let container: HTMLDivElement;
  let root: Root;
  const snapshot: Incoming = {
    type: "runtime_state", phase: "idle", turn_id: null, provider: "test",
    permission_preset: "full_access", context_window: null, cache_usage: null,
    jobs: [], approval: null, question: null, plan: null,
  };

  beforeEach(async () => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
    sent.length = 0;
    onTurnEnd.mockClear();
    onSessionAvailable.mockClear();
    onTitleChange.mockClear();
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
    await act(async () => root.render(<TestComposer />));
    await receive(snapshot);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  });

  const input = () => container.querySelector("textarea")!;
  async function type(text: string) {
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!.call(input(), text);
      input().dispatchEvent(new Event("input", { bubbles: true }));
    });
  }
  async function key(value: string, options: KeyboardEventInit = {}) {
    await act(async () => input().dispatchEvent(new KeyboardEvent("keydown", { key: value, bubbles: true, cancelable: true, ...options })));
  }
  async function receive(message: Incoming) {
    await act(async () => connection.onMessage(message));
  }

  it("applies title updates during a running turn without treating them as turn completion", async () => {
    await receive({ ...snapshot, phase: "running", turn_id: "active" });
    await receive({
      type: "session_title_updated", session_id: session.session_id,
      title: "分析会话加载", title_source: "generated",
    });
    expect(onTitleChange).toHaveBeenCalledWith(session.session_id, "分析会话加载");
    expect(onTurnEnd).not.toHaveBeenCalled();
    expect(sent).toEqual([]);
  });

  it("opens the slash menu and refreshes from a click without sending a message", async () => {
    await type("/");
    expect(container.querySelector('[role="listbox"]')?.textContent).toContain("/refresh");
    await act(async () => container.querySelector<HTMLButtonElement>('[role="option"]')!.click());
    expect(sent).toEqual([{ type: "runtime_refresh" }]);
    expect(input().value).toBe("");
    expect(container.textContent).toContain("正在重新加载运行环境");
    expect(container.querySelector('[aria-label="停止执行"]')).toBeNull();
    await receive({ ...snapshot, phase: "idle" });
    await receive({ type: "runtime_refreshed", provider: "test", model: "test/model" });
    expect(container.textContent).toContain("运行环境已重新加载");
    expect(onTurnEnd).not.toHaveBeenCalled();
    expect(onSessionAvailable).not.toHaveBeenCalled();
  });

  it("filters, completes with Tab, dismisses with Escape and submits the exact command", async () => {
    await type("/ref");
    await key("ArrowDown");
    await key("Tab");
    expect(input().value).toBe("/refresh");
    await key("Escape");
    expect(container.querySelector('[role="listbox"]')).toBeNull();
    await key("Enter");
    expect(sent).toEqual([{ type: "runtime_refresh" }]);
  });

  it("handles the send button as a command submission", async () => {
    await type("/refresh");
    await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="发送消息"]')!.click());
    expect(sent).toEqual([{ type: "runtime_refresh" }]);
  });

  it("preserves the command while running and does not turn it into steering", async () => {
    await receive({ ...snapshot, phase: "running", turn_id: "active" });
    await type("/refresh");
    expect(container.querySelector<HTMLButtonElement>('[role="option"]')!.disabled).toBe(true);
    await key("Enter");
    expect(sent).toEqual([]);
    expect(input().value).toBe("/refresh");
    await receive(snapshot);
    await key("Enter");
    expect(sent).toEqual([{ type: "runtime_refresh" }]);
  });

  it("keeps IME confirmation from executing a command", async () => {
    await type("/refresh");
    await key("Enter", { isComposing: true });
    expect(sent).toEqual([]);
    expect(input().value).toBe("/refresh");
  });

  it("preserves a new draft until refresh finishes", async () => {
    await type("/refresh");
    await key("Enter");
    await type("continue afterwards");
    await key("Enter");
    expect(sent).toEqual([{ type: "runtime_refresh" }]);
    expect(input().value).toBe("continue afterwards");
    await receive(snapshot);
    await receive({ type: "runtime_refreshed", provider: "test", model: "test/model" });
    await key("Enter");
    expect(sent.at(-1)).toEqual(expect.objectContaining({ type: "user_turn", text: "continue afterwards" }));
  });

  it("reports an assembly error without finishing a turn and allows another refresh", async () => {
    await type("/refresh");
    await key("Enter");
    await receive({ ...snapshot, phase: "failed" });
    await receive({ type: "runtime_refresh_failed", error: { type: "ValueError", message: "bad configuration", details: {} } });
    expect(container.textContent).toContain("重新加载失败：bad configuration");
    expect(onTurnEnd).not.toHaveBeenCalled();
    await type("/refresh");
    await key("Enter");
    expect(sent).toEqual([{ type: "runtime_refresh" }, { type: "runtime_refresh" }]);
  });

  it("leaves ordinary text, unknown commands and embedded slashes to normal messages", async () => {
    await type("explain /refresh");
    expect(container.querySelector('[role="listbox"]')).toBeNull();
    await type("/unknown");
    expect(container.querySelector('[role="listbox"]')).toBeNull();
    await key("Enter");
    expect(sent).toEqual([expect.objectContaining({ type: "user_turn", text: "/unknown" })]);
  });
});
