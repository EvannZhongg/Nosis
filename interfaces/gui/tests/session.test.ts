import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Incoming, Outgoing } from "@nosis/protocol";
import { SessionSocket } from "../src/session";

class FakeWebSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSED = 3;
  static latest: FakeWebSocket;

  readyState = FakeWebSocket.CONNECTING;
  sent: string[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;

  constructor(readonly url: string) {
    FakeWebSocket.latest = this;
  }

  open() {
    this.readyState = FakeWebSocket.OPEN;
    this.onopen?.();
  }

  receive(message: Incoming) {
    this.onmessage?.({ data: JSON.stringify(message) });
  }

  send(message: string) {
    this.sent.push(message);
  }

  close() {
    this.readyState = FakeWebSocket.CLOSED;
    this.onclose?.();
  }
}

const snapshot: Incoming = {
  type: "runtime_state",
  phase: "inactive",
  turn_id: null,
  provider: "default",
  permission_preset: "ask_for_approval",
  context_window: null,
  jobs: [],
  approval: null,
  question: null,
  plan: null,
};

describe("SessionSocket synchronization", () => {
  beforeEach(() => {
    vi.stubGlobal("location", { protocol: "http:", host: "localhost" });
    vi.stubGlobal("WebSocket", FakeWebSocket);
  });

  it("sends open_session first and holds commands until runtime_state", () => {
    const received: string[] = [];
    const socket = new SessionSocket({
      sessionId: "session-1",
      attachmentId: "page-1",
      onMessage: (message) => received.push(message.type),
      onClose: () => {},
      onError: () => {},
    });
    socket.send({ type: "user_turn", turn_id: "turn-1", text: "hello" });

    FakeWebSocket.latest.open();
    expect(FakeWebSocket.latest.sent.map((message) => JSON.parse(message))).toEqual([{
      type: "open_session",
      session_id: "session-1",
      attachment_id: "page-1",
    }]);

    FakeWebSocket.latest.receive({
      type: "session_ready",
      session_id: "session-1",
      workspace: "/workspace",
      provider: "default",
      model: "test/model",
      resumed: false,
      message_count: 0,
      permission_preset: "ask_for_approval",
    });
    expect(FakeWebSocket.latest.sent).toHaveLength(1);

    FakeWebSocket.latest.receive(snapshot);
    expect(received).toEqual(["session_ready", "runtime_state"]);
    expect(FakeWebSocket.latest.sent.map((message) => JSON.parse(message))[1]).toEqual({
      type: "user_turn",
      turn_id: "turn-1",
      text: "hello",
    });
  });

  it("queues workspace, model and permission choices during startup in order", () => {
    const socket = new SessionSocket({
      sessionId: "session-1",
      attachmentId: "page-1",
      onMessage: () => {},
      onClose: () => {},
      onError: () => {},
    });
    const choices = [
      { type: "workspace_set", workspace: "/selected-workspace" },
      { type: "provider_set", provider: "selected-model" },
      { type: "permission_set", preset: "workspace_access" },
    ] satisfies Outgoing[];
    socket.send(choices[0]);
    FakeWebSocket.latest.open();
    socket.send(choices[1]);
    socket.send(choices[2]);
    expect(FakeWebSocket.latest.sent).toHaveLength(1);

    FakeWebSocket.latest.receive(snapshot);
    expect(FakeWebSocket.latest.sent.slice(1).map((message) => JSON.parse(message))).toEqual(choices);

    // Later snapshots from configuration changes must not resend the choices.
    FakeWebSocket.latest.receive(snapshot);
    expect(FakeWebSocket.latest.sent).toHaveLength(4);
  });

  it("discards startup choices when the page cannot acquire the session", () => {
    const socket = new SessionSocket({
      sessionId: "session-1",
      attachmentId: "page-2",
      onMessage: (message) => {
        if (message.type === "attachment_replaced") socket.close();
      },
      onClose: () => {},
      onError: () => {},
    });
    socket.send({ type: "permission_set", preset: "full_access" });
    FakeWebSocket.latest.open();
    FakeWebSocket.latest.receive({ type: "attachment_replaced", phase: "running" });

    expect(FakeWebSocket.latest.readyState).toBe(FakeWebSocket.CLOSED);
    expect(FakeWebSocket.latest.sent.map((message) => JSON.parse(message).type)).toEqual(["open_session"]);
  });

  it.each(["starting", "running", "waiting_approval", "waiting_user"] as const)(
    "rejects queued configuration for a %s session without blocking permissions or cancel",
    (phase) => {
      const rejected = vi.fn();
      const socket = new SessionSocket({
        sessionId: "session-1",
        attachmentId: "new-page",
        onMessage: () => {},
        onConfigurationRejected: rejected,
        onClose: () => {},
        onError: () => {},
      });
      socket.send({ type: "provider_set", provider: "second" });
      socket.send({ type: "workspace_set", workspace: "/new-workspace" });
      socket.send({ type: "permission_set", preset: "workspace_access" });
      FakeWebSocket.latest.open();
      FakeWebSocket.latest.receive({ ...snapshot, phase, turn_id: "active-turn" });

      expect(rejected.mock.calls).toEqual([["provider_set"], ["workspace_set"]]);
      expect(FakeWebSocket.latest.sent.slice(1).map((message) => JSON.parse(message))).toEqual([
        { type: "permission_set", preset: "workspace_access" },
      ]);
      socket.send({ type: "cancel", turn_id: "active-turn" });
      expect(JSON.parse(FakeWebSocket.latest.sent.at(-1)!)).toEqual({ type: "cancel", turn_id: "active-turn" });

      FakeWebSocket.latest.receive({ ...snapshot, phase: "idle" });
      expect(FakeWebSocket.latest.sent).toHaveLength(3);
      socket.send({ type: "provider_set", provider: "second" });
      expect(JSON.parse(FakeWebSocket.latest.sent.at(-1)!)).toEqual({ type: "provider_set", provider: "second" });
    },
  );

  it("waits through replayed idle snapshots for the current running snapshot", () => {
    const rejected = vi.fn();
    const socket = new SessionSocket({
      sessionId: "session-1",
      attachmentId: "new-page",
      onMessage: () => {},
      onConfigurationRejected: rejected,
      onClose: () => {},
      onError: () => {},
    });
    socket.send({ type: "provider_set", provider: "second" });
    socket.send({ type: "workspace_set", workspace: "/new-workspace" });
    FakeWebSocket.latest.open();
    FakeWebSocket.latest.receive({ ...snapshot, replayed: true, event_sequence: 2 });
    FakeWebSocket.latest.receive({ type: "turn_completed", turn_id: "old-turn", usage: null, replayed: true });
    expect(FakeWebSocket.latest.sent).toHaveLength(1);
    expect(rejected).not.toHaveBeenCalled();

    FakeWebSocket.latest.receive({ ...snapshot, phase: "running", turn_id: "active-turn", event_sequence: 8 });
    expect(FakeWebSocket.latest.sent).toHaveLength(1);
    expect(rejected.mock.calls).toEqual([["provider_set"], ["workspace_set"]]);
  });

  it("guards changes made before React renders the active phase and unlocks after completion", () => {
    const rejected = vi.fn();
    const socket = new SessionSocket({
      sessionId: "session-1",
      attachmentId: "page-1",
      onMessage: () => {},
      onConfigurationRejected: rejected,
      onClose: () => {},
      onError: () => {},
    });
    FakeWebSocket.latest.open();
    FakeWebSocket.latest.receive(snapshot);
    socket.send({ type: "user_turn", turn_id: "t1", text: "hello" });
    socket.send({ type: "provider_set", provider: "second" });
    expect(rejected).toHaveBeenCalledWith("provider_set");
    expect(FakeWebSocket.latest.sent).toHaveLength(2);

    FakeWebSocket.latest.receive({ type: "turn_completed", turn_id: "t1", usage: null });
    socket.send({ type: "provider_set", provider: "second" });
    expect(FakeWebSocket.latest.sent).toHaveLength(3);
  });
});
