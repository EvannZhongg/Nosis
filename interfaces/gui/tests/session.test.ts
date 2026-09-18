import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Incoming } from "@nosis/protocol";
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
});
