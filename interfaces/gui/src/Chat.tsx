import { useCallback, useEffect, useMemo, useRef, useState, type ClipboardEvent, type FormEvent, type KeyboardEvent, type PropsWithChildren } from "react";
import {
  AssistantRuntimeProvider, ComposerPrimitive, MessagePartPrimitive, MessagePrimitive,
  ThreadPrimitive, useAuiState, useExternalStoreRuntime,
  type AppendMessage, type FileMessagePartProps, type PartState, type ReasoningMessagePartProps, type ToolCallMessagePartProps,
} from "@assistant-ui/react";
import { MarkdownTextPrimitive } from "@assistant-ui/react-markdown";
import { AlertTriangle, ArrowUp, Check, ChevronDown, ChevronRight, FileText, LoaderCircle, MessageCircleQuestion, Paperclip, ShieldCheck, Square, Terminal, X } from "lucide-react";
import remarkGfm from "remark-gfm";
import { createScratchWorkspace, get, releaseActiveSession, selectWorkspace, sessionUrl, uploadAttachments, type ModelOption, type Session, type UserAttachment } from "./api";
import { SessionSocket } from "./session";
import { applyMessage, isTurnActivity, toMessages, TURN_PROCESS_GROUP, turnProcessPartIndexes, type Feedback, type TranscriptItem } from "./transcript";
import { runtimeIsActive, type ContextWindow, type Incoming, type PermissionPreset, type PlanSnapshot, type RuntimePhase, type UserQuestion } from "@nosis/protocol";

type Approval = {
  requestId: string;
  command: string;
  kind?: 'shell' | 'mcp';
  server?: string;
  toolName?: string;
};

type JobStatusMessage = Extract<Incoming, { type: "job_status" }>;
export type BackgroundJob = Pick<JobStatusMessage, "job_id" | "kind" | "status">;

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

const MARKDOWN_PLUGINS = [remarkGfm];

export function shouldSubmitComposerEnter(
  event: Pick<globalThis.KeyboardEvent, "key" | "shiftKey" | "isComposing" | "keyCode">,
  composing: boolean,
): boolean {
  return event.key === "Enter"
    && !event.shiftKey
    && !composing
    && !event.isComposing
    && event.keyCode !== 229;
}

export function shouldBlockRunningAttachmentSubmit(
  running: boolean,
  pendingFileCount: number,
): boolean {
  return running && pendingFileCount > 0;
}

export function composerConnectionGate({
  attaching,
  configurationPending,
  attachmentReplaced,
  interactionActive,
  backgroundDisconnected,
}: {
  attaching: boolean;
  configurationPending: boolean;
  attachmentReplaced: boolean;
  interactionActive: boolean;
  backgroundDisconnected: boolean;
}): { inputDisabled: boolean; sendDisabled: boolean } {
  return {
    inputDisabled: attachmentReplaced || interactionActive,
    sendDisabled: attaching || configurationPending || attachmentReplaced || interactionActive || backgroundDisconnected,
  };
}

export function shouldShowPlan(plan: PlanSnapshot | null): plan is PlanSnapshot {
  return plan !== null && plan.steps.some((step) => step.status !== "completed");
}

type RuntimeIndicator = "plan" | "jobs";

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

