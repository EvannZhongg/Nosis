import type { Incoming, Outgoing } from "@nosis/protocol";

export type SessionSocketOptions = {
  sessionId: string | null;
  provider: string;
  attachOnly?: boolean;
  afterEvent?: number;
  onMessage: (message: Incoming) => void;
  onClose: () => void;
  onError: () => void;
};

/** What the browser may send: relayed bridge messages plus 'cancel'. */
type Sendable =
  | Extract<Outgoing, { type: "user_turn" | "approval_response" }>
  | { type: "cancel" };

/**
 * Owns one WebSocket to the agent bridge for the lifetime of a session.
 *
 * The server builds the bridge's 'start' message, so the opening frame
 * only carries the session and provider choice.
 */
export class SessionSocket {
  private socket: WebSocket;
  private queued: Sendable[] = [];

  constructor(options: SessionSocketOptions) {
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    this.socket = new WebSocket(`${scheme}://${location.host}/api/session`);

    this.socket.onopen = () => {
      this.write({
        type: "start",
        session_id: options.sessionId,
        provider: options.provider,
        ...(options.attachOnly ? { attach_only: true } : {}),
        ...(options.afterEvent ? { after_event: options.afterEvent } : {}),
      });
      for (const message of this.queued) this.write(message);
      this.queued = [];
    };
    this.socket.onmessage = ({ data }) =>
      options.onMessage(JSON.parse(data as string) as Incoming);
    this.socket.onclose = () => options.onClose();
    this.socket.onerror = () => options.onError();
  }

  /** Sends now, or once the connection opens. */
  send(message: Sendable): void {
    if (this.socket.readyState === WebSocket.OPEN) this.write(message);
    else if (this.socket.readyState === WebSocket.CONNECTING) {
      this.queued.push(message);
    }
  }

  close(): void {
    this.queued = [];
    this.socket.close();
  }

  private write(message: object): void {
    this.socket.send(JSON.stringify(message));
  }
}
