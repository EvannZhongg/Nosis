// @vitest-environment jsdom
import { act, useRef } from "react";
import { createRoot, type Root } from "react-dom/client";
import { AssistantRuntimeProvider, ThreadPrimitive, useExternalStoreRuntime } from "@assistant-ui/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Transcript } from "../src/chat/Transcript";
import { applyMessage, toMessages, type TranscriptItem } from "../src/transcript";
import type { Incoming } from "@nosis/protocol";

function TestTranscript({ items, running }: { items: TranscriptItem[]; running: boolean }) {
  const viewportRef = useRef<HTMLDivElement>(null);
  const messages = toMessages(items);
  const runtime = useExternalStoreRuntime({
    messages, convertMessage: (message) => message, isRunning: running, onNew: async () => {},
  });
  return <AssistantRuntimeProvider runtime={runtime}><ThreadPrimitive.Root>
    <Transcript previews={messages.filter((message) => message.role === "user").map(() => "prompt")} viewportRef={viewportRef} active />
  </ThreadPrimitive.Root></AssistantRuntimeProvider>;
}

describe("process folding in the live transcript", () => {
  let container: HTMLDivElement;
  let root: Root;
  let items: TranscriptItem[];
  const scrollIntoView = vi.fn(function (this: HTMLElement) {
    const viewport = this.closest(".thread-viewport")!;
    viewport.scrollTop = 0;
    viewport.dispatchEvent(new Event("scroll"));
  });
  const scrollTo = vi.fn(function (this: HTMLElement, options: ScrollToOptions) {
    this.scrollTop = Math.min(options.top ?? 0, this.scrollHeight - this.clientHeight);
    this.dispatchEvent(new Event("scroll"));
  });

  beforeEach(() => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
    vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => setTimeout(() => callback(0), 0));
    vi.stubGlobal("cancelAnimationFrame", clearTimeout);
    HTMLElement.prototype.scrollIntoView = scrollIntoView;
    HTMLElement.prototype.scrollTo = scrollTo as HTMLElement["scrollTo"];
    vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockImplementation(function (this: HTMLElement) {
      return 3000 + (this.textContent?.length ?? 0) * 10;
    });
    vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(600);
    scrollIntoView.mockClear();
    scrollTo.mockClear();
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
    items = [{ role: "user", content: "prompt" }];
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  async function render(running = true) {
    await act(async () => {
      root.render(<TestTranscript items={items} running={running} />);
    });
    // Native details toggle events and navigation frames are asynchronous.
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 30)); });
  }

  async function receive(message: Incoming, running = true) {
    items = applyMessage(items, message).items;
    await render(running);
  }

  const processes = () => Array.from(container.querySelectorAll<HTMLDetailsElement>(".turn-process"));
  const reasoning = (text: string): Incoming => ({ type: "reasoning_delta", turn_id: "t1", text, model_call_index: 1 });
  const text = (value: string): Incoming => ({ type: "assistant_delta", turn_id: "t1", text: value, model_call_index: 1 });
  const settled = (content: string): Incoming => ({ type: "assistant_message", turn_id: "t1", content, timestamp_utc: "2026-10-16T12:00:00Z", model_call_index: 1 });

  it("folds each process at the next text part while the turn is still running", async () => {
    await receive(reasoning("first reasoning"));
    expect(processes().map((group) => group.open)).toEqual([true]);
    await receive(text("first answer"));
    expect(processes().map((group) => group.open)).toEqual([false]);
    expect(container.querySelector(".turn-process p")).toBeNull();
    expect(scrollIntoView.mock.instances.at(-1)).toBe(container.querySelector(".user-turn"));
    expect(scrollIntoView).toHaveBeenLastCalledWith({ block: "start" });

    await receive(settled("first answer"));
    await receive({ type: "tool_batch_started", turn_id: "t1", model_call_index: 2, tool_calls: [{ id: "call-1", name: "shell", arguments: { command: "ls" } }] });
    await receive({ type: "tool_result", turn_id: "t1", tool_call_id: "call-1", name: "shell", ok: true, error: null, tool_index: 1, tool_count: 1 });
    expect(processes().map((group) => group.open)).toEqual([false, true]);
    await receive(reasoning("second reasoning"));
    expect(processes().map((group) => group.open)).toEqual([false, true]);
    await receive(text("second answer"));
    expect(processes().map((group) => group.open)).toEqual([false, false]);
    expect(container.textContent).toContain("first answer");
    expect(container.querySelectorAll(".assistant-content > div[data-status]")).toHaveLength(2);
    expect(processes().some((group) => group.textContent?.includes("answer"))).toBe(false);

    scrollTo.mockClear();
    await receive(text(" continues streaming"));
    expect(scrollTo).not.toHaveBeenCalled();
  });

  it.each(["turn_completed", "turn_cancelled", "turn_failed"] as const)("folds a trailing process on %s and navigates to its user", async (type) => {
    await receive(reasoning("reasoning without an answer"));
    const terminal: Incoming = type === "turn_failed"
      ? { type, turn_id: "t1", error: { type: "ProviderError", message: "failed", details: {} } }
      : type === "turn_completed" ? { type, turn_id: "t1", usage: null } : { type, turn_id: "t1" };
    await receive(terminal, false);
    expect(processes()[0]?.open).toBe(false);
    expect(scrollIntoView.mock.instances.at(-1)).toBe(container.querySelector(".user-turn"));
  });

  it("loads history folded without navigation and lets users reopen and close a process", async () => {
    items.push({ role: "assistant", reasoning: "stored reasoning", content: "stored answer" });
    await render(false);
    expect(processes()[0]?.open).toBe(false);
    expect(scrollIntoView).not.toHaveBeenCalled();
    const group = processes()[0]!;
    await act(async () => { group.open = true; await new Promise((resolve) => setTimeout(resolve, 30)); });
    expect(group.open).toBe(true);
    await render(false);
    expect(group.open).toBe(true);
    await act(async () => { group.open = false; await new Promise((resolve) => setTimeout(resolve, 30)); });
    expect(scrollIntoView.mock.instances.at(-1)).toBe(container.querySelector(".user-turn"));
  });

  it("keeps process groups open across images until text arrives", async () => {
    await receive(reasoning("inspect image"));
    await receive({ type: "tool_media", turn_id: "t1", attachments: [{ type: "image", path: "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=", filename: "image.png", mime_type: "image/png", size_bytes: 68 }] });
    expect(processes()[0]?.open).toBe(true);
    expect(scrollIntoView).not.toHaveBeenCalled();
    await receive(text("image description"));
    expect(processes()[0]?.open).toBe(false);
  });

  it("uses the same user target for folding and the turn navigation", async () => {
    items = [
      { role: "user", content: "earlier prompt" },
      { role: "assistant", reasoning: "earlier reasoning", content: "earlier answer" },
      { role: "user", content: "latest prompt" },
      { role: "assistant", content: "latest answer" },
    ];
    await render(false);
    const group = processes()[0]!;
    await act(async () => { group.open = true; await new Promise((resolve) => setTimeout(resolve, 30)); });
    await act(async () => { group.open = false; await new Promise((resolve) => setTimeout(resolve, 30)); });
    const firstUser = container.querySelector(".user-turn");
    expect(scrollIntoView.mock.instances.at(-1)).toBe(firstUser);
    const firstMark = container.querySelector<HTMLButtonElement>(".turn-navigation-mark")!;
    await act(async () => { firstMark.click(); await new Promise((resolve) => setTimeout(resolve, 30)); });
    expect(scrollIntoView.mock.instances.at(-1)).toBe(firstUser);
  });

  it("resumes normal bottom following when the next turn starts", async () => {
    await receive(reasoning("first reasoning"));
    await receive(text("first answer"));
    await receive(settled("first answer"));
    await render(false);
    items.push({ role: "user", content: "next prompt" });
    scrollTo.mockClear();
    await receive(reasoning("next reasoning"));
    expect(scrollTo).toHaveBeenCalled();
    scrollTo.mockClear();
    await receive(reasoning(" continues"));
    expect(scrollTo).toHaveBeenCalled();
  });
});