function ToolCard({ toolName, args, result }: ToolCallMessagePartProps) {
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

function jobKindLabel(kind: string): string {
  if (kind === "subagent") return "子代理";
  if (kind === "shell") return "后台命令";
  return kind;
}

function BackgroundJobs({ jobs, open, disabled, onOpenChange }: {
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

function PlanStatus({ plan, open, disabled, onOpenChange }: {
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

function UserMessage() {
  return <MessagePrimitive.Root className="user-turn">
    <MessageTimestamp className="turn-timestamp" />
    <div className="user-message"><MessagePrimitive.Parts components={{ File: FileAttachmentPart }} /></div>
  </MessagePrimitive.Root>;
}

function FileAttachmentPart({ data, filename, mimeType }: FileMessagePartProps) {
  return <a className="message-file" href={data} download={filename} title={mimeType}>
    <FileText size={18} />
    <span>{filename || "附件"}</span>
  </a>;
}

function MessageTimestamp({ className = "" }: { className?: string } = {}) {
  const createdAt = useAuiState((state) => state.message.createdAt);
  if (!createdAt) return null;
  return <time className={`message-timestamp ${className}`.trim()} dateTime={createdAt.toISOString()}>{createdAt.toLocaleString()}</time>;
}

function MarkdownText() {
  return <MarkdownTextPrimitive remarkPlugins={MARKDOWN_PLUGINS} />;
}

/** Model reasoning, shown the way the TUI shows it: dim and italic. */
function ReasoningText({ text }: ReasoningMessagePartProps) {
  return <div className="reasoning-text">{text}</div>;
}

function TurnPartGroup({ children }: PropsWithChildren) {
  const active = useAuiState((state) => state.thread.isRunning && state.message.isLast);
  const [expanded, setExpanded] = useState(active);
  const wasActiveRef = useRef(active);
  useEffect(() => {
    if (active) setExpanded(true);
    else if (wasActiveRef.current) setExpanded(false);
    wasActiveRef.current = active;
  }, [active]);
  return <details className="turn-process" open={active || expanded} onToggle={(event) => {
    if (!active) setExpanded(event.currentTarget.open);
  }}>
    <summary onClick={(event) => { if (active) event.preventDefault(); }}><ChevronRight size={14} className="turn-process-chevron" />推理与执行过程</summary>
    <div className="turn-process-content">{children}</div>
  </details>;
}

function AssistantParts() {
  const parts = useAuiState((state) => state.message.parts);
  const processParts = useMemo(() => new Set(
    turnProcessPartIndexes(parts).map((index) => parts[index]),
  ), [parts]);
  const groupBy = useCallback((part: PartState) => (
    processParts.has(part) ? [TURN_PROCESS_GROUP] as const : []
  ), [processParts]);

  return <MessagePrimitive.GroupedParts groupBy={groupBy} indicator="never">
    {({ part, children }) => {
      switch (part.type) {
        case TURN_PROCESS_GROUP:
          return <TurnPartGroup>{children}</TurnPartGroup>;
        case "text":
          return <MarkdownText />;
        case "reasoning":
          return <ReasoningText {...part} />;
        case "image":
          return <MessagePartPrimitive.Image />;
        case "tool-call":
          return part.toolUI ?? <ToolCard {...part} />;
        default:
          return null;
      }
    }}
  </MessagePrimitive.GroupedParts>;
}

function AssistantMessage() {
  return <MessagePrimitive.Root className="assistant-message">
    <div className="assistant-label"><span className="assistant-avatar"><img src="/nosis-avatar-128.png" alt="" /></span>Nosis</div>
    <div className="assistant-content"><AssistantParts /><MessageTimestamp /></div>
  </MessagePrimitive.Root>;
}

function PendingAttachment({ file, onRemove }: { file: File; onRemove: () => void }) {
  const [url, setUrl] = useState("");
  const image = file.type.startsWith("image/");
  useEffect(() => {
    if (!image) return;
    const next = URL.createObjectURL(file);
    setUrl(next);
    return () => URL.revokeObjectURL(next);
  }, [file, image]);
  return <span className={`attachment-chip ${image ? "image" : "file"}`}>
    {image ? <img src={url} alt={file.name} /> : <><FileText size={20} /><span><strong>{file.name}</strong><small>{formatFileSize(file.size)}</small></span></>}
    <button type="button" aria-label={`移除 ${file.name}`} onClick={onRemove}><X size={12} /></button>
  </span>;
}

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function formatTokens(value: number): string {
  return new Intl.NumberFormat("en-US").format(value);
}

function ContextWindowIndicator({ window }: { window: ContextWindow | null }) {
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

export function Chat({ session, selected, contextWindow, workspaceOptions = [], backgroundActive = false, providers, provider, onProviderChange, onBusyChange, onContextWindowChange, onSessionAvailable, onTurnEnd, onWorkspaceChange }: {
  session: Session; onBusyChange: (busy: boolean) => void; onTurnEnd: () => void;
  selected: boolean;
  contextWindow: ContextWindow | null;
  onContextWindowChange: (window: ContextWindow | null) => void;
  onSessionAvailable: () => void;
  providers: ModelOption[]; provider: string; onProviderChange: (provider: string) => void;
  backgroundActive?: boolean;
  workspaceOptions?: string[];
  onWorkspaceChange?: (workspace: string) => void;
}) {
  const [items, setItems] = useState<TranscriptItem[]>(session.items);
  const [running, setRunning] = useState(false);
  const [runtimePhase, setRuntimePhase] = useState<RuntimePhase>("inactive");
  const [pendingSteers, setPendingSteers] = useState(0);
  const [jobs, setJobs] = useState<Record<string, BackgroundJob>>({});
  const [attaching, setAttaching] = useState(false);
  const [attachmentReplaced, setAttachmentReplaced] = useState(false);
  const [toast, setToast] = useState<Extract<Feedback, { kind: "toast" }> | null>(null);
  const [alerts, setAlerts] = useState<Record<string, Extract<Feedback, { kind: "alert" }>>>({});
  const [reconnecting, setReconnecting] = useState(false);
  const [approval, setApproval] = useState<Approval | null>(null);
  const [question, setQuestion] = useState<UserQuestion | null>(null);
  const [plan, setPlan] = useState<PlanSnapshot | null>(session.plan ?? null);
  const [runtimePopover, setRuntimePopover] = useState<"plan" | "jobs" | null>(null);
  const [runtimeIndicatorOrder, setRuntimeIndicatorOrder] = useState<RuntimeIndicator[]>(
    () => shouldShowPlan(session.plan ?? null) ? ["plan"] : [],
  );
  const [permissionPreset, setPermissionPreset] = useState<PermissionPreset>(session.permission_preset);
  const [pendingPermissionPreset, setPendingPermissionPreset] = useState<PermissionPreset | null>(null);
  const [pendingProvider, setPendingProvider] = useState<string | null>(null);
  const [questionDraft, setQuestionDraft] = useState("");
  const [pendingFiles, setPendingFiles] = useState<File[]>([]);
  const [workspaceDraft, setWorkspaceDraft] = useState(session.workspace ?? "");
  const [pendingWorkspace, setPendingWorkspace] = useState<string | null>(null);
  const [workspaceEditing, setWorkspaceEditing] = useState(false);
  const workspacePickerRef = useRef<HTMLDivElement | null>(null);
  const socketRef = useRef<SessionSocket | null>(null);
  const socketModelRef = useRef("");
  const reconnectTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const mountedRef = useRef(true);
  const toastTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const compositionEndTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const composingRef = useRef(false);
  const turnCounter = useRef(0);
  const steerCounter = useRef(0);
  const activeTurnIdRef = useRef<string | null>(null);
  const eventCounterRef = useRef(session.event_sequence ?? 0);
  const [attachmentId] = useState(() => crypto.randomUUID());
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const messages = useMemo(() => toMessages(items, session.session_id), [items, session.session_id]);

  // Socket callbacks fire outside React's render, so the transcript and
  // turn state they fold onto are kept in refs.
  const itemsRef = useRef(items);
  const runningRef = useRef(false);
  const attachmentReplacedRef = useRef(false);
  const awaitingSessionActivityRef = useRef(false);
  const permissionSaving = pendingPermissionPreset !== null;
  const providerSaving = pendingProvider !== null;
  const workspaceSaving = pendingWorkspace !== null;
  const configurationPending = permissionSaving || providerSaving || workspaceSaving;
  const displayedProvider = pendingProvider ?? provider;
  const displayedWorkspace = pendingWorkspace ?? workspaceDraft;

  useEffect(() => {
    if (session.workspace) setWorkspaceDraft(session.workspace);
  }, [session.workspace]);

  useEffect(() => {
    if (!workspaceEditing) return;
    const closeOnOutsidePointer = (event: PointerEvent) => {
      if (workspacePickerRef.current?.contains(event.target as Node)) return;
      setWorkspaceEditing(false);
      if (!workspaceDraft.trim()) setWorkspaceDraft(session.workspace ?? "");
    };
    document.addEventListener("pointerdown", closeOnOutsidePointer);
    return () => document.removeEventListener("pointerdown", closeOnOutsidePointer);
  }, [workspaceEditing, workspaceDraft, session.workspace]);

  const showItems = useCallback((next: TranscriptItem[]) => {
    itemsRef.current = next;
    setItems(next);
  }, []);

  // A selected Chat owns a lightweight Session connection. Hidden Chats keep
  // one only while their turn remains active.
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      if (reconnectTimeoutRef.current) clearTimeout(reconnectTimeoutRef.current);
      socketRef.current?.close();
      socketRef.current = null;
      if (toastTimeoutRef.current) clearTimeout(toastTimeoutRef.current);
      if (compositionEndTimeoutRef.current) clearTimeout(compositionEndTimeoutRef.current);
    };
  }, []);

  useEffect(() => {
    const releaseOnPageLeave = () => {
      if (!runningRef.current) {
        void releaseActiveSession(
          session.session_id,
          socketModelRef.current || provider,
          attachmentId,
        ).catch(() => undefined);
      }
    };
    window.addEventListener("pagehide", releaseOnPageLeave);
    return () => window.removeEventListener("pagehide", releaseOnPageLeave);
  }, [attachmentId, provider, session.session_id]);

  useEffect(() => {
    if ((selected || backgroundActive) && socketRef.current === null && !attachmentReplacedRef.current) {
      connect({ attachOnly: !selected });
    }
  }, [backgroundActive, provider, selected, session.session_id, session.workspace]);

  useEffect(() => {
    if (selected || running || configurationPending || socketRef.current === null) return;
    const socket = socketRef.current;
    socketRef.current = null;
    socketModelRef.current = "";
    socket.close();
    void releaseActiveSession(session.session_id, provider, attachmentId).catch(() => undefined);
  }, [attachmentId, configurationPending, provider, running, selected, session.session_id]);

  const showToast = useCallback((next: Extract<Feedback, { kind: "toast" }>) => {
    if (toastTimeoutRef.current) clearTimeout(toastTimeoutRef.current);
    setToast(next);
    toastTimeoutRef.current = setTimeout(() => {
      toastTimeoutRef.current = null;
      setToast(null);
    }, 4000);
  }, []);

  const showAlert = useCallback((next: Extract<Feedback, { kind: "alert" }>) => {
    setAlerts((current) => ({ ...current, [next.id]: next }));
  }, []);

  const endTurn = useCallback(async () => {
    runningRef.current = false;
    activeTurnIdRef.current = null;
    awaitingSessionActivityRef.current = false;
    setRunning(false);
    setPendingSteers(0);
    setJobs({});
    setReconnecting(false);
    onBusyChange(false);
    setApproval(null);
    setQuestion(null);
    setQuestionDraft("");
    try {
      // The protocol omits tool output; the stored session has it.
      const stored = await get<Session>(sessionUrl(session.session_id));
      // A session without stored items would erase the live transcript.
      if (stored.items.length) showItems(stored.items);
      setPlan(stored.plan ?? null);
    } catch {
      // A turn without stored transcript items keeps the live transcript.
    }
    onTurnEnd();
  }, [session.session_id, onBusyChange, onTurnEnd, showItems]);

  function clearPendingSettings() {
    setPendingPermissionPreset(null);
    setPendingProvider(null);
    setPendingWorkspace(null);
  }

  function connect({ attachOnly = false, takeover = false }: { attachOnly?: boolean; takeover?: boolean } = {}): SessionSocket {
    setAttaching(true);
    let socket: SessionSocket;
    socket = new SessionSocket({
      sessionId: session.session_id,
      provider,
      workspace: workspaceDraft || session.workspace,
      attachmentId,
      attachOnly,
      afterEvent: eventCounterRef.current,
      takeover,
      onConfigurationRejected: (type) => {
        if (socketRef.current !== socket) return;
        if (type === "provider_set") setPendingProvider(null);
        else setPendingWorkspace(null);
        showToast({
          kind: "toast",
          level: "info",
          text: "会话正在执行任务，本次模型或工作区切换未应用，请在任务结束后重试。",
        });
      },
      onMessage: (message) => {
        // A socket that was replaced during a model switch may still have
        // messages queued in the browser event loop. Ignore those messages
        // so stale transcript, feedback, usage, and turn callbacks cannot
        // mutate the active connection's state.
        if (socketRef.current !== socket) return;
        if (message.type === "attachment_replaced" || message.type === "fatal") {
          clearPendingSettings();
        }
        if (message.type === "attachment_replaced") {
          if (toastTimeoutRef.current) {
            clearTimeout(toastTimeoutRef.current);
            toastTimeoutRef.current = null;
          }
          setToast(null);
          setJobs({});
          setReconnecting(false);
          socketRef.current = null;
          socketModelRef.current = "";
          attachmentReplacedRef.current = true;
          setAttachmentReplaced(true);
          setAttaching(false);
          setApproval(null);
          setQuestion(null);
          const active = runtimeIsActive(message.phase);
          setRuntimePhase(message.phase);
          runningRef.current = active;
          setRunning(active);
          onBusyChange(active);
          socket.close();
          return;
        }
        if (message.type === "runtime_state") {
          if (message.replayed) return;
          if (message.event_sequence !== undefined) eventCounterRef.current = message.event_sequence;
          if (toastTimeoutRef.current) {
            clearTimeout(toastTimeoutRef.current);
            toastTimeoutRef.current = null;
          }
          setToast(null);
          setReconnecting(false);
          setAttaching(false);
          attachmentReplacedRef.current = false;
          setAttachmentReplaced(false);
          if (message.provider) {
            socketModelRef.current = message.provider;
            onProviderChange(message.provider);
          }
          const active = runtimeIsActive(message.phase);
          setRuntimePhase(message.phase);
          runningRef.current = active;
          activeTurnIdRef.current = message.turn_id;
          setRunning(active);
          onBusyChange(active);
          setApproval(message.approval ? {
            requestId: message.approval.request_id,
            command: message.approval.command,
            kind: message.approval.kind,
            server: message.approval.server,
            toolName: message.approval.tool_name,
          } : null);
          setQuestion(message.question);
          setJobs(Object.fromEntries(message.jobs.map((job) => [job.job_id, job])));
          setPermissionPreset(message.permission_preset);
          setPlan(message.plan ?? null);
          onContextWindowChange(message.context_window);
          if (message.runtime_warnings?.length) {
            showAlert({
              kind: "alert",
              id: "runtime-warnings",
              level: "warning",
              text: message.runtime_warnings.join("\n"),
            });
          }
          return;
        }
        if (typeof message.event_sequence === "number") {
          eventCounterRef.current = message.event_sequence;
        }
        if (message.type === "user_steer_applied" || message.type === "user_steer_rejected") {
          setPendingSteers((count) => Math.max(0, count - 1));
        }
        if (message.type === "job_status") {
          setJobs((current) => updateBackgroundJobs(current, message));
          if (message.status === "failed") {
            showAlert({
              kind: "alert",
              id: `job:${message.job_id}`,
              level: "error",
              text: `${jobKindLabel(message.kind)}任务失败。`,
            });
          }
        }
        if (message.type === "session_ready") {
          attachmentReplacedRef.current = false;
          setAttachmentReplaced(false);
          setReconnecting(false);
          if (toastTimeoutRef.current) {
            clearTimeout(toastTimeoutRef.current);
            toastTimeoutRef.current = null;
          }
          setToast(null);
          setAlerts((current) => {
            if (!("connection" in current)) return current;
            const next = { ...current };
            delete next.connection;
            return next;
          });
        }
        if (message.type === "permission_changed" && !message.replayed) setPendingPermissionPreset(null);
        if (message.type === "plan_updated") setPlan(message.plan);
        if (message.type === "provider_changed") {
          if (!message.replayed) setPendingProvider(null);
          socketModelRef.current = message.provider;
          onProviderChange(message.provider);
          onContextWindowChange(null);
        }
        if (message.type === "workspace_changed") {
          setWorkspaceDraft(message.workspace);
          onWorkspaceChange?.(message.workspace);
          if (!message.replayed) setPendingWorkspace(null);
        }
        if (awaitingSessionActivityRef.current && isTurnActivity(message)) {
          awaitingSessionActivityRef.current = false;
          onSessionAvailable();
        }
        const applied = applyMessage(itemsRef.current, message);
        showItems(applied.items);
        if (applied.feedback?.kind === "toast") showToast(applied.feedback);
        if (applied.feedback?.kind === "alert") showAlert(applied.feedback);
        if (applied.contextWindow !== undefined) onContextWindowChange(applied.contextWindow);
        if (applied.approval !== undefined) setApproval(applied.approval);
        if (applied.question !== undefined) {
          setQuestion(applied.question);
          setQuestionDraft("");
        }
        if (applied.permissionPreset !== undefined) setPermissionPreset(applied.permissionPreset);
        if (message.type === "fatal") eventCounterRef.current = 0;
        if (applied.finished) void endTurn();
      },
      onClose: () => {
        // A stale socket can close after a replacement has already been
        // installed (for example after changing the model). Do not clear
        // the newer connection in that case.
        if (socketRef.current !== socket) return;
        socketRef.current = null;
        socketModelRef.current = "";
        setAttaching(false);
        clearPendingSettings();
        if (runningRef.current && !attachmentReplacedRef.current) {
          setReconnecting(true);
          reconnectTimeoutRef.current = setTimeout(() => {
            reconnectTimeoutRef.current = null;
            if (mountedRef.current && runningRef.current && socketRef.current === null && !attachmentReplacedRef.current) {
              connect({ attachOnly: true, takeover: false });
            }
          }, 1000);
        }
      },
      onError: () => {
        // Browsers report an error before closing a WebSocket that went
        // stale while the page was idle. That is not an interrupted turn;
        // leave the idle UI quiet. The close callback clears the socket so
        // the next message can establish a fresh connection.
        if (socketRef.current !== socket) return;
        if (!runningRef.current) {
          showAlert({ kind: "alert", id: "connection", level: "error", text: "无法连接 Nosis。" });
        }
      },
    });
    socketRef.current = socket;
    socketModelRef.current = provider;
    return socket;
  }

  function takeOverAttachment() {
    if (attaching || socketRef.current !== null) return;
    connect({ attachOnly: true, takeover: true });
  }

  async function onNew(message: AppendMessage) {
    const text = message.content.filter((part) => part.type === "text").map((part) => part.text).join("\n").trim();
    if (!text && pendingFiles.length === 0) return;
    if (runningRef.current) {
      const turnId = activeTurnIdRef.current;
      if (pendingFiles.length > 0) {
        showToast({ kind: "toast", level: "info", text: "任务运行中不能发送附件；待发送内容已保留，请停止或等待当前任务结束。" });
        return;
      }
      if (!turnId) return;
      steerCounter.current += 1;
      socketRef.current?.send({
        type: "user_steer",
        turn_id: turnId,
        steer_id: `steer-${steerCounter.current}`,
        text,
      });
      setPendingSteers((count) => count + 1);
      return;
    }
    const startsSession = !itemsRef.current.some((item) => item.role === "user" && item.origin !== "tool_media");

    let attachments: UserAttachment[] = [];
    if (pendingFiles.length) {
      try {
      attachments = await uploadAttachments(pendingFiles, session.session_id);
      } catch (error) {
        showAlert({ kind: "alert", id: "attachment-upload", level: "error", text: String(error) });
        return;
      }
      setPendingFiles([]);
    }

    showItems([
      ...itemsRef.current,
      {
        role: "user",
        content: attachments.length
          ? [{ type: "text", text }, ...attachments]
          : text,
      },
    ]);
    runningRef.current = true;
    setRuntimePhase("starting");
    setRunning(true);
    onBusyChange(true);
    if (toastTimeoutRef.current) clearTimeout(toastTimeoutRef.current);
    setToast(null);
    setAlerts({});
    setJobs({});
    setReconnecting(false);
    turnCounter.current += 1;
    activeTurnIdRef.current = `turn-${turnCounter.current}`;
    const socket = socketRef.current ?? connect();
    awaitingSessionActivityRef.current = startsSession;
    socket.send({
      type: "user_turn",
      turn_id: activeTurnIdRef.current,
      text,
      ...(attachments.length ? { attachments } : {}),
    });
  }

  function respond(approved: boolean) {
    if (!approval) return;
    socketRef.current?.send({ type: "approval_response", request_id: approval.requestId, approved });
    setApproval(null);
  }

  function changePermissionPreset(preset: PermissionPreset) {
    if (preset === permissionPreset) return;
    setPendingPermissionPreset(preset);
    const socket = socketRef.current ?? connect();
    socket.send({ type: "permission_set", preset });
  }

  function changeProvider(provider: string) {
    setPendingProvider(provider);
    const socket = socketRef.current ?? connect();
    socket.send({ type: "provider_set", provider });
  }

  function answerQuestion(answer: { option_id: string } | { text: string }) {
    if (!question) return;
    socketRef.current?.send({
      type: "user_question_response",
      request_id: question.request_id,
      ...answer,
    });
    setQuestion(null);
    setQuestionDraft("");
  }

  async function saveWorkspace(value = workspaceDraft) {
    const nextWorkspace = value.trim();
    if (!nextWorkspace || nextWorkspace === session.workspace) return;
    setPendingWorkspace(nextWorkspace);
    try {
      const socket = socketRef.current ?? connect();
      socket.send({ type: "workspace_set", workspace: nextWorkspace });
    } catch (error) {
      showAlert({ kind: "alert", id: "workspace-update", level: "error", text: String(error) });
      setPendingWorkspace(null);
    }
  }

  async function chooseNewWorkspace() {
    try {
      const selected = await selectWorkspace();
      if (!selected) return;
      setWorkspaceEditing(false);
      await saveWorkspace(selected);
    } catch (error) {
      showAlert({ kind: "alert", id: "workspace-picker", level: "error", text: String(error) });
    }
  }

  async function chooseScratchWorkspace() {
    try {
      const selected = await createScratchWorkspace(session.session_id);
      setWorkspaceEditing(false);
      await saveWorkspace(selected);
    } catch (error) {
      showAlert({ kind: "alert", id: "scratch-workspace", level: "error", text: String(error) });
    }
  }

  /**
   * Browsers expose pasted files through clipboardData.items rather than the
   * textarea's value. Capture those files and feed them
   * through the same pending-attachment queue used by the paperclip picker.
   * Keep normal text paste untouched; only suppress the browser default when
   * the clipboard contains files and no textual payload.
   */
  function onPaste(event: ClipboardEvent<HTMLTextAreaElement>) {
    const files = Array.from(event.clipboardData.items)
      .filter((item) => item.kind === "file")
      .map((item) => item.getAsFile())
      .filter((file): file is File => file !== null);
    if (files.length === 0) return;
    setPendingFiles((previous) => [...previous, ...files]);
    if (!event.clipboardData.getData("text/plain")) event.preventDefault();
  }

  function onComposerKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    const nativeEvent = event.nativeEvent as globalThis.KeyboardEvent;
    if (!shouldSubmitComposerEnter(nativeEvent, composingRef.current)) return;
    event.preventDefault();
    event.currentTarget.closest("form")?.requestSubmit();
  }

  function onComposerSubmit(event: FormEvent<HTMLFormElement>) {
    if (!shouldBlockRunningAttachmentSubmit(runningRef.current, pendingFiles.length)) return;
    // Stop assistant-ui before it clears the draft; attachments cannot be steered
    // into an already running turn, so the whole pending message stays intact.
    event.preventDefault();
    showToast({ kind: "toast", level: "info", text: "任务运行中不能发送附件；待发送内容已保留，请停止或等待当前任务结束。" });
  }

  function onCompositionStart() {
    if (compositionEndTimeoutRef.current) clearTimeout(compositionEndTimeoutRef.current);
    composingRef.current = true;
  }

  function onCompositionEnd() {
    if (compositionEndTimeoutRef.current) clearTimeout(compositionEndTimeoutRef.current);
    // Some IMEs dispatch the Enter keydown immediately after compositionend
    // with isComposing=false. Keep the composition guard through that event.
    compositionEndTimeoutRef.current = setTimeout(() => {
      compositionEndTimeoutRef.current = null;
      composingRef.current = false;
    }, 0);
  }

  const interactionActive = approval !== null || question !== null;
  const backgroundDisconnected = backgroundActive && socketRef.current === null;
  const composerGate = composerConnectionGate({
    attaching,
    configurationPending,
    attachmentReplaced,
    interactionActive,
    backgroundDisconnected,
  });
  const controlsDisabled = attachmentReplaced || backgroundDisconnected;
  const sessionRunning = running || backgroundActive;
  const runtime = useExternalStoreRuntime({
    messages,
    convertMessage: (message) => message,
    isRunning: running,
    isDisabled: composerGate.inputDisabled,
    isSendDisabled: composerGate.sendDisabled,
    onNew,
    queue: {
      items: [],
      steerItems: [],
      enqueue: onNew,
      steer: onNew,
      move: () => {},
      edit: () => {},
      remove: () => {},
    },
  });
  const availableWorkspaces = Array.from(new Set([...workspaceOptions, displayedWorkspace].filter(Boolean)));
  const activeJobs = Object.values(jobs);
  const hasActiveJobs = activeJobs.length > 0;
  const visibleAlerts = Object.values(alerts);
  const visiblePlan = shouldShowPlan(plan) ? plan : null;
  const questionFocusOptionId = question?.options.find((option) => option.recommended)?.id ?? question?.options[0]?.id;
  const setPlanOpen = useCallback((open: boolean) => setRuntimePopover(open ? "plan" : null), []);
  const setJobsOpen = useCallback((open: boolean) => setRuntimePopover(open ? "jobs" : null), []);

  useEffect(() => {
    setRuntimeIndicatorOrder((current) => updateRuntimeIndicatorOrder(
      current,
      visiblePlan !== null,
      hasActiveJobs,
    ));
  }, [hasActiveJobs, visiblePlan]);

  useEffect(() => {
    if (
      interactionActive
      || reconnecting
      || (runtimePopover === "plan" && visiblePlan === null)
      || (runtimePopover === "jobs" && !hasActiveJobs)
    ) {
      setRuntimePopover(null);
    }
  }, [hasActiveJobs, interactionActive, reconnecting, runtimePopover, visiblePlan]);

  return <AssistantRuntimeProvider runtime={runtime}>
    <ThreadPrimitive.Root className="thread">
      <ThreadPrimitive.Viewport className="thread-viewport">
        <ThreadPrimitive.Empty>
          <div className="welcome"><div className="welcome-symbol"><Terminal size={26} /></div><div className="eyebrow">YOUR PERSONAL AGENT</div><h1>一起，把想法变成现实。</h1><p>聊聊你的项目，或者交给 Nosis 一个任务。</p></div>
        </ThreadPrimitive.Empty>
        <div className="messages"><ThreadPrimitive.Messages components={{ UserMessage, AssistantMessage }} /></div>
      </ThreadPrimitive.Viewport>
      <div className="composer-area">
        {attachmentReplaced && <div className="attachment-replaced" role="status"><span>此会话由另一个页面控制，可在此页面手动接管。</span><button type="button" onClick={takeOverAttachment} disabled={attaching}>{attaching ? "正在接管…" : "在此页面接管"}</button></div>}
        {!attachmentReplaced && approval && <div className="approval-card" role="region" aria-label="工具执行确认"><div className="approval-title"><ShieldCheck size={17} /> 允许执行此工具调用？</div><pre>{approval.command}</pre><div className="approval-actions"><button onClick={() => respond(false)}>拒绝</button><button className="approve-button" onClick={() => respond(true)}>允许执行</button></div></div>}
        {!attachmentReplaced && question && <div className="question-card" role="region" aria-label="需要你的选择">
          <div className="question-title"><MessageCircleQuestion size={18} /><span>{question.question}</span></div>
          <div className="question-options">{question.options.map((option) => <button type="button" className="question-option" key={option.id} autoFocus={option.id === questionFocusOptionId} onClick={() => answerQuestion({ option_id: option.id })}>
            <span className="question-option-heading"><span>{option.label}</span>{option.recommended && <span className="recommended-badge">推荐</span>}</span>
            {option.description && <span className="question-option-description">{option.description}</span>}
          </button>)}</div>
          {question.allow_free_text && <form className="question-free-text" onSubmit={(event) => { event.preventDefault(); const text = questionDraft.trim(); if (text) answerQuestion({ text }); }}>
            <input value={questionDraft} onChange={(event) => setQuestionDraft(event.target.value)} placeholder="输入其他答案…" aria-label="其他答案" />
            <button type="submit" disabled={!questionDraft.trim()}>提交</button>
          </form>}
        </div>}
        {visibleAlerts.length > 0 && <div className="feedback-alerts">{visibleAlerts.map((item) => <div className={`feedback-alert ${item.level}`} role="alert" key={item.id}><AlertTriangle size={15} /><span>{item.text}</span><button type="button" aria-label="关闭提示" onClick={() => setAlerts((current) => { const next = { ...current }; delete next[item.id]; return next; })}><X size={13} /></button></div>)}</div>}
        {toast && <div className={`feedback-toast ${toast.level}`} role="status">{toast.text}</div>}
        {(running || visiblePlan) && !attachmentReplaced && <div className="activity">
          {running && <LoaderCircle size={13} className="spin" />}
          <span className="activity-label" role="status">{running ? reconnecting ? "连接中断，任务仍在后台运行，正在重新连接…" : runtimePhase === "starting" ? "正在启动 Agent…" : approval ? "等待你的确认" : question ? "等待你的选择" : pendingSteers ? `Nosis 正在处理… ${pendingSteers} 条引导待应用` : "Nosis 正在处理…" : "Plan 尚未完成"}</span>
          <div className="activity-controls">
            {runtimeIndicatorOrder.map((item) => item === "plan"
              ? visiblePlan && <PlanStatus key="plan" plan={visiblePlan} open={runtimePopover === "plan" && !interactionActive} disabled={interactionActive} onOpenChange={setPlanOpen} />
              : hasActiveJobs && <BackgroundJobs key="jobs" jobs={activeJobs} open={runtimePopover === "jobs" && !interactionActive && !reconnecting} disabled={interactionActive || reconnecting} onOpenChange={setJobsOpen} />)}
            {running && !interactionActive && <button className="stop-button" aria-label="停止执行" onClick={() => { const turnId = activeTurnIdRef.current; if (turnId) socketRef.current?.send({ type: "cancel", turn_id: turnId }); }}><Square size={11} /> 停止</button>}
          </div>
        </div>}
        {pendingFiles.length > 0 && <div className="attachment-list" aria-label="待发送附件">{pendingFiles.map((file, index) => <PendingAttachment key={`${file.name}-${file.lastModified}-${index}`} file={file} onRemove={() => setPendingFiles((files) => files.filter((_, itemIndex) => itemIndex !== index))} />)}</div>}
        <ComposerPrimitive.Root className="composer" onSubmit={onComposerSubmit}>
          <div ref={workspacePickerRef} className="workspace-picker-wrap">
            <button type="button" className="workspace-picker" onClick={() => setWorkspaceEditing((value) => !value)} disabled={controlsDisabled || sessionRunning} aria-expanded={workspaceEditing}>
              <span className="workspace-picker-icon">⌂</span><span className="workspace-picker-value">{displayedWorkspace || "选择项目"}</span>
              {workspaceSaving ? <LoaderCircle size={14} className="spin" aria-label="正在切换工作区" /> : <ChevronDown size={14} />}
            </button>
            {workspaceEditing && <div className="workspace-menu" role="menu">
              <div className="workspace-menu-heading">选择工作区</div>
              {availableWorkspaces.map((path) => <button type="button" role="menuitem" className={`workspace-option ${path === displayedWorkspace ? "selected" : ""}`} key={path} onClick={() => { void saveWorkspace(path); setWorkspaceEditing(false); }} disabled={controlsDisabled || sessionRunning || workspaceSaving} title={path}><span className="workspace-option-path">{path}</span></button>)}
              <div className="workspace-menu-divider" />
              <button type="button" role="menuitem" className="workspace-new-option" onClick={() => { void chooseNewWorkspace(); }} disabled={controlsDisabled || sessionRunning || workspaceSaving}>＋ 新建工作区</button>
              <button type="button" role="menuitem" className="workspace-new-option" onClick={() => { void chooseScratchWorkspace(); }} disabled={controlsDisabled || sessionRunning || workspaceSaving}>＋ 临时工作区</button>
            </div>}
          </div>
          <ComposerPrimitive.Input placeholder={question ? "请先回答上方问题…" : approval ? "请先处理上方确认…" : "Ask Nosis…"} aria-label="消息" rows={2} autoFocus submitMode="none" disabled={composerGate.inputDisabled} onKeyDown={onComposerKeyDown} onCompositionStart={onCompositionStart} onCompositionEnd={onCompositionEnd} onPaste={onPaste} /><div className="composer-bottom">
          <button type="button" className="attachment-button" aria-label="添加附件" title="添加附件" disabled={controlsDisabled || sessionRunning} onClick={() => fileInputRef.current?.click()}><Paperclip size={15} /></button>
          <input ref={fileInputRef} className="attachment-input" type="file" multiple onChange={(event) => { setPendingFiles((files) => [...files, ...Array.from(event.target.files ?? [])]); event.currentTarget.value = ""; }} />
          <label className="model-selector" title={providers.find((option) => option.id === displayedProvider)?.model}>
            <select aria-label="选择模型" value={displayedProvider} disabled={controlsDisabled || sessionRunning || providerSaving} onChange={(event) => changeProvider(event.target.value)}>
              {providers.map((option) => <option key={option.id} value={option.id}>{option.model}</option>)}
            </select>{providerSaving ? <LoaderCircle size={12} className="spin" aria-label="正在切换模型" /> : <ChevronDown size={12} />}
          </label>
          <label className="permission-selector" title="权限模式">
            <ShieldCheck size={13} />
            <select aria-label="权限模式" value={pendingPermissionPreset ?? permissionPreset} disabled={controlsDisabled || permissionSaving} onChange={(event) => changePermissionPreset(event.target.value as PermissionPreset)}>
              <option value="ask_for_approval">请求批准</option>
              <option value="workspace_access">工作区访问</option>
              <option value="full_access">完全访问</option>
            </select>{permissionSaving ? <LoaderCircle size={12} className="spin" aria-label="正在保存权限" /> : <ChevronDown size={12} />}
          </label>
          <div className="composer-actions"><ContextWindowIndicator window={contextWindow} /><ComposerPrimitive.Send className="send-button" aria-label={running ? "发送引导" : "发送消息"}><ArrowUp size={19} /></ComposerPrimitive.Send></div></div></ComposerPrimitive.Root>
        <div className="composer-footer">Nosis · 你的项目搭档</div>
      </div>
    </ThreadPrimitive.Root>
  </AssistantRuntimeProvider>;
}
