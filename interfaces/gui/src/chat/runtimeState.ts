import type { ThreadMessageLike } from "@assistant-ui/react";
import type { Incoming, PlanSnapshot } from "@nosis/protocol";

type JobStatusMessage = Extract<Incoming, { type: "job_status" }>;

export type Approval = {
  requestId: string;
  command: string;
  kind?: "shell" | "mcp";
  server?: string;
  toolName?: string;
};

export type BackgroundJob = Pick<JobStatusMessage, "job_id" | "kind" | "status">;
export type RuntimeIndicator = "plan" | "jobs";

export function updateBackgroundJobs(
  current: Record<string, BackgroundJob>,
  update: BackgroundJob,
): Record<string, BackgroundJob> {
  const next = { ...current };
  if (update.status === "submitted" || update.status === "running") {
    next[update.job_id] = update;
  } else {
    delete next[update.job_id];
  }
  return next;
}

export function shouldShowPlan(plan: PlanSnapshot | null): plan is PlanSnapshot {
  return plan !== null && plan.steps.some((step) => step.status !== "completed");
}

export function updateRuntimeIndicatorOrder(
  current: RuntimeIndicator[],
  planVisible: boolean,
  jobsVisible: boolean,
): RuntimeIndicator[] {
  const visible = new Set<RuntimeIndicator>([
    ...(planVisible ? ["plan" as const] : []),
    ...(jobsVisible ? ["jobs" as const] : []),
  ]);
  const next = current.filter((item) => visible.has(item));
  for (const item of ["plan", "jobs"] as const) {
    if (visible.has(item) && !next.includes(item)) next.push(item);
  }
  return next.length === current.length && next.every((item, index) => item === current[index])
    ? current
    : next;
}

export function userMessagePreview(message: Pick<ThreadMessageLike, "content">, maxLength = 48): string {
  const text = (typeof message.content === "string"
    ? message.content
    : message.content
      .filter((part) => part.type === "text")
      .map((part) => part.text)
      .join(" "))
    .replace(/\s+/g, " ")
    .trim();
  if (!text) return "附件消息";
  const characters = Array.from(text);
  return characters.length > maxLength
    ? `${characters.slice(0, maxLength).join("")}…`
    : text;
}

export function jobKindLabel(kind: string): string {
  if (kind === "subagent") return "子代理";
  if (kind === "shell") return "后台命令";
  return kind;
}
