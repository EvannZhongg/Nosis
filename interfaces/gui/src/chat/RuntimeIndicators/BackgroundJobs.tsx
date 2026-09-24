import { useEffect, useRef } from "react";
import { LoaderCircle } from "lucide-react";
import { jobKindLabel, type BackgroundJob } from "../runtimeState";

export function BackgroundJobs({ jobs, open, disabled, onOpenChange }: {
  jobs: BackgroundJob[];
  open: boolean;
  disabled: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const rootRef = useRef<HTMLDivElement | null>(null);

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

  if (jobs.length === 0) return null;
  return <div ref={rootRef} className="background-jobs">
    <button type="button" className="runtime-menu-trigger" aria-expanded={open} disabled={disabled} onClick={() => onOpenChange(!open)}>{jobs.length} 个后台任务</button>
    {open && <div className="background-job-list" role="dialog" aria-label="后台任务">
      {jobs.map((job) => <div className="background-job" key={job.job_id}>
        <LoaderCircle size={12} className="spin" />
        <span>{jobKindLabel(job.kind)}</span>
        <code title={job.job_id}>{job.job_id}</code>
        <small>{job.status === "submitted" ? "等待开始" : "运行中"}</small>
      </div>)}
    </div>}
  </div>;
}
