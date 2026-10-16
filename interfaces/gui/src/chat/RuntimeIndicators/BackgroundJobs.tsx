import { useEffect, useRef, useState } from "react";
import { LoaderCircle } from "lucide-react";
import { jobKindLabel, type BackgroundJob } from "../runtimeState";

export function BackgroundJobs({ jobs, open, disabled, onOpenChange }: {
  jobs: BackgroundJob[];
  open: boolean;
  disabled: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const rootRef = useRef<HTMLDivElement | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);
  const [selectedJobId, setSelectedJobId] = useState<string | null>(null);
  const [detailsSide, setDetailsSide] = useState<"left" | "right" | "overlay">("right");
  const selectedJob = open ? jobs.find((job) => job.job_id === selectedJobId) : undefined;

  useEffect(() => {
    if (!open || !jobs.some((job) => job.job_id === selectedJobId)) setSelectedJobId(null);
  }, [jobs, open, selectedJobId]);

  const toggleDetails = (jobId: string) => {
    setSelectedJobId((current) => current === jobId ? null : jobId);
    const menu = menuRef.current?.getBoundingClientRect();
    if (!menu) return;
    const needed = Math.min(340, window.innerWidth - 48) + 8;
    // Cover the list when neither side of it fits, so a narrow window never
    // clips the details panel.
    setDetailsSide(
      window.innerWidth - menu.right >= needed ? "right"
        : menu.left >= needed ? "left"
        : "overlay",
    );
  };

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
    {open && <div className="background-job-menu" ref={menuRef}>
      <div className="background-job-list" role="dialog" aria-label="后台任务">
        {jobs.map((job) => <button type="button" className="background-job" key={job.job_id} aria-expanded={selectedJobId === job.job_id} onClick={() => toggleDetails(job.job_id)}>
          <LoaderCircle size={12} className="spin" />
          <span>{jobKindLabel(job.kind)}</span>
          <code title={job.job_id}>{job.job_id}</code>
          <small>{job.status === "submitted" ? "等待开始" : "运行中"}</small>
        </button>)}
      </div>
      {selectedJob && <div className={`background-job-popover ${detailsSide}`} role="dialog" aria-label={`${jobKindLabel(selectedJob.kind)}详情`}>
        <div className="background-job-popover-title">{jobKindLabel(selectedJob.kind)}</div>
        {selectedJob.kind === "shell" && <pre className="background-job-command">{selectedJob.details.command}</pre>}
        {selectedJob.kind === "subagent" && <div className="background-job-task">
          <div className="background-job-role">{selectedJob.details.role}</div>
          <p>{selectedJob.details.task}</p>
        </div>}
      </div>}
    </div>}
  </div>;
}
