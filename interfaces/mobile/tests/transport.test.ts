import { afterAll, beforeEach, describe, expect, it, vi } from "vitest";
import { Readable, Writable } from "node:stream";

const mocks = vi.hoisted(() => ({
  handlers: {} as Record<string, (...args: any[]) => any>,
  input: {} as Record<string, (...args: any[]) => any>,
  create: vi.fn(), image: vi.fn(), file: vi.fn(), resource: vi.fn(),
  card: vi.fn(), content: vi.fn(), settings: vi.fn(), react: vi.fn(), unreact: vi.fn(),
  read: vi.fn(), write: vi.fn(), output: [] as any[],
}));

vi.mock("@larksuiteoapi/node-sdk", () => ({
  Client: class { im = { v1: {
    message: { create: mocks.create }, image: { create: mocks.image },
    file: { create: mocks.file }, messageResource: { get: mocks.resource },
    messageReaction: { create: mocks.react, delete: mocks.unreact },
  } }; cardkit = { v1: { card: { create: mocks.card, settings: mocks.settings },
    cardElement: { content: mocks.content } } }; },
  EventDispatcher: class {
    register(handlers: typeof mocks.handlers) { mocks.handlers = handlers; return this; }
  },
  WSClient: class {
    constructor(config: any) { queueMicrotask(config.onReady); }
    async start() {}
  },
}));
vi.mock("node:readline", () => ({
  createInterface: () => ({ on: (event: string, callback: any) => { mocks.input[event] = callback; } }),
}));
vi.mock("node:fs", () => ({ createReadStream: mocks.read, createWriteStream: mocks.write }));

// Capture only the JSON transport output, leaving test runner output intact.
const originalWrite = process.stdout.write.bind(process.stdout);
const stdout = vi.spyOn(process.stdout, "write").mockImplementation(((chunk: any, ...args: any[]) => {
  if (typeof chunk === "string" && chunk.startsWith('{"')) {
    mocks.output.push(JSON.parse(chunk));
    return true;
  }
  return (originalWrite as any)(chunk, ...args);
}) as any);
const { parseMessage } = await import("../feishu.mjs");
afterAll(() => stdout.mockRestore());

async function command(op: string, fields = {}) {
  const id = `request-${mocks.output.length}`;
  mocks.input.line(JSON.stringify({ id, op, ...fields }));
  await vi.waitFor(() => expect(mocks.output.some(item => item.id === id)).toBe(true));
  return mocks.output.find(item => item.id === id);
}

beforeEach(async () => {
  vi.clearAllMocks();
  mocks.output.length = 0;
  await command("start", { app_id: "app", app_secret: "secret" });
  mocks.create.mockResolvedValue({ code: 0, data: { message_id: "sent" } });
  mocks.card.mockResolvedValue({ code: 0, data: { card_id: "card" } });
  mocks.content.mockResolvedValue({ code: 0 });
  mocks.settings.mockResolvedValue({ code: 0 });
  mocks.react.mockResolvedValue({ code: 0, data: { reaction_id: "reaction" } });
  mocks.unreact.mockResolvedValue({ code: 0 });
});

