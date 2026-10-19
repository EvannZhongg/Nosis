import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import { MessageDecoder, type Incoming, type Outgoing } from '@nosis/protocol';

export type BridgeExit = { code: number | null; signal: NodeJS.Signals | null };

export type BridgeOptions<Message = Incoming> = {
  /** Installed Bridge executable. Defaults to nosis-bridge on PATH. */
  command?: string;
  args?: readonly string[];
  cwd?: string;
  env?: NodeJS.ProcessEnv;
  onMessage: (message: Message) => void;
  onExit: (code: number | null, signal: NodeJS.Signals | null) => void;
  onProtocolError: (error: Error) => void;
  onProcessError: (error: Error) => void;
};

/** Stdio transport only. Session/Runtime synchronization belongs to the caller. */
export class BridgeClient<Received = Incoming, Sent = Outgoing> {
  private child: ChildProcessWithoutNullStreams;
  private decoder = new MessageDecoder<Received>();
  private stderr: string[] = [];
  private closing = false;
  private ended = false;
  private protocolFailed = false;
  private killTimer?: NodeJS.Timeout;
  readonly closed: Promise<BridgeExit>;

  constructor(private options: BridgeOptions<Received>) {
    this.child = spawn(options.command ?? 'nosis-bridge', [...(options.args ?? [])], {
      cwd: options.cwd,
      env: options.env,
      stdio: ['pipe', 'pipe', 'pipe'],
      // Isolate POSIX signals; hiding rather than detaching avoids a new Windows console.
      detached: process.platform !== 'win32',
      windowsHide: true,
    });
    this.child.stdout.setEncoding('utf8');
    this.child.stdout.on('data', (chunk: string) => {
      if (this.protocolFailed) return;
      let messages: Received[];
      try {
        messages = this.decoder.push(chunk);
      } catch (error) {
        this.protocolError(error as Error);
        return;
      }
      for (const message of messages) options.onMessage(message);
    });
    this.child.stdout.on('end', () => {
      if (this.protocolFailed) return;
      try {
        this.decoder.finish();
      } catch (error) {
        this.protocolError(error as Error);
      }
    });
    this.child.stderr.setEncoding('utf8');
    this.child.stderr.on('data', (chunk: string) => {
      this.stderr.push(chunk);
      if (this.stderr.length > 50) this.stderr.shift();
    });
    this.child.on('error', options.onProcessError);
    this.child.stdin.on('error', options.onProcessError);
    this.closed = new Promise((resolve) => {
      // close follows stdout/stderr draining; exit can precede the last event.
      this.child.on('close', (code, signal) => {
        this.ended = true;
        clearTimeout(this.killTimer);
        resolve({ code, signal });
        options.onExit(code, signal);
      });
    });
  }

  send(message: Sent): void {
    if (this.closing || this.ended || !this.child.stdin.writable) {
      throw new Error('Bridge input is closed');
    }
    this.child.stdin.write(`${JSON.stringify(message)}\n`);
  }

  cancel(turnId: string): void {
    this.send({ type: 'cancel', turn_id: turnId } as Sent);
  }

  shutdown(): Promise<BridgeExit> {
    if (this.closing || this.ended) return this.closed;
    if (this.child.stdin.writable) {
      this.child.stdin.end(`${JSON.stringify({ type: 'shutdown' })}\n`);
    }
    this.closing = true;
    this.killTimer = setTimeout(() => this.kill(), 2000);
    this.killTimer.unref();
    return this.closed;
  }

  diagnostics(): string {
    return this.stderr.join('');
  }

  private protocolError(error: Error): void {
    this.protocolFailed = true;
    this.options.onProtocolError(error);
    void this.shutdown();
  }

  private kill(): void {
    if (this.ended || this.child.exitCode !== null || this.child.signalCode !== null) return;
    const pid = this.child.pid;
    if (pid === undefined) return;
    if (process.platform === 'win32') {
      spawn('taskkill', ['/F', '/T', '/PID', String(pid)], {
        windowsHide: true, stdio: 'ignore',
      }).on('error', this.options.onProcessError);
    } else {
      try {
        process.kill(-pid, 'SIGKILL');
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code !== 'ESRCH') this.options.onProcessError(error as Error);
      }
    }
  }
}
