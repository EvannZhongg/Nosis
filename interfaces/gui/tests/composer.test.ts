import { describe, expect, it } from "vitest";
import { composerConnectionGate, isRemoteMarkdownImage, shouldBlockRunningAttachmentSubmit, shouldShowPlan, shouldSubmitAttachmentOnly, shouldSubmitComposerEnter, updateBackgroundJobs, updateRuntimeIndicatorOrder, userMessagePreview } from "../src/Chat";

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

describe("Markdown image routing", () => {
  it.each([
    "https://example.com/image.png",
    "http://example.com/image.png",
    "HTTPS://example.com/image.png",
  ])("keeps remote image URLs in the browser: %s", (src) => {
    expect(isRemoteMarkdownImage(src)).toBe(true);
  });

  it.each([
    "docs/image.png",
    "./docs/image.png",
    ".nosis/attachments/image.png",
    "/absolute/image.png",
    "data:image/png;base64,abc",
    undefined,
  ])("routes non-HTTP image sources through the workspace signer: %s", (src) => {
    expect(isRemoteMarkdownImage(src)).toBe(false);
  });
});

describe("background job state", () => {
  it("updates a job in place and removes terminal jobs", () => {
    const submitted = updateBackgroundJobs({}, {
      job_id: "job-1",
      kind: "subagent",
      status: "submitted",
    });
    const running = updateBackgroundJobs(submitted, {
      job_id: "job-1",
      kind: "subagent",
      status: "running",
    });
    const completed = updateBackgroundJobs(running, {
      job_id: "job-1",
      kind: "subagent",
      status: "completed",
    });

    expect(Object.keys(submitted)).toEqual(["job-1"]);
    expect(Object.keys(running)).toEqual(["job-1"]);
    expect(running["job-1"]?.status).toBe("running");
    expect(completed).toEqual({});
  });

  it("keeps concurrent jobs independent", () => {
    const first = updateBackgroundJobs({}, {
      job_id: "job-1",
      kind: "subagent",
      status: "running",
    });
    const both = updateBackgroundJobs(first, {
      job_id: "job-2",
      kind: "shell",
      status: "running",
    });
    const secondOnly = updateBackgroundJobs(both, {
      job_id: "job-1",
      kind: "subagent",
      status: "failed",
    });

    expect(Object.keys(both)).toEqual(["job-1", "job-2"]);
    expect(Object.keys(secondOnly)).toEqual(["job-2"]);
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

describe("attachment-only submission", () => {
  it("submits when attachments are present and text is empty", () => {
    expect(shouldSubmitAttachmentOnly("", 1)).toBe(true);
    expect(shouldSubmitAttachmentOnly("   ", 2)).toBe(true);
  });

  it("leaves text messages and empty composers to the normal submit path", () => {
    expect(shouldSubmitAttachmentOnly("describe this", 1)).toBe(false);
    expect(shouldSubmitAttachmentOnly("", 0)).toBe(false);
  });
});

describe("turn navigation previews", () => {
  it("normalizes user text into a short single-line preview", () => {
    expect(userMessagePreview({ content: [{ type: "text", text: "  分析当前的 GUI\n并增加导航  " }] }, 12))
      .toBe("分析当前的 GUI 并增…");
  });

  it("labels an attachment-only turn", () => {
    expect(userMessagePreview({ content: [{ type: "image", image: "image.png" }] }))
      .toBe("附件消息");
  });
});

describe("composer connection state", () => {
  it("keeps the draft editable while the opening snapshot is pending", () => {
    expect(composerConnectionGate({
      attaching: true,
      configurationPending: false,
      attachmentReplaced: false,
      interactionActive: false,
      backgroundDisconnected: false,
    })).toEqual({ inputDisabled: false, sendDisabled: true });
  });

  it("disables input when another interaction owns it", () => {
    expect(composerConnectionGate({
      attaching: false,
      configurationPending: false,
      attachmentReplaced: false,
      interactionActive: true,
      backgroundDisconnected: false,
    })).toEqual({ inputDisabled: true, sendDisabled: true });
  });

  it("keeps the draft editable but waits for settings to apply before sending", () => {
    expect(composerConnectionGate({
      attaching: false,
      configurationPending: true,
      attachmentReplaced: false,
      interactionActive: false,
      backgroundDisconnected: false,
    })).toEqual({ inputDisabled: false, sendDisabled: true });
  });

  it("allows sending after the connection and settings are ready", () => {
    expect(composerConnectionGate({
      attaching: false,
      configurationPending: false,
      attachmentReplaced: false,
      interactionActive: false,
      backgroundDisconnected: false,
    })).toEqual({ inputDisabled: false, sendDisabled: false });
  });
});

describe("plan visibility", () => {
  it("shows a plan while any step still needs attention", () => {
    expect(shouldShowPlan({
      plan_id: "plan-1",
      goal: "Ship",
      revision: 1,
      steps: [
        { id: "done", title: "Done", status: "completed" },
        { id: "next", title: "Next", status: "pending" },
      ],
    })).toBe(true);
  });

  it("removes a plan from the GUI once every step is completed", () => {
    expect(shouldShowPlan({
      plan_id: "plan-1",
      goal: "Ship",
      revision: 2,
      steps: [
        { id: "done", title: "Done", status: "completed" },
        { id: "next", title: "Next", status: "completed" },
      ],
    })).toBe(false);
  });

  it("keeps a blocked plan visible", () => {
    expect(shouldShowPlan({
      plan_id: "plan-1",
      goal: "Ship",
      revision: 2,
      steps: [{ id: "blocked", title: "Blocked", status: "blocked" }],
    })).toBe(true);
  });
});

describe("runtime indicator order", () => {
  it("appends indicators in appearance order", () => {
    const jobsFirst = updateRuntimeIndicatorOrder([], false, true);
    expect(jobsFirst).toEqual(["jobs"]);
    expect(updateRuntimeIndicatorOrder(jobsFirst, true, true)).toEqual(["jobs", "plan"]);
  });

  it("moves remaining indicators forward and appends reappearing ones", () => {
    const planRemoved = updateRuntimeIndicatorOrder(["plan", "jobs"], false, true);
    expect(planRemoved).toEqual(["jobs"]);
    expect(updateRuntimeIndicatorOrder(planRemoved, true, true)).toEqual(["jobs", "plan"]);
  });

  it("does not reorder indicators while they remain visible", () => {
    const current = ["jobs", "plan"] as const;
    expect(updateRuntimeIndicatorOrder([...current], true, true)).toEqual(current);
  });
});
