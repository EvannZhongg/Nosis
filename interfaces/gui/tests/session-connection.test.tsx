// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Incoming, Outgoing } from "@nosis/protocol/internal";
import { useSessionRuntime, type SessionRuntime } from "../src/chat/useSessionRuntime";

class FakeWebSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSED = 3;
  static all: FakeWebSocket[] = [];

  readyState = FakeWebSocket.CONNECTING;
  sent: Outgoing[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;

  constructor() { FakeWebSocket.all.push(this); }
  open() {
    this.readyState = FakeWebSocket.OPEN;
    this.onopen?.();
  }
  receive(message: Incoming) { this.onmessage?.({ data: JSON.stringify(message) }); }
  send(message: string) {
    if (this.readyState !== FakeWebSocket.OPEN) throw new Error("socket is not open");
    this.sent.push(JSON.parse(message));
  }
  close() {
    this.readyState = FakeWebSocket.CLOSED;
    this.onclose?.();
  }
  fail() {
    this.onerror?.();
    this.close();
  }
}

const noop = () => {};
const session = { session_id: "connection", workspace: "/workspace", items: [], permission_preset: "full_access" as const };
const providers = [{ id: "test", model: "test/model" }];
const snapshot: Incoming = {
  type: "runtime_state", phase: "idle", turn_id: null, provider: "test",
  permission_preset: "full_access", context_window: null, cache_usage: null,
  jobs: [], approval: null, question: null, plan: null, event_sequence: 12,
};
let runtime: SessionRuntime;

function Harness({ selected = true }: { selected?: boolean }) {
  runtime = useSessionRuntime({
    session, selected, contextWindow: null, workspaceOptions: [], backgroundActive: false,
    providers, provider: "test", onProviderChange: noop, onBusyChange: noop,
    onContextWindowChange: noop, onSessionAvailable: noop, onTurnEnd: noop, onInitialPromptConsumed: noop,
  });
  return null;
}

