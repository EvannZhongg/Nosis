import { describe, expect, it } from "vitest";
import { shouldBlockRunningAttachmentSubmit, shouldSubmitComposerEnter } from "../src/Chat";

const enter = {
  key: "Enter",
  shiftKey: false,
  isComposing: false,
  keyCode: 13,
};

describe("composer Enter handling", () => {
  it("submits a normal Enter", () => {
    expect(shouldSubmitComposerEnter(enter, false)).toBe(true);
  });

  it("does not submit while the browser reports IME composition", () => {
    expect(
      shouldSubmitComposerEnter({ ...enter, isComposing: true }, false),
    ).toBe(false);
  });

  it("does not submit legacy IME keyCode 229", () => {
    expect(shouldSubmitComposerEnter({ ...enter, keyCode: 229 }, false)).toBe(false);
  });

  it("does not submit the Enter immediately following compositionend", () => {
    expect(shouldSubmitComposerEnter(enter, true)).toBe(false);
  });

  it("keeps Shift+Enter as a newline", () => {
    expect(shouldSubmitComposerEnter({ ...enter, shiftKey: true }, false)).toBe(false);
  });
});

describe("running attachment submission", () => {
  it("blocks submission while a turn is running so the composer stays intact", () => {
    expect(shouldBlockRunningAttachmentSubmit(true, 1)).toBe(true);
  });

  it("allows steering without attachments", () => {
    expect(shouldBlockRunningAttachmentSubmit(true, 0)).toBe(false);
  });
});
