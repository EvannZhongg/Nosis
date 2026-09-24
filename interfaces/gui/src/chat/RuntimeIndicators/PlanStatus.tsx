import { useEffect, useRef } from "react";
import type { PlanSnapshot } from "@nosis/protocol";

export function PlanStatus({ plan, open, disabled, onOpenChange }: {
  plan: PlanSnapshot;
  open: boolean;
  disabled: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const rootRef = useRef<HTMLDivElement | null>(null);
  const completed = plan.steps.filter((step) => step.status === "completed").length;

  useEffect(() => {
    if (!open) return;
    const closeOnOutsidePointer = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) onOpenChange(false);
    };
    const closeOnEscape = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape") onOpenChange(false);
    };
    document.addEventListener("pointerdown", closeOnOutsidePointer);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOnOutsidePointer);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [onOpenChange, open]);

  return <div ref={rootRef} className="plan-status">
    <button type="button" className="runtime-menu-trigger" aria-expanded={open} disabled={disabled} onClick={() => onOpenChange(!open)}>
      Plan {completed}/{plan.steps.length}
    </button>
    {open && <div className="plan-popover" role="dialog" aria-label="任务计划">
      <div className="plan-popover-title">任务计划</div>
      <div className="plan-goal">{plan.goal}</div>
      <div className="plan-steps">{plan.steps.map((step) => {
        const marker = step.status === "completed" ? "✓" : step.status === "in_progress" ? "◉" : step.status === "blocked" ? "×" : "○";
        return <div className={`plan-step ${step.status}`} key={step.id}><span>{marker}</span><span><span className="plan-step-title">{step.title}</span>{step.outcome && <small>{step.outcome}</small>}</span></div>;
      })}</div>
    </div>}
  </div>;
}
