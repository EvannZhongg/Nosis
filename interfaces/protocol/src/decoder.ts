import type { Incoming } from './protocol.js';

/** Frames decoded UTF-8 text; does not interpret Runtime state. */
export class MessageDecoder<Message = Incoming> {
  private buffer = '';

  push(chunk: string): Message[] {
    this.buffer += chunk;
    const lines = this.buffer.split('\n');
    this.buffer = lines.pop() ?? '';
    const messages: Message[] = [];
    for (const line of lines) {
      if (!line.trim()) continue;
      const message: unknown = JSON.parse(line);
      if (message === null || typeof message !== 'object' || Array.isArray(message)
          || !('type' in message) || typeof message.type !== 'string') {
        throw new Error("protocol message must be an object with a string 'type'");
      }
      messages.push(message as Message);
    }
    return messages;
  }

  finish(): void {
    if (this.buffer.trim()) throw new Error('Bridge output ended before the final newline');
    this.buffer = '';
  }
}
