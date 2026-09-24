import { useCallback, useEffect, useState } from "react";
import { LoaderCircle, Square } from "lucide-react";
import type { PlanSnapshot, RuntimePhase, UserQuestion } from "@nosis/protocol";
import { BackgroundJobs } from "./BackgroundJobs";
import { PlanStatus } from "./PlanStatus";
import { updateRuntimeIndicatorOrder, type Approval, type BackgroundJob, type RuntimeIndicator } from "../runtimeState";

export { ContextWindowIndicator } from "./ContextWindow";

export function RuntimeIndicators({
  running,
  reconnecting,
  runtimePhase,
  pendingSteers,
  approval,
  question,
  plan,
  jobs,
  onStop,
}: {
  running: boolean;
  reconnecting: boolean;
  runtimePhase: RuntimePhase;
  pendingSteers: number;
  approval: Approval | null;
  question: UserQuestion | null;
  plan: PlanSnapshot | null;
  jobs: BackgroundJob[];
  onStop: () => void;
}) {
  const interactionActive = approval !== null || question !== null;
  const [popover, setPopover] = useState<RuntimeIndicator | null>(null);
  const [order, setOrder] = useState<RuntimeIndicator[]>(() => plan ? ["plan"] : []);
  const hasJobs = jobs.length > 0;
  const setPlanOpen = useCallback((open: boolean) => setPopover(open ? "plan" : null), []);
  const setJobsOpen = useCallback((open: boolean) => setPopover(open ? "jobs" : null), []);

  useEffect(() => {
    setOrder((current) => updateRuntimeIndicatorOrder(current, plan !== null, hasJobs));
  }, [hasJobs, plan]);

  useEffect(() => {
    if (
      interactionActive
      || reconnecting
      || (popover === "plan" && plan === null)
      || (popover === "jobs" && !hasJobs)
    ) setPopover(null);
  }, [hasJobs, interactionActive, plan, popover, reconnecting]);

  if (!running && !plan) return null;
  return <div className="activity">
    {running && <LoaderCircle size={13} className="spin" />}
    <span className="activity-label" role="status">{running
      ? reconnecting
        ? "连接中断，任务仍在后台运行，正在重新连接…"
        : runtimePhase === "starting"
          ? "正在启动 Agent…"
          : approval
            ? "等待你的确认"
            : question
              ? "等待你的选择"
              : pendingSteers
                ? `Nosis 正在处理… ${pendingSteers} 条引导待应用`
                : "Nosis 正在处理…"
      : "Plan 尚未完成"}</span>
    <div className="activity-controls">
      {order.map((item) => item === "plan"
        ? plan && <PlanStatus key="plan" plan={plan} open={popover === "plan" && !interactionActive} disabled={interactionActive} onOpenChange={setPlanOpen} />
        : hasJobs && <BackgroundJobs key="jobs" jobs={jobs} open={popover === "jobs" && !interactionActive && !reconnecting} disabled={interactionActive || reconnecting} onOpenChange={setJobsOpen} />)}
      {running && !interactionActive && <button className="stop-button" aria-label="停止执行" onClick={onStop}><Square size={11} /> 停止</button>}
    </div>
  </div>;
}
