import { describe, expect, it } from "vitest";
import { shouldBlockRunningAttachmentSubmit, shouldShowPlan, shouldSubmitComposerEnter, updateBackgroundJobs } from "../src/Chat";

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
