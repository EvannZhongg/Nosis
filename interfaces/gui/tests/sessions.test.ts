import { describe, expect, it } from "vitest";
import { addSessionSummary } from "../src/sessions";

describe("addSessionSummary", () => {
  it("adds a new session at the front of its workspace", () => {
    const existing = {
      workspace: "C:\\work",
      sessions: [{ session_id: "old", title: "Old chat" }],
    };

    expect(addSessionSummary([existing], "C:\\work", { session_id: "new", title: "New chat" })).toEqual([
      {
        workspace: "C:\\work",
        sessions: [
          { session_id: "new", title: "New chat" },
          { session_id: "old", title: "Old chat" },
        ],
      },
    ]);
  });

  it("adds a new workspace at the front", () => {
    const existing = [{ workspace: "C:\\old", sessions: [] }];

    expect(addSessionSummary(existing, "C:\\new", { session_id: "new", title: "Hello" })).toEqual([
      { workspace: "C:\\new", sessions: [{ session_id: "new", title: "Hello" }] },
      ...existing,
    ]);
  });

  it("does not duplicate a session already returned by storage", () => {
    const groups = [{
      workspace: "C:\\work",
      sessions: [{ session_id: "same", title: "Stored title" }],
    }];

    expect(addSessionSummary(groups, "C:\\work", { session_id: "same", title: "Optimistic title" })).toBe(groups);
  });
});
