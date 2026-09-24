import { useEffect, useRef, useState } from "react";
import type { ContextWindow } from "@nosis/protocol";

function formatTokens(value: number): string {
  return new Intl.NumberFormat("en-US").format(value);
}

export function ContextWindowIndicator({ window }: { window: ContextWindow | null }) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const closeOnOutsidePointer = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", closeOnOutsidePointer);
    return () => document.removeEventListener("pointerdown", closeOnOutsidePointer);
  }, [open]);

  if (!window) return null;
  const ratio = Math.min(1, window.input_tokens / window.max_input_tokens);
  const circumference = 2 * Math.PI * 7;
  const remaining = Math.max(0, window.max_input_tokens - window.input_tokens);
  const percent = Math.round(ratio * 100);
  return <div className="context-window" ref={rootRef}>
    <button type="button" className="context-window-trigger" aria-label={`上下文窗口已使用 ${percent}%`} aria-expanded={open} title="查看上下文窗口" onClick={() => setOpen((value) => !value)}>
      <svg className="context-ring" viewBox="0 0 18 18" aria-hidden="true">
        <circle className="context-ring-track" cx="9" cy="9" r="7" />
        <circle className="context-ring-value" cx="9" cy="9" r="7" strokeDasharray={circumference} strokeDashoffset={circumference * (1 - ratio)} />
      </svg>
      <span>{percent}%</span>
    </button>
    {open && <div className="context-popover" role="dialog" aria-label="上下文窗口详情">
      <div className="context-popover-title">Context window</div>
      <dl>
        <div><dt>当前输入</dt><dd>{formatTokens(window.input_tokens)}</dd></div>
        <div><dt>可用输入上限</dt><dd>{formatTokens(window.max_input_tokens)}</dd></div>
        <div><dt>剩余输入空间</dt><dd>{formatTokens(remaining)}</dd></div>
        <div><dt>压缩触发点</dt><dd>{formatTokens(window.compression_threshold)}</dd></div>
        <div><dt>模型总窗口</dt><dd>{formatTokens(window.max_context_tokens)}</dd></div>
        <div><dt>压缩次数</dt><dd>{window.compression_count}</dd></div>
      </dl>
    </div>}
  </div>;
}
