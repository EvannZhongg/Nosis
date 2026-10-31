import { afterAll, beforeEach, describe, expect, it, vi } from "vitest";
import { Readable, Writable } from "node:stream";

const mocks = vi.hoisted(() => ({
  handlers: {} as Record<string, (...args: any[]) => any>,
  input: {} as Record<string, (...args: any[]) => any>,
  create: vi.fn(), image: vi.fn(), file: vi.fn(), resource: vi.fn(),
  read: vi.fn(), write: vi.fn(), output: [] as any[],
}));

vi.mock("@larksuiteoapi/node-sdk", () => ({
  Client: class { im = { v1: {
    message: { create: mocks.create }, image: { create: mocks.image },
    file: { create: mocks.file }, messageResource: { get: mocks.resource },
  } }; },
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
