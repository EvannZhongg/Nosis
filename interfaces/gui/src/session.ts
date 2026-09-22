import { runtimeIsActive, type Incoming, type Outgoing, type RuntimePhase } from "@nosis/protocol";

export type SessionSocketOptions = {
  sessionId: string | null;
  provider?: string;
  workspace?: string | null;
  attachmentId: string;
  attachOnly?: boolean;
  afterEvent?: number;
  takeover?: boolean;
  onMessage: (message: Incoming) => void;
  onConfigurationRejected?: (type: "provider_set" | "workspace_set") => void;
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
  private phase: RuntimePhase = "inactive";

  constructor(private readonly options: SessionSocketOptions) {
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
      if (message.type === "runtime_state" && !message.replayed) {
        this.phase = message.phase;
      } else if (!message.replayed && (
        message.type === "turn_completed" || message.type === "turn_cancelled" || message.type === "turn_failed"
      )) {
        this.phase = "idle";
      }
      options.onMessage(message);
      // Only the current snapshot ends attachment synchronization; historical
      // runtime_state events can describe an earlier idle turn.
      if (message.type === "runtime_state" && !message.replayed && !this.synchronized && this.socket.readyState === WebSocket.OPEN) {
        this.synchronized = true;
        for (const queued of this.queued) this.dispatch(queued);
        this.queued = [];
      }
    };
    this.socket.onclose = () => options.onClose();
    this.socket.onerror = () => options.onError();
  }

  /** Sends now, or after the opening runtime snapshot has been applied. */
  send(message: Sendable): void {
    if (this.socket.readyState === WebSocket.OPEN && this.synchronized) {
      this.dispatch(message);
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

  private dispatch(message: Sendable): void {
    // Configuration selected before attachment is conditional on the current
    // snapshot, never on a replayed phase or React's next render.
    if ((message.type === "provider_set" || message.type === "workspace_set")
      && runtimeIsActive(this.phase)) {
      this.options.onConfigurationRejected?.(message.type);
      return;
    }
    if (message.type === "user_turn") this.phase = "starting";
    this.write(message);
  }

  private write(message: object): void {
    this.socket.send(JSON.stringify(message));
  }
}