describe("Feishu streaming cards and receipts", () => {
  it("creates a JSON 2.0 card and sends its entity reference", async () => {
    expect((await command("create_streaming_card", { chat_id: "chat", text: "正文" })).result).toEqual({ card_id: "card" });
    const card = JSON.parse(mocks.card.mock.calls[0][0].data.data);
    expect(card).toEqual({ schema: "2.0", config: { streaming_mode: true, update_multi: true },
      body: { elements: [{ tag: "markdown", element_id: "body", content: "正文" }] } });
    expect(mocks.create).toHaveBeenCalledWith({ params: { receive_id_type: "chat_id" }, data: {
      receive_id: "chat", msg_type: "interactive", content: JSON.stringify({ type: "card", data: { card_id: "card" } }),
    } });
  });

  it("streams cumulative text and closes using strictly ordered card operations", async () => {
    await command("stream_text", { card_id: "card", text: "全部正文", sequence: 41 });
    await command("stream_settings", { card_id: "card", streaming: false, sequence: 42 });
    expect(mocks.content).toHaveBeenCalledWith({ path: { card_id: "card", element_id: "body" },
      data: { content: "全部正文", sequence: 41 } });
    expect(mocks.settings).toHaveBeenCalledWith({ path: { card_id: "card" },
      data: { settings: JSON.stringify({ config: { streaming_mode: false } }), sequence: 42 } });
  });

  it("never sends a message if card creation fails", async () => {
    mocks.card.mockResolvedValue({ code: 999, msg: "no permission" });
    expect((await command("create_streaming_card", { chat_id: "chat", text: "reply" })).error).toContain("no permission");
    expect(mocks.create).not.toHaveBeenCalled();
  });

  it("adds and removes only the bot's reaction on the original message", async () => {
    expect((await command("react", { message_id: "incoming", emoji: "OnIt" })).result).toEqual({ reaction_id: "reaction" });
    await command("unreact", { message_id: "incoming", reaction_id: "reaction" });
    expect(mocks.react).toHaveBeenCalledWith({ path: { message_id: "incoming" }, data: { reaction_type: { emoji_type: "OnIt" } } });
    expect(mocks.unreact).toHaveBeenCalledWith({ path: { message_id: "incoming", reaction_id: "reaction" } });
  });

  it("uses the installed SDK CardKit and reaction endpoints", async () => {
    const sdk = await vi.importActual<typeof import("@larksuiteoapi/node-sdk")>("@larksuiteoapi/node-sdk");
    const request = vi.fn().mockResolvedValue({ code: 0, data: { card_id: "card" } });
    const client = new sdk.Client({ appId: "app", appSecret: "secret", httpInstance: { request } as any });
    vi.spyOn(client as any, "formatPayload").mockImplementation(async (payload: any) => ({ headers: {}, params: {}, ...payload }));
    expect(await client.cardkit.v1.card.create({ data: { type: "card_json", data: "{}" } })).toEqual({ code: 0, data: { card_id: "card" } });
    await client.cardkit.v1.cardElement.content({ path: { card_id: "card", element_id: "body" }, data: { content: "text", sequence: 1 } });
    expect(request.mock.calls.at(-1)?.[0].url).toContain("/cardkit/v1/cards/card/elements/body/content");
    await client.cardkit.v1.card.settings({ path: { card_id: "card" }, data: { settings: "{}", sequence: 2 } });
    expect(request.mock.calls.at(-1)?.[0].url).toContain("/cardkit/v1/cards/card/settings");
    await client.im.v1.messageReaction.create({ path: { message_id: "incoming" }, data: { reaction_type: { emoji_type: "OnIt" } } });
    expect(request.mock.calls.at(-1)?.[0].url).toContain("/im/v1/messages/incoming/reactions");
  });
});

describe("Feishu incoming content", () => {
  it("preserves text, file names, and resource keys", () => {
    const parse = (message_type: string, content: object) => parseMessage({ message_type, content: JSON.stringify(content) });
    expect(parse("text", { text: "hello" })).toEqual({ text: "hello", attachments: [] });
    expect(parse("image", { image_key: "img" }).attachments).toEqual([{ type: "image", key: "img", filename: "image" }]);
    expect(parse("file", { file_key: "file", file_name: "报告.pdf" })).toEqual({
      text: "", attachments: [{ type: "file", key: "file", filename: "报告.pdf" }],
    });
    expect(parse("sticker", {})).toEqual({ text: null, attachments: [] });
  });

  it("collects rich text and images in message order without duplicate downloads", () => {
    expect(parseMessage({ message_type: "post", content: JSON.stringify({
      title: "分析", content: [
        [{ tag: "text", text: "图一" }, { tag: "img", image_key: "img1" }],
        [{ tag: "img", image_key: "img2" }, { tag: "img", image_key: "img1" }, { tag: "md", text: "**说明**" }],
      ],
    }) })).toEqual({ text: "分析\n图一\n**说明**", attachments: [
      { type: "image", key: "img1", filename: "image" },
      { type: "image", key: "img2", filename: "image" },
    ] });
  });

  it("forwards attachments only for user direct messages", () => {
    const data = { sender: { sender_type: "user", sender_id: { open_id: "owner" } },
      message: { chat_type: "p2p", chat_id: "chat", message_id: "incoming", message_type: "image", content: '{"image_key":"img"}' } };
    mocks.handlers["im.message.receive_v1"](data);
    expect(mocks.output.at(-1)).toMatchObject({ type: "message", message_id: "incoming", text: "", attachments: [{ key: "img" }] });
    const count = mocks.output.length;
    mocks.handlers["im.message.receive_v1"]({ ...data, message: { ...data.message, chat_type: "group" } });
    expect(mocks.output).toHaveLength(count);
  });
});

