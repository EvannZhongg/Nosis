import { useCallback, useEffect, useMemo, useRef, useState, type ClipboardEvent, type FormEvent, type KeyboardEvent } from "react";
import {
  AssistantRuntimeProvider, ComposerPrimitive, MessagePrimitive,
  ThreadPrimitive, useAuiState, useExternalStoreRuntime,
  type AppendMessage, type ReasoningMessagePartProps, type ToolCallMessagePartProps,
} from "@assistant-ui/react";
import { MarkdownTextPrimitive } from "@assistant-ui/react-markdown";
import { ArrowUp, Check, ChevronDown, ChevronRight, LoaderCircle, MessageCircleQuestion, Paperclip, ShieldCheck, Square, Terminal, X } from "lucide-react";
import remarkGfm from "remark-gfm";
import { get, releaseRuntime, selectWorkspace, sessionUrl, updateSessionWorkspace, uploadAttachments, type ImageAttachment, type ModelOption, type Session } from "./api";
import { SessionSocket } from "./session";
import { applyMessage, isTurnActivity, toMessages, type Notice, type TranscriptItem } from "./transcript";
import type { PermissionPreset, Usage, UserQuestion } from "@nosis/protocol";

type Approval = {
  requestId: string;
  command: string;
  kind?: 'shell' | 'mcp';
  server?: string;
  toolName?: string;
};

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

function ToolCard({ toolName, args, result }: ToolCallMessagePartProps) {
  const running = useAuiState((state) => state.thread.isRunning);
  const output = result as { ok: boolean; output?: unknown; error?: { message: string } } | undefined;
  const detail = args.path ?? args.pattern ?? args.command;
  return <details className="tool-card">
    <summary><ChevronRight size={14} className="tool-chevron" /><span className="tool-name">{toolName}</span><span className="tool-detail">{typeof detail === "string" ? detail : ""}</span>
      {output ? output.ok ? <Check size={15} className="success" /> : <X size={15} className="failure" /> : running ? <LoaderCircle size={15} className="spin" /> : <span className="tool-status">未完成</span>}
    </summary>
    <div className="tool-body"><div className="tool-caption">参数</div><pre>{JSON.stringify(args, null, 2)}</pre>
      {output && (output.ok
        ? output.output !== undefined && <><div className="tool-caption">结果</div><pre>{JSON.stringify(output.output, null, 2)}</pre></>
        : <><div className="tool-caption">执行失败</div><pre>{JSON.stringify(output.error, null, 2)}</pre></>)}
    </div>
  </details>;
}

function UserMessage() {
  return <MessagePrimitive.Root className="user-message"><MessagePrimitive.Parts /><MessageTimestamp /></MessagePrimitive.Root>;
}

function MessageTimestamp() {
  const createdAt = useAuiState((state) => state.message.createdAt);
  if (!createdAt) return null;
  return <time className="message-timestamp" dateTime={createdAt.toISOString()}>{createdAt.toLocaleString()}</time>;
}

function MarkdownText() {
  return <MarkdownTextPrimitive remarkPlugins={MARKDOWN_PLUGINS} />;
}

/** Model reasoning, shown the way the TUI shows it: dim and italic. */
function ReasoningText({ text }: ReasoningMessagePartProps) {
  return <div className="reasoning-text">{text}</div>;
}

function AssistantMessage() {
  return <MessagePrimitive.Root className="assistant-message">
    <div className="assistant-label"><span className="assistant-avatar"><img src="/nosis-avatar-128.png" alt="" /></span>Nosis</div>
    <div className="assistant-content"><MessagePrimitive.Parts components={{ Text: MarkdownText, Reasoning: ReasoningText, tools: { Fallback: ToolCard } }} /><MessageTimestamp /></div>
  </MessagePrimitive.Root>;
}

function PendingAttachment({ file, onRemove }: { file: File; onRemove: () => void }) {
  const [url, setUrl] = useState("");
  useEffect(() => {
    const next = URL.createObjectURL(file);
    setUrl(next);
    return () => URL.revokeObjectURL(next);
  }, [file]);
  return <span className="attachment-chip"><img src={url} alt={file.name} /><span>{file.name}</span><button type="button" aria-label={`移除 ${file.name}`} onClick={onRemove}><X size={12} /></button></span>;
}