describe("session connection recovery", () => {
  let root: Root;
  let container: HTMLDivElement;
  let visibility: DocumentVisibilityState;
  const latest = () => FakeWebSocket.all.at(-1)!;

  beforeEach(async () => {
    vi.useFakeTimers();
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    vi.stubGlobal("WebSocket", FakeWebSocket);
    visibility = "visible";
    vi.spyOn(document, "visibilityState", "get").mockImplementation(() => visibility);
    FakeWebSocket.all = [];
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
    await act(async () => root.render(<Harness />));
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  async function synchronize(state: Incoming = snapshot) {
    await act(async () => {
      latest().open();
      latest().receive(state);
    });
  }

  async function resume(event = "visibilitychange") {
    await act(async () => (event === "visibilitychange" ? document : window).dispatchEvent(new Event(event)));
  }

  it("silently drops an expired idle connection and reconnects on the next message", async () => {
    await synchronize();
    await act(async () => latest().fail());
    expect(runtime.alerts).toEqual([]);
    await act(async () => vi.advanceTimersByTime(5000));
    expect(FakeWebSocket.all).toHaveLength(1);

    let delivery!: Promise<void>;
    await act(async () => { delivery = runtime.submitMessage("continue"); });
    await synchronize();
    const turn = latest().sent.find((message) => message.type === "user_turn")!;
    expect(turn).toMatchObject({ text: "continue" });
    await act(async () => {
      latest().receive({ type: "user_turn_received", turn_id: (turn as Extract<Outgoing, { type: "user_turn" }>).turn_id });
      await delivery;
    });
    expect(runtime.alerts).toEqual([]);
  });

  it("reports a failed connection attempt and clears it even when the recovered runtime failed", async () => {
    await act(async () => latest().fail());
    expect(runtime.alerts).toEqual([expect.objectContaining({ id: "connection", text: expect.stringContaining("无法建立 Nosis 会话连接") })]);
    await resume();
    await synchronize({ ...snapshot, phase: "failed" });
    expect(runtime.alerts).toEqual([]);
  });

  it("does not treat a historical snapshot as a successful connection", async () => {
    await synchronize({ ...snapshot, replayed: true });
    await act(async () => latest().fail());
    expect(runtime.alerts).toEqual([expect.objectContaining({ id: "connection" })]);
  });

  it.each(["visibilitychange", "pageshow", "online"])("reattaches a disconnected session on %s with its cursor and identity", async (event) => {
    await synchronize();
    const original = latest();
    await act(async () => original.fail());
    await resume(event);
    expect(FakeWebSocket.all).toHaveLength(2);
    await synchronize();
    expect(latest().sent).toEqual([{
      ...original.sent[0], attach_only: true, after_event: 12,
    }]);
    expect(runtime.attaching).toBe(false);
  });

  it("replaces a half-open connection once when several resume events arrive together", async () => {
    await synchronize();
    const original = latest();
    await act(async () => {
      document.dispatchEvent(new Event("visibilitychange"));
      window.dispatchEvent(new Event("pageshow"));
      window.dispatchEvent(new Event("online"));
    });
    expect(original.readyState).toBe(FakeWebSocket.CLOSED);
    expect(FakeWebSocket.all).toHaveLength(2);
    await act(async () => latest().open());
    await resume();
    expect(FakeWebSocket.all).toHaveLength(2);
    expect(runtime.attaching).toBe(true);
    await act(async () => latest().receive(snapshot));
    expect(runtime.attaching).toBe(false);
    expect(runtime.alerts).toEqual([]);
  });

  it("waits until the page is visible before restoring its connection", async () => {
    await synchronize();
    await act(async () => latest().fail());
    visibility = "hidden";
    await resume();
    await resume("online");
    expect(FakeWebSocket.all).toHaveLength(1);
    visibility = "visible";
    await resume();
    expect(FakeWebSocket.all).toHaveLength(2);
  });

  it("does not reconnect an unselected idle session", async () => {
    await synchronize();
    await act(async () => root.render(<Harness selected={false} />));
    await resume();
    expect(FakeWebSocket.all).toHaveLength(1);
  });

  it("does not take control back from another page", async () => {
    await synchronize();
    await act(async () => latest().receive({ type: "attachment_replaced", phase: "running" }));
    await resume();
    await resume("online");
    expect(FakeWebSocket.all).toHaveLength(1);
    expect(runtime.attachmentReplaced).toBe(true);
  });

  it("reattaches a running turn and ignores late callbacks from the old connection", async () => {
    const running: Incoming = { ...snapshot, phase: "running", turn_id: "active" };
    await synchronize(running);
    const original = latest();
    await resume();
    await act(async () => {
      original.fail();
      original.receive(snapshot);
    });
    await synchronize(running);
    expect(runtime.running).toBe(true);
    expect(runtime.alerts).toEqual([]);
    expect(latest().sent).toEqual([expect.objectContaining({ type: "open_session", attach_only: true })]);
  });

  it("keeps retrying a running connection and cancels the old timer when the page resumes", async () => {
    await synchronize({ ...snapshot, phase: "running", turn_id: "active" });
    await act(async () => latest().fail());
    expect(runtime.reconnecting).toBe(true);
    await act(async () => vi.advanceTimersByTime(1000));
    expect(FakeWebSocket.all).toHaveLength(2);
    await act(async () => latest().fail());
    await resume();
    await synchronize({ ...snapshot, phase: "running", turn_id: "active" });
    await act(async () => vi.advanceTimersByTime(1000));
    expect(FakeWebSocket.all).toHaveLength(3);
    expect(runtime.reconnecting).toBe(false);
  });

  it("preserves an unconfirmed message through page resume", async () => {
    await synchronize();
    const original = latest();
    let delivery!: Promise<void>;
    await act(async () => { delivery = runtime.submitMessage("keep my message"); });
    await resume();
    expect(FakeWebSocket.all).toHaveLength(1);
    const turn = original.sent.at(-1) as Extract<Outgoing, { type: "user_turn" }>;
    await act(async () => {
      original.receive({ type: "user_turn_received", turn_id: turn.turn_id });
      await delivery;
    });
    expect(runtime.messages.filter((message) => message.role === "user")).toHaveLength(1);
  });

  it("preserves a pending configuration change through page resume", async () => {
    await synchronize();
    await act(async () => runtime.changePermissionPreset("ask_for_approval"));
    await resume();
    expect(FakeWebSocket.all).toHaveLength(1);
    expect(runtime.permissionSaving).toBe(true);
    expect(latest().sent.at(-1)).toEqual({ type: "permission_set", preset: "ask_for_approval" });
  });

  it("removes resume handlers and reconnect timers on unmount", async () => {
    await synchronize({ ...snapshot, phase: "running", turn_id: "active" });
    await act(async () => latest().fail());
    await act(async () => root.render(null));
    await resume();
    await resume("online");
    await resume("pageshow");
    await act(async () => vi.advanceTimersByTime(1000));
    expect(FakeWebSocket.all).toHaveLength(1);
  });
});