describe("Feishu resource transport", () => {
  it("uses the installed SDK's unwrapped upload response", async () => {
    const sdk = await vi.importActual<typeof import("@larksuiteoapi/node-sdk")>("@larksuiteoapi/node-sdk");
    const request = vi.fn();
    const client = new sdk.Client({ appId: "app", appSecret: "secret", httpInstance: { request } as any });
    vi.spyOn(client as any, "formatPayload").mockResolvedValue({ headers: {}, params: {}, data: {}, path: {} });
    request.mockResolvedValue({ code: 0, data: { image_key: "image" } });
    expect(await client.im.v1.image.create({ data: { image_type: "message", image: "bytes" } })).toEqual({ image_key: "image" });
    request.mockResolvedValue({ code: 0, data: { file_key: "file" } });
    expect(await client.im.v1.file.create({ data: { file_type: "stream", file_name: "file", file: "bytes" } })).toEqual({ file_key: "file" });
  });

  it("stops oversized resources before writing them to disk", async () => {
    let bytes = 0;
    mocks.write.mockReturnValue(new Writable({ write(chunk, encoding, done) { bytes += chunk.length; done(); } }));
    mocks.resource.mockResolvedValue({ getReadableStream: () => Readable.from([Buffer.alloc(51 * 1024 * 1024)]) });
    expect((await command("download_attachment", { message_id: "incoming", key: "key", resource_type: "file", path: "staged" })).error).toContain("50 MiB");
    expect(bytes).toBe(0);
  });
  it.each(["image", "file"])("downloads %s from the originating message", async resource_type => {
    let bytes = "";
    mocks.write.mockReturnValue(new Writable({ write(chunk, encoding, done) { bytes += chunk; done(); } }));
    mocks.resource.mockResolvedValue({ getReadableStream: () => Readable.from([Buffer.from("resource")]) });
    const result = await command("download_attachment", { message_id: "incoming", key: "key", resource_type, path: "staged" });
    expect(result.error).toBeUndefined();
    expect(mocks.resource).toHaveBeenCalledWith({ path: { message_id: "incoming", file_key: "key" }, params: { type: resource_type } });
    expect(mocks.write).toHaveBeenCalledWith("staged");
    expect(bytes).toBe("resource");
  });

  it("reports a failed download stream", async () => {
    mocks.write.mockReturnValue(new Writable({ write(chunk, encoding, done) { done(); } }));
    mocks.resource.mockResolvedValue({ getReadableStream: () => new Readable({ read() { this.destroy(new Error("download failed")); } }) });
    expect((await command("download_attachment", { message_id: "incoming", key: "key", resource_type: "file", path: "staged" })).error).toBe("download failed");
  });

  it("preserves Feishu error codes from binary endpoint error responses", async () => {
    mocks.resource.mockRejectedValue({ message: "Request failed with status code 400", response: {
      data: Readable.from([Buffer.from('{"code":234001,"msg":"resource unavailable"}')]),
    } });
    expect((await command("download_attachment", { message_id: "incoming", key: "key", resource_type: "file", path: "staged" })).error).toBe("Feishu 234001: resource unavailable");
  });

  it.each(["image", "file"])("uploads and sends a %s attachment", async attachment_type => {
    mocks.read.mockReturnValue("file-stream");
    mocks.image.mockResolvedValue({ image_key: "uploaded-image" });
    mocks.file.mockResolvedValue({ file_key: "uploaded-file" });
    const result = await command("send_attachment", { attachment_type, chat_id: "chat", path: "local", filename: "report.pdf" });
    expect(result.result).toEqual({ message_id: "sent" });
    const data = mocks.create.mock.calls[0][0].data;
    expect(data.msg_type).toBe(attachment_type);
    expect(JSON.parse(data.content)).toEqual(attachment_type === "image" ? { image_key: "uploaded-image" } : { file_key: "uploaded-file" });
    if (attachment_type === "file") expect(mocks.file).toHaveBeenCalledWith({ data: { file_type: "stream", file_name: "report.pdf", file: "file-stream" } });
  });

  it("propagates upload errors and never sends a broken attachment", async () => {
    mocks.image.mockRejectedValue({ message: "Request failed with status code 400", response: { data: { code: 999, msg: "no permission" } } });
    expect((await command("send_attachment", { attachment_type: "image", path: "local" })).error).toContain("no permission");
    expect(mocks.create).not.toHaveBeenCalled();
  });

  it("rejects an upload without a resource key", async () => {
    mocks.file.mockResolvedValue(null);
    expect((await command("send_attachment", { attachment_type: "file", path: "local" })).error).toContain("no resource key");
    expect(mocks.create).not.toHaveBeenCalled();
  });
});