export function Chat({ session, workspaceOptions = [], inputDisabled, runtimeActive = false, models, model, onModelChange, onBusyChange, onUsageChange, onSessionAvailable, onTurnEnd, onWorkspaceChange }: {
  session: Session; inputDisabled: boolean; onBusyChange: (busy: boolean) => void; onTurnEnd: () => void;
  onUsageChange: (usage: Usage | null) => void;
  onSessionAvailable: () => void;
  models: ModelOption[]; model: string; onModelChange: (model: string) => void;
  runtimeActive?: boolean;
  workspaceOptions?: string[];
  onWorkspaceChange?: (workspace: string) => void;
}) {
  const [items, setItems] = useState<TranscriptItem[]>(session.items);
  const [running, setRunning] = useState(false);
  const [pendingSteers, setPendingSteers] = useState(0);
  const [attaching, setAttaching] = useState(false);
  const [attachmentReplaced, setAttachmentReplaced] = useState(false);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [compressionNotice, setCompressionNotice] = useState("");
  const [approval, setApproval] = useState<Approval | null>(null);
  const [question, setQuestion] = useState<UserQuestion | null>(null);
  const [permissionPreset, setPermissionPreset] = useState<PermissionPreset>(session.permission_preset);
  const [questionDraft, setQuestionDraft] = useState("");
  const [pendingFiles, setPendingFiles] = useState<File[]>([]);
  const [workspaceDraft, setWorkspaceDraft] = useState(session.workspace ?? "");
  const [workspaceSaving, setWorkspaceSaving] = useState(false);
  const [workspaceEditing, setWorkspaceEditing] = useState(false);
  const workspacePickerRef = useRef<HTMLDivElement | null>(null);
  const socketRef = useRef<SessionSocket | null>(null);
  const socketModelRef = useRef("");
  const selectedModelRef = useRef(model);
  const reconnectTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const mountedRef = useRef(true);
  const noticeTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const compressionTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
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

  // A Chat owns its attachment independently of whether it is visible. Only
  // unmounting detaches it; opening a historical session does not connect.
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      if (reconnectTimeoutRef.current) clearTimeout(reconnectTimeoutRef.current);
      socketRef.current?.close();
      socketRef.current = null;
      if (noticeTimeoutRef.current) clearTimeout(noticeTimeoutRef.current);
      if (compressionTimeoutRef.current) clearTimeout(compressionTimeoutRef.current);
      if (compositionEndTimeoutRef.current) clearTimeout(compositionEndTimeoutRef.current);
    };
  }, []);

  useEffect(() => {
    const releaseOnPageLeave = () => {
      if (!runningRef.current) {
        void releaseRuntime(
          session.session_id,
          socketModelRef.current || model,
          attachmentId,
        ).catch(() => undefined);
      }
    };
    window.addEventListener("pagehide", releaseOnPageLeave);
    return () => window.removeEventListener("pagehide", releaseOnPageLeave);
  }, [attachmentId, model, session.session_id]);

  // Model selection belongs to this session. It detaches only this Chat and
  // is applied when its next turn attaches or starts a runtime.
  useEffect(() => {
    const previousModel = selectedModelRef.current;
    selectedModelRef.current = model;
    if (previousModel === model) return;
    if (socketRef.current && socketModelRef.current === model) return;
    const socket = socketRef.current;
    socketRef.current = null;
    socketModelRef.current = "";
    eventCounterRef.current = 0;
    void releaseRuntime(
      session.session_id,
      previousModel,
      attachmentId,
    ).catch(() => undefined);
    socket?.close();
  }, [attachmentId, model, session.session_id]);

  useEffect(() => {
    if (runtimeActive && model && socketRef.current === null) {
      setAttaching(true);
      connect({ attachOnly: true, takeover: true });
    }
  }, [runtimeActive, model]);

  const showNotice = useCallback((next: Notice, transient = false) => {
    if (noticeTimeoutRef.current) clearTimeout(noticeTimeoutRef.current);
    setNotice(next);
    if (transient) {
      noticeTimeoutRef.current = setTimeout(() => {
        noticeTimeoutRef.current = null;
        setNotice(null);
      }, 4000);
    }
  }, []);

  const showCompressionNotice = useCallback((checkpoint: number) => {
    if (compressionTimeoutRef.current) clearTimeout(compressionTimeoutRef.current);
    setCompressionNotice(`上下文已压缩（checkpoint ${checkpoint}）。`);
    compressionTimeoutRef.current = setTimeout(() => {
      compressionTimeoutRef.current = null;
      setCompressionNotice("");
    }, 30_000);
  }, []);

  const endTurn = useCallback(async () => {
    runningRef.current = false;
    activeTurnIdRef.current = null;
    awaitingSessionActivityRef.current = false;
    setRunning(false);
    setPendingSteers(0);
    onBusyChange(false);
    setApproval(null);
    setQuestion(null);
    setQuestionDraft("");
    try {
      // The protocol omits tool output; the stored session has it.
      const stored = await get<Session>(sessionUrl(session.session_id));
      // A session without stored items would erase the live transcript.
      if (stored.items.length) showItems(stored.items);
    } catch {
      // A turn without stored transcript items keeps the live transcript.
    }
    onTurnEnd();
  }, [session.session_id, onBusyChange, onTurnEnd, showItems]);

  function connect({ attachOnly = false, takeover = true }: { attachOnly?: boolean; takeover?: boolean } = {}): SessionSocket {
    let socket: SessionSocket;
    socket = new SessionSocket({
      sessionId: session.session_id,
      provider: model,
      attachmentId,
      attachOnly,
      afterEvent: eventCounterRef.current,
      takeover,
      onMessage: (message) => {
        // A socket that was replaced during a model switch may still have
        // messages queued in the browser event loop. Ignore those messages
        // so stale transcript, notice, usage, and turn callbacks cannot
        // mutate the active connection's state.
        if (socketRef.current !== socket) return;
        if (message.type === "attachment_replaced") {
          if (noticeTimeoutRef.current) {
            clearTimeout(noticeTimeoutRef.current);
            noticeTimeoutRef.current = null;
          }
          setNotice(null);
          socketRef.current = null;
          socketModelRef.current = "";
          attachmentReplacedRef.current = true;
          setAttachmentReplaced(true);
          setAttaching(false);
          setApproval(null);
          setQuestion(null);
          const wasRunning = runningRef.current;
          runningRef.current = message.running;
          setRunning(message.running);
          onBusyChange(message.running);
          socket.close();
          if (wasRunning && !message.running) void endTurn();
          return;
        }
        if (message.type === "runtime_state") {
          eventCounterRef.current = message.event_sequence;
          if (noticeTimeoutRef.current) {
            clearTimeout(noticeTimeoutRef.current);
            noticeTimeoutRef.current = null;
          }
          setNotice(null);
          setAttaching(false);
          attachmentReplacedRef.current = false;
          setAttachmentReplaced(false);
          if (message.provider) {
            socketModelRef.current = message.provider;
            if (message.provider !== model) onModelChange(message.provider);
          }
          const wasRunning = runningRef.current;
          runningRef.current = message.running;
          activeTurnIdRef.current = message.turn_id;
          setRunning(message.running);
          onBusyChange(message.running);
          setApproval(message.approval ? {
            requestId: message.approval.request_id,
            command: message.approval.command,
            kind: message.approval.kind,
            server: message.approval.server,
            toolName: message.approval.tool_name,
          } : null);
          setQuestion(message.question);
          setPermissionPreset(message.permission_preset);
          if (wasRunning && !message.running) void endTurn();
          return;
        }
        if (typeof message.event_sequence === "number") {
          eventCounterRef.current = message.event_sequence;
        }
        if (message.type === "user_steer_applied" || message.type === "user_steer_rejected") {
          setPendingSteers((count) => Math.max(0, count - 1));
        }
        if (message.type === "ready") {
          attachmentReplacedRef.current = false;
          setAttachmentReplaced(false);
          if (noticeTimeoutRef.current) {
            clearTimeout(noticeTimeoutRef.current);
            noticeTimeoutRef.current = null;
          }
          setNotice(null);
        }
        if (awaitingSessionActivityRef.current && isTurnActivity(message)) {
          awaitingSessionActivityRef.current = false;
          onSessionAvailable();
        }
        const applied = applyMessage(itemsRef.current, message);
        showItems(applied.items);
        if (applied.notice) showNotice(applied.notice, message.type === "mcp_server_status");
        if (applied.archivedCheckpoint !== undefined) showCompressionNotice(applied.archivedCheckpoint);
        if (applied.approval !== undefined) setApproval(applied.approval);
        if (applied.question !== undefined) {
          setQuestion(applied.question);
          setQuestionDraft("");
        }
        if (applied.permissionPreset !== undefined) setPermissionPreset(applied.permissionPreset);
        if (applied.usage !== undefined) onUsageChange(applied.usage);
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
        if (runningRef.current && !attachmentReplacedRef.current) {
          showNotice({
            level: "error",
            text: "连接已断开，Nosis 仍在后台运行，正在重新连接。",
          });
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
        if (runningRef.current) {
          showNotice({ level: "error", text: "无法连接 Nosis。" });
        }
      },
    });
    socketRef.current = socket;
    socketModelRef.current = model;
    return socket;
  }

  function takeOverAttachment() {
    if (attaching || socketRef.current !== null) return;
    setAttaching(true);
    connect({ attachOnly: true, takeover: true });
  }

  async function onNew(message: AppendMessage) {
    const text = message.content.filter((part) => part.type === "text").map((part) => part.text).join("\n").trim();
    if (!text && pendingFiles.length === 0) return;
    if (runningRef.current) {
      const turnId = activeTurnIdRef.current;
      if (pendingFiles.length > 0) {
        showNotice({ level: "info", text: "任务运行中不能发送图片；待发送内容已保留，请停止或等待当前任务结束。" }, true);
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

    let attachments: ImageAttachment[] = [];
    if (pendingFiles.length) {
      try {
      attachments = await uploadAttachments(pendingFiles, session.session_id);
      } catch (error) {
        showNotice({ level: "error", text: String(error) });
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
    setRunning(true);
    onBusyChange(true);
    if (noticeTimeoutRef.current) clearTimeout(noticeTimeoutRef.current);
    setNotice(null);
    // Tokens belong to the turn that is running, not to the previous one.
    onUsageChange(null);

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
    const socket = socketRef.current ?? connect();
    socket.send({ type: "permission_set", preset });
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
    setWorkspaceSaving(true);
    try {
      const savedWorkspace = await updateSessionWorkspace(session.session_id, nextWorkspace);
      setWorkspaceDraft(savedWorkspace);
      onWorkspaceChange?.(savedWorkspace);
      // Detach this session. The next turn starts/attaches its runtime with
      // the newly selected workspace; no other Chat is affected.
      const socket = socketRef.current;
      socketRef.current = null;
      socketModelRef.current = "";
      eventCounterRef.current = 0;
      socket?.close();
      showNotice({ level: "info", text: `工作区已切换为 ${savedWorkspace}` }, true);
    } catch (error) {
      showNotice({ level: "error", text: String(error) });
    } finally {
      setWorkspaceSaving(false);
    }
  }

  async function chooseNewWorkspace() {
    try {
      const selected = await selectWorkspace();
      if (!selected) return;
      setWorkspaceEditing(false);
      await saveWorkspace(selected);
    } catch (error) {
      showNotice({ level: "error", text: String(error) });
    }
  }

  /**
   * Browsers expose pasted screenshots/images through clipboardData.items,
   * rather than the textarea's value.  Capture those files and feed them
   * through the same pending-attachment queue used by the paperclip picker.
   * Keep normal text paste untouched; only suppress the browser default when
   * the clipboard contains an image and no textual payload.
   */
  function onPaste(event: ClipboardEvent<HTMLTextAreaElement>) {
    const files = Array.from(event.clipboardData.items)
      .filter((item) => item.kind === "file" && item.type.startsWith("image/"))
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
    // Stop assistant-ui before it clears the draft; images cannot be steered
    // into an already running turn, so the whole pending message stays intact.
    event.preventDefault();
    showNotice({ level: "info", text: "任务运行中不能发送图片；待发送内容已保留，请停止或等待当前任务结束。" }, true);
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

  const controlsDisabled = inputDisabled || attaching || attachmentReplaced || (runtimeActive && socketRef.current === null);
  const runtime = useExternalStoreRuntime({
    messages,
    convertMessage: (message) => message,
    isRunning: running,
    isDisabled: controlsDisabled,
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
  const availableWorkspaces = Array.from(new Set([...workspaceOptions, workspaceDraft].filter(Boolean)));

  return <AssistantRuntimeProvider runtime={runtime}>
    <ThreadPrimitive.Root className="thread">
      <ThreadPrimitive.Viewport className="thread-viewport">
        <ThreadPrimitive.Empty>
          <div className="welcome"><div className="welcome-symbol"><Terminal size={26} /></div><div className="eyebrow">YOUR PERSONAL AGENT</div><h1>一起，把想法变成现实。</h1><p>聊聊你的项目，或者交给 Nosis 一个任务。</p></div>
        </ThreadPrimitive.Empty>
        <div className="messages"><ThreadPrimitive.Messages components={{ UserMessage, AssistantMessage }} /></div>
      </ThreadPrimitive.Viewport>
      <div className="composer-area">
        {attachmentReplaced && <div className="attachment-replaced" role="status"><span>{running ? "此会话已在另一个页面接管。任务仍在后台运行，本页已暂停实时更新。" : "此会话已在另一个页面接管，本页已暂停实时更新。"}</span><button type="button" onClick={takeOverAttachment} disabled={attaching}>{attaching ? "正在接管…" : "在此页面接管"}</button></div>}
        {!attachmentReplaced && approval && <div className="approval-card" role="region" aria-label="工具执行确认"><div className="approval-title"><ShieldCheck size={17} /> 允许执行此工具调用？</div><pre>{approval.command}</pre><div className="approval-actions"><button onClick={() => respond(false)}>拒绝</button><button className="approve-button" onClick={() => respond(true)}>允许执行</button></div></div>}
        {!attachmentReplaced && question && <div className="question-card" role="region" aria-label="需要你的选择">
          <div className="question-title"><MessageCircleQuestion size={18} /><span>{question.question}</span></div>
          <div className="question-options">{question.options.map((option) => <button type="button" className="question-option" key={option.id} onClick={() => answerQuestion({ option_id: option.id })}>
            <span className="question-option-heading"><span>{option.label}</span>{option.recommended && <span className="recommended-badge">推荐</span>}</span>
            {option.description && <span className="question-option-description">{option.description}</span>}
          </button>)}</div>
          {question.allow_free_text && <form className="question-free-text" onSubmit={(event) => { event.preventDefault(); const text = questionDraft.trim(); if (text) answerQuestion({ text }); }}>
            <input value={questionDraft} onChange={(event) => setQuestionDraft(event.target.value)} placeholder="输入其他答案…" aria-label="其他答案" />
            <button type="submit" disabled={!questionDraft.trim()}>提交</button>
          </form>}
        </div>}
        {notice && <div className={notice.level === "error" ? "error-banner" : "notice-banner"} role="alert">{notice.text}</div>}
        {running && !attachmentReplaced && <div className="activity" role="status"><LoaderCircle size={13} className="spin" />{approval ? "等待你的确认" : question ? "等待你的选择" : pendingSteers ? `Nosis 正在处理… ${pendingSteers} 条引导待应用` : "Nosis 正在处理…"}
          {!approval && !question && <button className="stop-button" aria-label="停止执行" onClick={() => { const turnId = activeTurnIdRef.current; if (turnId) socketRef.current?.send({ type: "cancel", turn_id: turnId }); }}><Square size={11} /> 停止</button>}
        </div>}
        {pendingFiles.length > 0 && <div className="attachment-list" aria-label="待发送图片">{pendingFiles.map((file, index) => <PendingAttachment key={`${file.name}-${file.lastModified}-${index}`} file={file} onRemove={() => setPendingFiles((files) => files.filter((_, itemIndex) => itemIndex !== index))} />)}</div>}
        {compressionNotice && <div className="compression-notice" role="status">{compressionNotice}</div>}
        <ComposerPrimitive.Root className="composer" onSubmit={onComposerSubmit}><div ref={workspacePickerRef} className="workspace-picker-wrap"><button type="button" className="workspace-picker" onClick={() => setWorkspaceEditing((value) => { if (value && !workspaceDraft.trim()) setWorkspaceDraft(session.workspace ?? ""); return !value; })} disabled={controlsDisabled || running} aria-expanded={workspaceEditing}><span className="workspace-picker-icon">⌂</span><span className="workspace-picker-value">{workspaceDraft || "选择项目"}</span><ChevronDown size={14} /></button>{workspaceEditing && <div className="workspace-menu" role="menu"><div className="workspace-menu-heading">选择工作区</div>{availableWorkspaces.map((path) => <button type="button" role="menuitem" className={`workspace-option ${path === workspaceDraft ? "selected" : ""}`} key={path} onClick={() => { void saveWorkspace(path); setWorkspaceEditing(false); }} disabled={controlsDisabled || running || workspaceSaving} title={path}><span className="workspace-option-path">{path}</span></button>)}<div className="workspace-menu-divider" /><button type="button" role="menuitem" className="workspace-new-option" onClick={() => { void chooseNewWorkspace(); }} disabled={controlsDisabled || running || workspaceSaving}>＋ 新建工作区</button></div>}</div><ComposerPrimitive.Input placeholder="Ask Nosis…" aria-label="消息" rows={2} autoFocus submitMode="none" onKeyDown={onComposerKeyDown} onCompositionStart={onCompositionStart} onCompositionEnd={onCompositionEnd} onPaste={onPaste} /><div className="composer-bottom">
          <button type="button" className="attachment-button" aria-label="添加图片" title="添加图片" disabled={controlsDisabled || running} onClick={() => fileInputRef.current?.click()}><Paperclip size={15} /></button>
          <input ref={fileInputRef} className="attachment-input" type="file" accept="image/*" multiple onChange={(event) => { setPendingFiles((files) => [...files, ...Array.from(event.target.files ?? [])]); event.currentTarget.value = ""; }} />
          <label className="model-selector" title={models.find((option) => option.id === model)?.model}>
            <select aria-label="选择模型" value={model} disabled={controlsDisabled || running} onChange={(event) => onModelChange(event.target.value)}>
              {models.map((option) => <option key={option.id} value={option.id}>{option.model}</option>)}
            </select><ChevronDown size={12} />
          </label>
          <label className="permission-selector" title="权限模式">
            <ShieldCheck size={13} />
            <select aria-label="权限模式" value={permissionPreset} disabled={controlsDisabled} onChange={(event) => changePermissionPreset(event.target.value as PermissionPreset)}>
              <option value="ask_for_approval">请求批准</option>
              <option value="full_access">完全访问</option>
            </select><ChevronDown size={12} />
          </label>
          <span className="composer-hint">{running ? "Enter 引导当前任务" : "Enter 发送"} · Shift + Enter 换行</span><ComposerPrimitive.Send className="send-button" aria-label={running ? "发送引导" : "发送消息"}><ArrowUp size={19} /></ComposerPrimitive.Send></div></ComposerPrimitive.Root>
        <div className="composer-footer">Nosis · 你的项目搭档</div>
      </div>
    </ThreadPrimitive.Root>
  </AssistantRuntimeProvider>;
}
