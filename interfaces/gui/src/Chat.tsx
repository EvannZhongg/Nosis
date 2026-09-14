import { useCallback, useEffect, useMemo, useRef, useState, type ClipboardEvent } from "react";
import {
  AssistantRuntimeProvider, ComposerPrimitive, MessagePrimitive,
  ThreadPrimitive, useAuiState, useExternalStoreRuntime,
  type AppendMessage, type ReasoningMessagePartProps, type ToolCallMessagePartProps,
} from "@assistant-ui/react";
import { MarkdownTextPrimitive } from "@assistant-ui/react-markdown";
import { ArrowUp, Check, ChevronDown, ChevronRight, LoaderCircle, Paperclip, ShieldCheck, Square, Terminal, X } from "lucide-react";
import remarkGfm from "remark-gfm";
import { get, selectWorkspace, sessionUrl, updateSessionWorkspace, uploadAttachments, type ImageAttachment, type ModelOption, type Session } from "./api";
import { SessionSocket } from "./session";
import { applyMessage, toMessages, type Notice, type TranscriptItem } from "./transcript";
import type { Usage } from "@nosis/protocol";

type Approval = {
  requestId: string;
  command: string;
  kind?: 'shell' | 'mcp';
  server?: string;
  toolName?: string;
};

const MARKDOWN_PLUGINS = [remarkGfm];

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

