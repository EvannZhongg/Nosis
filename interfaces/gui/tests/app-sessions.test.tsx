// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Session, WorkspaceSessions } from "../src/api";
import { App } from "../src/App";

vi.mock("../src/Chat", () => ({
  Chat: ({ session, selected }: { session: Session; selected: boolean }) =>
    <div data-chat={session.session_id} data-selected={selected} data-workspace={session.workspace ?? ""} data-items={session.items.length} />,
}));
vi.mock("../src/Workspace", () => ({ Workspace: () => null }));
vi.mock("../src/Settings", () => ({ Settings: () => null }));

const workspace = "F:\\Project";
const storedSessions: Session[] = ["first", "second"].map((session_id) => ({
  session_id, workspace, permission_preset: "ask_for_approval", items: [],
}));

describe("session deletion navigation", () => {
  let container: HTMLDivElement;
  let root: Root;
  let groups: WorkspaceSessions[];
  const fetchMock = vi.fn();
  const confirmMock = vi.fn();

  beforeEach(() => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    vi.stubGlobal("fetch", fetchMock);
    vi.spyOn(window, "confirm").mockImplementation(confirmMock);
    confirmMock.mockReset().mockReturnValue(true);
    localStorage.clear();
    localStorage.setItem("nosis.selectedSessionId", "first");
    groups = [{ workspace, sessions: storedSessions.map(({ session_id }) => ({ session_id, title: session_id })) }];
    fetchMock.mockReset().mockImplementation(async (url: string, options?: RequestInit) => {
      if (options?.method === "DELETE") {
        const id = url.split("/").pop();
        groups = groups.map((group) => ({ ...group, sessions: group.sessions.filter((session) => session.session_id !== id) }));
        return { ok: true };
      }
      const data = url === "/api/sessions" ? groups
        : url === "/api/models" ? { default: "main", models: [] }
        : url === "/api/active-sessions" ? storedSessions.map((session) => ({ ...session, phase: "idle", provider: null }))
        : storedSessions.find((session) => url === `/api/sessions/${session.session_id}`);
      if (data === undefined) throw new Error(`Unexpected request: ${url}`);
      return { ok: true, json: async () => data };
    });
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    localStorage.clear();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  async function mount() {
    await act(async () => root.render(<App />));
  }

  async function remove(id: string) {
    await act(async () => container.querySelector<HTMLButtonElement>(`[aria-label="删除会话 ${id}"]`)!.click());
  }

  it.each(["first", "second"])("opens a blank New chat after deleting %s, regardless of which session is selected", async (id) => {
    await mount();
    expect(container.querySelector('[data-selected="true"]')!.getAttribute("data-chat")).toBe("first");
    const remainingId = id === "first" ? "second" : "first";
    const remainingChat = container.querySelector(`[data-chat="${remainingId}"]`);
    await remove(id);

    const selected = container.querySelector('[data-selected="true"]')!;
    const selectedId = selected.getAttribute("data-chat");
    expect(selectedId).not.toBe("first");
    expect(selectedId).not.toBe("second");
    expect(selected.getAttribute("data-items")).toBe("0");
    expect(selected.getAttribute("data-workspace")).toBe("");
    expect(container.querySelector(".breadcrumb")!.textContent).toContain("New chat");
    expect(localStorage.getItem("nosis.selectedSessionId")).toBe(selectedId);
    expect(container.querySelector(`[data-chat="${id}"]`)).toBeNull();
    expect(container.querySelector(`[data-chat="${remainingId}"]`)).toBe(remainingChat);
    expect(container.querySelector(`[aria-label="删除会话 ${id}"]`)).toBeNull();
    expect(fetchMock.mock.calls.filter(([, options]) => options?.method === "DELETE").map(([url]) => url)).toEqual([`/api/sessions/${id}`]);
  });

  it("opens a blank chat when the last saved session is deleted", async () => {
    groups[0].sessions = groups[0].sessions.filter((session) => session.session_id === "first");
    await mount();
    await remove("first");
    expect(container.querySelector('[data-selected="true"]')!.getAttribute("data-chat")).not.toBe("first");
    expect(container.querySelector(".breadcrumb")!.textContent).toContain("New chat");
    expect(container.querySelectorAll(".session-row")).toHaveLength(0);
  });

  it("keeps the current chat when deletion is cancelled or fails", async () => {
    await mount();
    confirmMock.mockReturnValueOnce(false);
    await remove("first");
    expect(fetchMock.mock.calls.some(([, options]) => options?.method === "DELETE")).toBe(false);
    fetchMock.mockResolvedValueOnce({ ok: false, status: 409, json: async () => ({ detail: "会话正在运行。" }) });
    await remove("first");
    expect(container.querySelector('[data-selected="true"]')!.getAttribute("data-chat")).toBe("first");
    expect(container.querySelector('[aria-label="删除会话 first"]')).not.toBeNull();
    expect(container.querySelector('[role="alert"]')!.textContent).toContain("会话正在运行。");
  });
});
