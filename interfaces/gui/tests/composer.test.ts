import { describe, expect, it } from "vitest";
import { composerConnectionGate, shouldBlockRunningAttachmentSubmit, shouldSubmitAttachmentOnly } from "../src/chat/composerState";
import { shouldShowPlan, updateBackgroundJobs } from "../src/chat/runtimeState";

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
