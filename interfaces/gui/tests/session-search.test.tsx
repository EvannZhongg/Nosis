// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SessionSearch } from "../src/SessionSearch";
import type { SessionSearchMatch } from "../src/api";

const match: SessionSearchMatch = {
  session_id: "history", title: "历史会话", workspace: "F:\\Project", scratch: false,
  item_index: 4, role: "assistant", timestamp_utc: "2026-10-26T01:00:00Z",
  has_more_in_session: false,
  snippet: "找到 needle 正文", match_start: 3, match_end: 9,
  prefix_omitted: false, suffix_omitted: true,
};

describe("session search popover", () => {
  let container: HTMLDivElement;
  let root: Root;
  const onSelect = vi.fn();
  const fetchMock = vi.fn();

  beforeEach(async () => {
    vi.useFakeTimers();
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    vi.stubGlobal("fetch", fetchMock);
    HTMLElement.prototype.scrollIntoView = vi.fn();
    fetchMock.mockReset().mockResolvedValue({ ok: true, json: async () => ({ matches: [match], has_more: false }) });
    onSelect.mockReset().mockResolvedValue(true);
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
    await act(async () => root.render(<SessionSearch collapsed={false} onSelect={onSelect} />));
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  async function open() {
    await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="搜索会话"]')!.click());
  }

  async function type(text: string) {
    await act(async () => {
      const input = container.querySelector<HTMLInputElement>("input")!;
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, text);
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
  }

  async function search() {
    await type("needle");
    await act(async () => { await vi.advanceTimersByTimeAsync(250); });
  }

  it("focuses the input, dismisses outside, and keeps the query when reopened in the collapsed rail", async () => {
    await open();
    expect(document.activeElement).toBe(container.querySelector("input"));
    expect(fetchMock).not.toHaveBeenCalled();
    await search();
    await act(async () => container.querySelector('[role="dialog"]')!.dispatchEvent(new Event("pointerdown", { bubbles: true })));
    expect(container.querySelector('[role="dialog"]')).not.toBeNull();
    await act(async () => document.body.dispatchEvent(new Event("pointerdown", { bubbles: true })));
    expect(container.querySelector('[role="dialog"]')).toBeNull();
    await act(async () => root.render(<SessionSearch collapsed onSelect={onSelect} />));
    await open();
    expect(container.querySelector<HTMLInputElement>("input")!.value).toBe("needle");
    await act(async () => document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
    expect(container.querySelector('[role="dialog"]')).toBeNull();
    expect(document.activeElement).toBe(container.querySelector('[aria-label="搜索会话"]'));
  });

  it("groups matching messages and uses keyboard selection to open the exact source item", async () => {
    const second = { ...match, item_index: 9, role: "user" };
    fetchMock.mockResolvedValue({ ok: true, json: async () => ({ matches: [match, second], has_more: true }) });
    await open();
    await search();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toBe("/api/sessions/search?q=needle&limit=50&per_session_limit=3");
    expect(container.querySelectorAll('[role="group"]')).toHaveLength(1);
    expect(container.querySelectorAll('[role="option"]')).toHaveLength(2);
    expect(Array.from(container.querySelectorAll(".session-search-source"), (source) => source.textContent)).toEqual(["Project/历史会话"]);
    expect(container.querySelector(".session-search-source")!.getAttribute("title")).toBe("F:\\Project / 历史会话");
    expect(container.querySelector("mark")!.textContent).toBe("needle");
    expect(container.textContent).toContain("Project");
    expect(container.textContent).toContain("前 50 条");
    await act(async () => container.querySelector("input")!.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true })));
    expect(container.querySelectorAll('[role="option"]')[1].getAttribute("aria-selected")).toBe("true");
    await act(async () => container.querySelector("input")!.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true })));
    expect(onSelect).toHaveBeenCalledWith(second);
    expect(container.querySelector('[role="dialog"]')).toBeNull();
  });

  it("aborts a stale search and ignores its eventual response", async () => {
    let resolveFirst!: (value: unknown) => void;
    fetchMock.mockImplementationOnce(() => new Promise((resolve) => { resolveFirst = resolve; }));
    await open();
    await search();
    const signal = fetchMock.mock.calls[0][1].signal as AbortSignal;
    await type("other");
    expect(signal.aborted).toBe(true);
    fetchMock.mockResolvedValue({ ok: true, json: async () => ({ matches: [], has_more: false }) });
    await act(async () => { await vi.advanceTimersByTimeAsync(250); });
    await act(async () => resolveFirst({ ok: true, json: async () => ({ matches: [match], has_more: false }) }));
    expect(container.querySelectorAll('[role="option"]')).toHaveLength(0);
    expect(container.textContent).toContain("未找到相关对话");
  });

  it("shows one heading per session and navigates between capped previews across groups", async () => {
    const previews = [0, 1, 2].map((item_index) => ({ ...match, item_index, has_more_in_session: true }));
    const other = { ...match, session_id: "other", title: "另一个会话", scratch: true };
    fetchMock.mockResolvedValue({ ok: true, json: async () => ({ matches: [...previews, other], has_more: false }) });
    await open();
    await search();
    const groups = container.querySelectorAll('[role="group"]');
    expect(groups).toHaveLength(2);
    expect(groups[0].querySelectorAll(".session-search-source")).toHaveLength(1);
    expect(groups[0].querySelectorAll('[role="option"]')).toHaveLength(3);
    expect(groups[0].textContent).toContain("仅显示前 3 条命中");
    expect(groups[1].querySelector(".session-search-source")!.textContent).toBe("Chat/另一个会话");
    expect(groups[1].textContent).not.toContain("仅显示前 3 条命中");
    await act(async () => {
      const input = container.querySelector("input")!;
      for (let index = 0; index < 3; index++) input.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }));
    });
    await act(async () => container.querySelector("input")!.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true })));
    expect(onSelect).toHaveBeenCalledWith(other);
  });

  it("keeps results available when opening a result fails", async () => {
    onSelect.mockResolvedValue(false);
    await open();
    await search();
    await act(async () => container.querySelector<HTMLButtonElement>('[role="option"]')!.click());
    expect(container.querySelector('[role="dialog"]')).not.toBeNull();
    expect(container.querySelector('[role="alert"]')!.textContent).toContain("无法打开会话");
  });
});
