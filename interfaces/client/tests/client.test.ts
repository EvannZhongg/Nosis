import { describe, expect, it, vi } from 'vitest';
import { BridgeClient, type BridgeOptions } from '../src/client.js';
import { MessageDecoder } from '@nosis/protocol';

function launch(script: string, callbacks: Partial<BridgeOptions> = {}) {
  const options: BridgeOptions = {
    command: process.execPath, args: ['--input-type=module', '-e', script],
    onMessage: vi.fn(), onExit: vi.fn(), onProtocolError: vi.fn(), onProcessError: vi.fn(),
    ...callbacks,
  };
  return { client: new BridgeClient(options), options };
}

describe('Bridge transport', () => {
  it('preserves fragmented UTF-8, large checkpoints, unknown events and final event order', async () => {
    const received: unknown[] = [];
    const { client, options } = launch(`
      const bytes = Buffer.from(JSON.stringify({type:'channel_event',channel_id:'main',event:{type:'future_event', text:'中文🙂'}})+'\\n');
      for (const byte of bytes) process.stdout.write(Buffer.from([byte]));
      console.error('diagnostic only');
      process.stdout.write(JSON.stringify({type:'channel_event',channel_id:'main',event:{type:'session_items', items:[{role:'user',content:'x'.repeat(100000)}]}})+'\\n');
    `, {
      onMessage: message => received.push(message),
      onExit: () => received.push('closed'),
    });
    expect(await client.closed).toEqual({ code: 0, signal: null });
    expect(received[0]).toEqual({ type: 'future_event', text: '中文🙂' });
    expect((received[1] as any).items[0].content).toHaveLength(100000);
    expect(received[2]).toBe('closed');
    expect(client.diagnostics()).toContain('diagnostic only');
    expect(options.onProtocolError).not.toHaveBeenCalled();
  });

  it('sends turn cancellation and closes idempotently while draining final messages', async () => {
    let client: BridgeClient;
    const received: unknown[] = [];
    const launched = launch(`
      import {createInterface} from 'node:readline';
      const lines=createInterface({input:process.stdin});
      lines.on('line', line => {
        const packet=JSON.parse(line);
        const message=packet.command;
        if(message?.type==='cancel') console.log(JSON.stringify({type:'channel_event',channel_id:packet.channel_id,event:{type:'turn_cancelled',turn_id:message.turn_id}}));
        if(packet.type==='shutdown') { console.log(JSON.stringify({type:'channel_event',channel_id:'main',event:{type:'future_final'}})); lines.close(); process.stdin.destroy(); }
      });
    `, { onMessage: message => {
      received.push(message);
      if (message.type === 'turn_cancelled') void client.shutdown();
    } });
    client = launched.client;
    client.cancel('turn-1', 'main');
    expect(await client.closed).toEqual({ code: 0, signal: null });
    expect(received).toEqual([{ type: 'turn_cancelled', turn_id: 'turn-1' }, { type: 'future_final' }]);
    expect(client.shutdown()).toBe(client.closed);
    expect(() => client.cancel('stale', 'main')).toThrow('Bridge input is closed');
  });

  it.each(['not JSON\n', '[]\n', '{"type":"partial"}'])(
    'reports invalid/truncated output and completes shutdown: %j', async output => {
      const { client, options } = launch(`process.stdout.write(${JSON.stringify(output)});`);
      await client.closed;
      expect(options.onProtocolError).toHaveBeenCalledTimes(1);
    },
  );

  it('reports spawn failure without an unhandled error', async () => {
    const { client, options } = launch('', { command: 'nosis-missing-executable-test' });
    const result = await client.closed;
    expect(result.code).not.toBe(0);
    expect(options.onProcessError).toHaveBeenCalledTimes(1);
    expect(options.onExit).toHaveBeenCalledTimes(1);
  });

  it('routes multiple independent channels and closes one without stopping the process', async () => {
    const received: unknown[] = [];
    let client: BridgeClient;
    const launched = launch(`
      import {createInterface} from 'node:readline';
      const lines=createInterface({input:process.stdin});
      console.log(JSON.stringify({type:'bridge_ready',protocol_version:3}));
      lines.on('line', line => {
        const packet=JSON.parse(line);
        if(packet.type==='channel_command') console.log(JSON.stringify({type:'channel_event',channel_id:packet.channel_id,event:packet.command}));
        if(packet.type==='close_channel') console.log(JSON.stringify({type:'channel_closed',channel_id:packet.channel_id}));
        if(packet.type==='shutdown') { lines.close(); process.stdin.destroy(); }
      });
    `, {
      onReady: () => {
        client.cancel('same-turn', 'one');
        client.cancel('same-turn', 'two');
        client.closeChannel('one');
      },
      onMessage: (event, channelId) => received.push([channelId, event]),
      onChannelClosed: channelId => {
        received.push(['closed', channelId]);
        void client.shutdown();
      },
    });
    client = launched.client;
    expect(await client.closed).toEqual({ code: 0, signal: null });
    expect(received).toEqual([
      ['one', { type: 'cancel', turn_id: 'same-turn' }],
      ['two', { type: 'cancel', turn_id: 'same-turn' }],
      ['closed', 'one'],
    ]);
  });

  it('bounds shutdown of an unresponsive process', async () => {
    const { client, options } = launch('setInterval(() => {}, 1000);');
    const closing = client.shutdown();
    expect(client.shutdown()).toBe(closing);
    const result = await closing;
    expect(result.code !== 0 || result.signal !== null).toBe(true);
    expect(options.onExit).toHaveBeenCalledTimes(1);
  }, 10000);
});

describe('MessageDecoder EOF', () => {
  it('accepts CRLF and blank lines and requires the last newline', () => {
    const decoder = new MessageDecoder();
    expect(decoder.push('\r\n{"type":"future"}\r\n')).toEqual([{ type: 'future' }]);
    expect(() => decoder.finish()).not.toThrow();
    decoder.push('{"type":"future"}');
    expect(() => decoder.finish()).toThrow('final newline');
  });
});