export function Chat({ session, workspaceOptions = [], disabled, models, model, onModelChange, onBusyChange, onUsageChange, onTurnEnd, onWorkspaceChange }: {
  session: Session; disabled: boolean; onBusyChange: (busy: boolean) => void; onTurnEnd: () => void;
  onUsageChange: (usage: Usage | null) => void;
  models: ModelOption[]; model: string; onModelChange: (model: string) => void;
  workspaceOptions?: string[];
  onWorkspaceChange?: (workspace: string) => void;
}) {
  const [items, setItems] = useState<TranscriptItem[]>(session.items);
  const [running, setRunning] = useState(false);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [approval, setApproval] = useState<Approval | null>(null);
  const [pendingFiles, setPendingFiles] = useState<File[]>([]);
  const [workspaceDraft, setWorkspaceDraft] = useState(session.workspace ?? "");
  const [workspaceSaving, setWorkspaceSaving] = useState(false);
  const [workspaceEditing, setWorkspaceEditing] = useState(false);
  const workspacePickerRef = useRef<HTMLDivElement | null>(null);
  const socketRef = useRef<SessionSocket | null>(null);
  const noticeTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const turnCounter = useRef(0);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const messages = useMemo(() => toMessages(items, session.session_id), [items, session.session_id]);

  // Socket callbacks fire outside React's render, so the transcript and
  // turn state they fold onto are kept in refs.
  const itemsRef = useRef(items);
  const runningRef = useRef(false);

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

  // A new provider takes effect on the next connection; the transcript
  // is reloaded from the stored session, so context carries over.
  useEffect(() => {
    socketRef.current?.close();
    socketRef.current = null;
    // Establish the bridge as soon as the session is mounted.  The GUI and
    // runtime therefore come up together; sending a prompt is no longer what
    // starts the agent process. Defer the side effect by one task so React
    // StrictMode can run its setup/cleanup probe without spawning a throwaway
    // bridge and its MCP servers.
    let connectionTimer: ReturnType<typeof setTimeout> | null = null;
    if (!disabled && model) {
      connectionTimer = setTimeout(() => {
        if (socketRef.current === null) connect();
      }, 0);
    }
    return () => {
      if (connectionTimer !== null) clearTimeout(connectionTimer);
      socketRef.current?.close();
      socketRef.current = null;
      if (noticeTimeoutRef.current) clearTimeout(noticeTimeoutRef.current);
    };
  }, [model, disabled, session.session_id]);

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

  const endTurn = useCallback(async () => {
    runningRef.current = false;
    setRunning(false);
    onBusyChange(false);
    setApproval(null);
    try {
      // The protocol omits tool output; the stored session has it.
      const stored = await get<Session>(sessionUrl(session.session_id));
      // A session without stored items would erase the live transcript.
      if (stored.items.length) showItems(stored.items);
    } catch {
      // A turn that never persisted keeps the live transcript.
    }
    onTurnEnd();
  }, [session.session_id, onBusyChange, onTurnEnd, showItems]);

  function connect(): SessionSocket {
    let socket: SessionSocket;
    socket = new SessionSocket({
      sessionId: session.session_id,
      provider: model,
      onMessage: (message) => {
        // A socket that was replaced during a model switch may still have
        // messages queued in the browser event loop. Ignore those messages
        // so stale transcript, notice, usage, and turn callbacks cannot
        // mutate the active connection's state.
        if (socketRef.current !== socket) return;
        if (message.type === "ready") {
          if (noticeTimeoutRef.current) {
            clearTimeout(noticeTimeoutRef.current);
            noticeTimeoutRef.current = null;
          }
          setNotice(null);
        }
        const applied = applyMessage(itemsRef.current, message);
        showItems(applied.items);
        if (applied.notice) showNotice(applied.notice, message.type === "mcp_server_status");
        if (applied.approval !== undefined) setApproval(applied.approval);
        if (applied.usage !== undefined) onUsageChange(applied.usage);
        if (applied.finished) void endTurn();
      },
      onClose: () => {
        // A stale socket can close after a replacement has already been
        // installed (for example after changing the model). Do not clear
        // the newer connection in that case.
        if (socketRef.current !== socket) return;
        socketRef.current = null;
        if (runningRef.current) {
          showNotice({
            level: "error",
            text: "连接已断开，本轮对话未完成。已执行的工具操作不会撤销。",
          });
          void endTurn();
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
    return socket;
  }

  async function onNew(message: AppendMessage) {
    const text = message.content.filter((part) => part.type === "text").map((part) => part.text).join("\n").trim();
    if ((!text && pendingFiles.length === 0) || runningRef.current) return;

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
    const socket = socketRef.current ?? connect();
    socket.send({
      type: "user_turn",
      turn_id: `turn-${turnCounter.current}`,
      text,
      ...(attachments.length ? { attachments } : {}),
    });
  }

  function respond(approved: boolean) {
    if (!approval) return;
    socketRef.current?.send({ type: "approval_response", request_id: approval.requestId, approved });
    setApproval(null);
  }

  async function saveWorkspace(value = workspaceDraft) {
    const nextWorkspace = value.trim();
    if (!nextWorkspace || nextWorkspace === session.workspace) return;
    setWorkspaceSaving(true);
    try {
      const savedWorkspace = await updateSessionWorkspace(session.session_id, nextWorkspace);
      setWorkspaceDraft(savedWorkspace);
      onWorkspaceChange?.(savedWorkspace);
      // Restart the bridge so prompts, tools and vision provider all use the
      // newly selected workspace for subsequent turns.
      socketRef.current?.close();
      socketRef.current = null;
      if (!disabled && model) connect();
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

  const runtime = useExternalStoreRuntime({ messages, convertMessage: (message) => message, isRunning: running, isDisabled: disabled, onNew });
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
        {approval && <div className="approval-card" role="region" aria-label="工具执行确认"><div className="approval-title"><ShieldCheck size={17} /> 允许执行此工具调用？</div><pre>{approval.command}</pre><div className="approval-actions"><button onClick={() => respond(false)}>拒绝</button><button className="approve-button" onClick={() => respond(true)}>允许执行</button></div></div>}
        {notice && <div className={notice.level === "error" ? "error-banner" : "notice-banner"} role="alert">{notice.text}</div>}
        {running && <div className="activity" role="status"><LoaderCircle size={13} className="spin" />{approval ? "等待你的确认" : "Nosis 正在处理…"}
          {!approval && <button className="stop-button" aria-label="停止执行" onClick={() => socketRef.current?.send({ type: "cancel" })}><Square size={11} /> 停止</button>}
        </div>}
        {pendingFiles.length > 0 && <div className="attachment-list" aria-label="待发送图片">{pendingFiles.map((file, index) => <PendingAttachment key={`${file.name}-${file.lastModified}-${index}`} file={file} onRemove={() => setPendingFiles((files) => files.filter((_, itemIndex) => itemIndex !== index))} />)}</div>}
        <ComposerPrimitive.Root className="composer"><div ref={workspacePickerRef} className="workspace-picker-wrap"><button type="button" className="workspace-picker" onClick={() => setWorkspaceEditing((value) => { if (value && !workspaceDraft.trim()) setWorkspaceDraft(session.workspace ?? ""); return !value; })} disabled={disabled || running} aria-expanded={workspaceEditing}><span className="workspace-picker-icon">⌂</span><span className="workspace-picker-value">{workspaceDraft || "选择项目"}</span><ChevronDown size={14} /></button>{workspaceEditing && <div className="workspace-menu" role="menu"><div className="workspace-menu-heading">选择工作区</div>{availableWorkspaces.map((path) => <button type="button" role="menuitem" className={`workspace-option ${path === workspaceDraft ? "selected" : ""}`} key={path} onClick={() => { void saveWorkspace(path); setWorkspaceEditing(false); }} disabled={disabled || running || workspaceSaving} title={path}><span className="workspace-option-path">{path}</span></button>)}<div className="workspace-menu-divider" /><button type="button" role="menuitem" className="workspace-new-option" onClick={() => { void chooseNewWorkspace(); }} disabled={disabled || running || workspaceSaving}>＋ 新建工作区</button></div>}</div><ComposerPrimitive.Input placeholder="Ask Nosis…" aria-label="消息" rows={2} autoFocus onPaste={onPaste} /><div className="composer-bottom">
          <button type="button" className="attachment-button" aria-label="添加图片" title="添加图片" disabled={disabled || running} onClick={() => fileInputRef.current?.click()}><Paperclip size={15} /></button>
          <input ref={fileInputRef} className="attachment-input" type="file" accept="image/*" multiple onChange={(event) => { setPendingFiles((files) => [...files, ...Array.from(event.target.files ?? [])]); event.currentTarget.value = ""; }} />
          <label className="model-selector" title={models.find((option) => option.id === model)?.model}>
            <select aria-label="选择模型" value={model} disabled={disabled || running} onChange={(event) => onModelChange(event.target.value)}>
              {models.map((option) => <option key={option.id} value={option.id}>{option.model}</option>)}
            </select><ChevronDown size={12} />
          </label>
          <span className="composer-hint">Enter 发送 · Shift + Enter 换行</span><ComposerPrimitive.Send className="send-button" aria-label="发送消息"><ArrowUp size={19} /></ComposerPrimitive.Send></div></ComposerPrimitive.Root>
        <div className="composer-footer">Nosis · 你的项目搭档</div>
      </div>
    </ThreadPrimitive.Root>
  </AssistantRuntimeProvider>;
}
