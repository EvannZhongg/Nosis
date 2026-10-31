import * as lark from '@larksuiteoapi/node-sdk';
import { createInterface } from 'node:readline';
import { createReadStream, createWriteStream } from 'node:fs';
import { pipeline } from 'node:stream/promises';

const emit = value => process.stdout.write(`${JSON.stringify(value)}\n`);
let client;
let socket;
let secret = '';
const log = (...values) => process.stderr.write(`${values.map(value =>
  typeof value === 'string' ? value.replaceAll(secret || '\0', '[redacted]') : '[SDK diagnostic]'
).join(' ')}\n`);
const logger = { trace() {}, debug() {}, info() {}, warn: log, error: log };
const markdownContent = text => JSON.stringify({
  zh_cn: { content: [[{ tag: 'md', text }]] },
});

export function parseMessage(message) {
  const content = JSON.parse(message.content);
  const attachments = [];
  let text = null;
  if (message.message_type === 'text') {
    text = content.text;
  } else if (message.message_type === 'image') {
    text = '';
    attachments.push({ type: 'image', key: content.image_key, filename: 'image' });
  } else if (message.message_type === 'file') {
    text = '';
    attachments.push({ type: 'file', key: content.file_key, filename: content.file_name });
  } else if (message.message_type === 'post') {
    const lines = content.title ? [content.title] : [];
    for (const row of content.content) {
      let line = '';
      for (const part of row) {
        if (part.tag === 'img') {
          if (!attachments.some(item => item.key === part.image_key)) {
            attachments.push({ type: 'image', key: part.image_key, filename: 'image' });
          }
        } else if (part.tag === 'a') {
          line += `[${part.text}](${part.href})`;
        } else if (part.tag === 'at') {
          line += `@${part.user_name || part.user_id}`;
        } else if (['text', 'md', 'code_block'].includes(part.tag)) {
          line += part.text;
        }
      }
      lines.push(line);
    }
    text = lines.join('\n').trim();
  }
  return { text, attachments };
}

async function handle(command) {
  try {
    let result;
    if (command.op === 'start') {
      secret = command.app_secret;
      const config = { appId: command.app_id, appSecret: secret, logger };
      client = new lark.Client(config);
      const dispatcher = new lark.EventDispatcher({ logger }).register({
        'im.message.receive_v1': data => {
          if (data.message.chat_type !== 'p2p' || data.sender.sender_type !== 'user') return;
          const message = data.message;
          emit({ type: 'message', chat_id: message.chat_id, user_id: data.sender.sender_id.open_id,
            message_id: message.message_id, ...parseMessage(message) });
        },
        'card.action.trigger': data => {
          emit({ type: 'action', chat_id: data.context.open_chat_id, user_id: data.operator.open_id,
            message_id: data.context.open_message_id, token: data.action.value?.token });
          return { toast: { type: 'info', content: '已收到操作' } };
        },
      });
      await new Promise((resolve, reject) => {
        socket = new lark.WSClient({ ...config, onReady: resolve,
          onError: error => { reject(error); emit({ type:'connection_error', error:'Feishu connection failed' }); } });
        socket.start({ eventDispatcher: dispatcher }).catch(reject);
      });
      result = { connected: true };
    } else if (command.op === 'send') {
      const response = await client.im.v1.message.create({
        params: { receive_id_type: 'chat_id' },
        data: { receive_id: command.chat_id, msg_type: 'interactive', content: JSON.stringify(command.card) },
      });
      if (response.code !== 0) throw new Error(`Feishu ${response.code}: ${response.msg}`);
      result = { message_id: response.data.message_id };
    } else if (command.op === 'update') {
      const response = await client.im.v1.message.patch({
        path: { message_id: command.message_id }, data: { content: JSON.stringify(command.card) },
      });
      if (response.code !== 0) throw new Error(`Feishu ${response.code}: ${response.msg}`);
      result = {};
    } else if (command.op === 'send_markdown') {
      const response = await client.im.v1.message.create({
        params: { receive_id_type: 'chat_id' },
        data: { receive_id: command.chat_id, msg_type: 'post', content: markdownContent(command.text) },
      });
      if (response.code !== 0) throw new Error(`Feishu ${response.code}: ${response.msg}`);
      result = { message_id: response.data.message_id };
    } else if (command.op === 'update_markdown') {
      const response = await client.im.v1.message.update({
        path: { message_id: command.message_id },
        data: { msg_type: 'post', content: markdownContent(command.text) },
      });
      if (response.code !== 0) throw new Error(`Feishu ${response.code}: ${response.msg}`);
      result = {};
    } else if (command.op === 'download_attachment') {
      const response = await client.im.v1.messageResource.get({
        path: { message_id: command.message_id, file_key: command.key },
        params: { type: command.resource_type },
      });
      await pipeline(response.getReadableStream(), createWriteStream(command.path));
      result = {};
    } else if (command.op === 'send_attachment') {
      const image = command.attachment_type === 'image';
      const upload = image
        ? await client.im.v1.image.create({
          data: { image_type: 'message', image: createReadStream(command.path) },
        })
        : await client.im.v1.file.create({
          data: { file_type: 'stream', file_name: command.filename, file: createReadStream(command.path) },
        });
      const key = image ? upload?.image_key : upload?.file_key;
      if (!key) throw new Error('Feishu attachment upload returned no resource key');
      const response = await client.im.v1.message.create({
        params: { receive_id_type: 'chat_id' },
        data: { receive_id: command.chat_id, msg_type: image ? 'image' : 'file',
          content: JSON.stringify(image ? { image_key: key } : { file_key: key }) },
      });
      if (response.code !== 0) throw new Error(`Feishu ${response.code}: ${response.msg}`);
      result = { message_id: response.data.message_id };
    } else if (command.op === 'close') {
      socket?.close({ force: true });
      process.stdout.write(`${JSON.stringify({ id: command.id, result: {} })}\n`, () => process.exit(0));
      return;
    } else {
      throw new Error('Unknown transport command');
    }
    emit({ id: command.id, result });
  } catch (error) {
    let detail = error.response?.data;
    if (detail?.[Symbol.asyncIterator]) {
      const chunks = [];
      for await (const chunk of detail) chunks.push(Buffer.from(chunk));
      try { detail = JSON.parse(Buffer.concat(chunks).toString('utf8')); }
      catch { detail = null; }
    }
    const message = detail?.code ? `Feishu ${detail.code}: ${detail.msg}` : String(error.message);
    emit({ id: command.id, error: message.replaceAll(secret || '\0', '[redacted]') });
  }
}

const input = createInterface({ input: process.stdin });
input.on('line', line => {
  try { void handle(JSON.parse(line)); }
  catch { emit({ type: 'connection_error', error: 'Invalid transport command' }); }
});
input.on('close', () => { socket?.close({ force: true }); process.exit(0); });
