import { runtimeIsActive, type Incoming, type Outgoing, type RuntimePhase } from "@nosis/protocol/internal";

export type SessionSocketOptions = {
  sessionId: string | null;
  provider?: string;
  workspace?: string | null;
  attachmentId: string;
  attachOnly?: boolean;
  afterEvent?: number;
  takeover?: boolean;
  onMessage: (message: Incoming) => void;
  onConfigurationRejected?: (type: "provider_set" | "workspace_set" | "runtime_refresh") => void;
  onClose: () => void;
  onError: () => void;
};

/** What the browser may send: relayed bridge messages plus 'cancel'. */
type Sendable =
  Extract<Outgoing, { type: "user_turn" | "user_steer" | "approval_response" | "permission_set" | "provider_set" | "workspace_set" | "runtime_refresh" | "user_question_response" | "cancel" }>;
type UserTurn = Extract<Sendable, { type: "user_turn" }>;
type QueuedCommand = {
  message: Sendable;
  acknowledgment?: {
    resolve: () => void;
    reject: (error: Error) => void;
  };
};

/**
 * Owns one WebSocket to the agent bridge for the lifetime of a session.
 *
 * The server builds the bridge's `open_session` message, so the opening frame
 * only carries the session and provider choice.
 */
export class SessionSocket {
  private socket: WebSocket;
  private queued: QueuedCommand[] = [];
  private pendingUserTurns = new Map<string, QueuedCommand["acknowledgment"]>();
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
      if (message.type === "session_open_failed") {
        this.synchronized = false;
        this.rejectUnconfirmedTurns(new Error(message.error.message));
        options.onMessage(message);
        this.socket.close();
        return;
      }
      const state = message.type === "runtime_state" ? message : message.runtime;
      if (state && !message.replayed) {
        this.phase = state.phase;
      }
      options.onMessage(message);
      if (message.type === "user_turn_received") {
        const acknowledgment = this.pendingUserTurns.get(message.turn_id);
        if (acknowledgment) {
          this.pendingUserTurns.delete(message.turn_id);
          acknowledgment.resolve();
        }
      }
      // Only the current snapshot ends attachment synchronization; historical
      // runtime_state events can describe an earlier idle turn.
      if (message.type === "runtime_state" && !message.replayed && !this.synchronized && this.socket.readyState === WebSocket.OPEN) {
        this.synchronized = true;
        for (const queued of this.queued) {
          this.dispatch(queued.message, queued.acknowledgment);
        }
        this.queued = [];
      }
    };
    this.socket.onclose = () => {
      this.rejectUnconfirmedTurns();
      options.onClose();
    };
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
      this.queued.push({ message });
    }
  }

  /** Resolves only after the bridge confirms that it accepted the turn. */
  sendUserTurn(message: UserTurn): Promise<void> {
    return new Promise((resolve, reject) => {
      const command = { message, acknowledgment: { resolve, reject } };
      if (this.socket.readyState === WebSocket.OPEN && this.synchronized) {
        this.dispatch(command.message, command.acknowledgment);
      } else if (
        this.socket.readyState === WebSocket.CONNECTING
        || (this.socket.readyState === WebSocket.OPEN && !this.synchronized)
      ) {
        this.queued.push(command);
      } else {
        reject(new Error("session connection closed before receiving the turn"));
      }
    });
  }

  close(): void {
    this.rejectUnconfirmedTurns();
    this.synchronized = false;
    this.socket.close();
  }

  private dispatch(
    message: Sendable,
    acknowledgment?: QueuedCommand["acknowledgment"],
  ): void {
    // Configuration selected before attachment is conditional on the current
    // snapshot, never on a replayed phase or React's next render.
    if ((message.type === "provider_set" || message.type === "workspace_set" || message.type === "runtime_refresh")
      && runtimeIsActive(this.phase)) {
      this.options.onConfigurationRejected?.(message.type);
      return;
    }
    if (message.type === "user_turn") {
      this.phase = "starting";
      if (acknowledgment) {
        this.pendingUserTurns.set(message.turn_id, acknowledgment);
      }
    }
    if (message.type === "runtime_refresh") this.phase = "starting";
    try {
      this.write(message);
    } catch (error) {
      if (message.type === "user_turn" && acknowledgment) {
        this.pendingUserTurns.delete(message.turn_id);
        acknowledgment.reject(error instanceof Error ? error : new Error(String(error)));
      }
    }
  }

  private rejectUnconfirmedTurns(error = new Error("session connection closed before confirming the turn")): void {
    for (const command of this.queued) command.acknowledgment?.reject(error);
    this.queued = [];
    for (const acknowledgment of this.pendingUserTurns.values()) {
      acknowledgment?.reject(error);
    }
    this.pendingUserTurns.clear();
  }

  private write(message: object): void {
    this.socket.send(JSON.stringify(message));
  }
}
