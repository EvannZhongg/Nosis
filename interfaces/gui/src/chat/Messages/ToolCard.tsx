import { useAuiState, type ToolCallMessagePartProps } from "@assistant-ui/react";
import { Check, ChevronRight, LoaderCircle, X } from "lucide-react";

export function ToolCard({ toolName, args, result }: ToolCallMessagePartProps) {
  const running = useAuiState((state) => state.thread.isRunning);
  const output = result as { ok: boolean; output?: unknown; error?: { message: string } } | undefined;
  const detail = args.path ?? args.pattern ?? args.command;
  const submittedBackgroundJob = args.background === true && output?.ok === true;

  return <details className="tool-card">
    <summary><ChevronRight size={14} className="tool-chevron" /><span className="tool-name">{toolName}</span><span className="tool-detail">{typeof detail === "string" ? detail : ""}</span>
      {output ? output.ok ? submittedBackgroundJob ? <span className="tool-status success">已提交</span> : <Check size={15} className="success" /> : <X size={15} className="failure" /> : running ? <LoaderCircle size={15} className="spin" /> : <span className="tool-status">未完成</span>}
    </summary>
    <div className="tool-body"><div className="tool-caption">参数</div><pre>{JSON.stringify(args, null, 2)}</pre>
      {output && (output.ok
        ? output.output !== undefined && <><div className="tool-caption">结果</div><pre>{JSON.stringify(output.output, null, 2)}</pre></>
        : <><div className="tool-caption">执行失败</div><pre>{JSON.stringify(output.error, null, 2)}</pre></>)}
    </div>
  </details>;
}
