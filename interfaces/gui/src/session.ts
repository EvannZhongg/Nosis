import type { Incoming, Outgoing } from "@nosis/protocol";

export type SessionSocketOptions = {
  sessionId: string | null;
  provider?: string;
  workspace?: string | null;
  attachmentId: string;
  attachOnly?: boolean;
  afterEvent?: number;
  takeover?: boolean;
  onMessage: (message: Incoming) => void;
  onClose: () => void;
  onError: () => void;
};

/** What the browser may send: relayed bridge messages plus 'cancel'. */
type Sendable =
  Extract<Outgoing, { type: "user_turn" | "user_steer" | "approval_response" | "permission_set" | "provider_set" | "workspace_set" | "user_question_response" | "cancel" }>;

/**
 * Owns one WebSocket to the agent bridge for the lifetime of a session.
 *
 * The server builds the bridge's `open_session` message, so the opening frame
 * only carries the session and provider choice.
 */
export class SessionSocket {
  private socket: WebSocket;
  private queued: Sendable[] = [];
  private synchronized = false;

  constructor(options: SessionSocketOptions) {
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    this.socket = new WebSocket(`${scheme}://${location.host}/api/session`);

    this.socket.onopen = () => {
      this.write({
        type: "open_session",
        session_id: options.sessionId,
        provider: options.provider,
        workspace: options.workspace ?? undefined,
        attachment_id: options.attachmentId,
        ...(options.attachOnly ? { attach_only: true } : {}),
        ...(options.afterEvent !== undefined ? { after_event: options.afterEvent } : {}),
        ...(options.takeover ? { takeover: true } : {}),
      });
    };
    this.socket.onmessage = ({ data }) => {
      const message = JSON.parse(data as string) as Incoming;
      options.onMessage(message);
      // runtime_state is the protocol synchronization barrier. The opening
      // snapshot must be applied before any browser command can race with it
      // and make an optimistic local turn look idle again.
      if (message.type === "runtime_state" && !this.synchronized) {
        this.synchronized = true;
        for (const queued of this.queued) this.write(queued);
        this.queued = [];
      }
    };
    this.socket.onclose = () => options.onClose();
    this.socket.onerror = () => options.onError();
  }

  /** Sends now, or after the opening runtime snapshot has been applied. */
  send(message: Sendable): void {
    if (this.socket.readyState === WebSocket.OPEN && this.synchronized) {
      this.write(message);
    } else if (
      this.socket.readyState === WebSocket.CONNECTING
      || (this.socket.readyState === WebSocket.OPEN && !this.synchronized)
    ) {
      this.queued.push(message);
    }
  }

  close(): void {
    this.queued = [];
    this.synchronized = false;
    this.socket.close();
  }

  private write(message: object): void {
    this.socket.send(JSON.stringify(message));
  }
}
