import { useCallback, useMemo, useRef, useState, useEffect } from "react";
import { useExternalStoreRuntime, type AppendMessage } from "@assistant-ui/react";
import { runtimeIsActive, type ContextWindow, type PermissionPreset, type PlanSnapshot, type RuntimePhase, type UserQuestion } from "@nosis/protocol";
import { createScratchWorkspace, get, releaseActiveSession, selectWorkspace, sessionUrl, uploadAttachments, type ModelOption, type Session, type UserAttachment } from "../api";
import { SessionSocket } from "../session";
import { applyMessage, isTurnActivity, toMessages, type Feedback, type TranscriptItem } from "../transcript";
import { composerConnectionGate } from "./composerState";
import { jobKindLabel, shouldShowPlan, updateBackgroundJobs, userMessagePreview, type Approval, type BackgroundJob } from "./runtimeState";

export type SessionRuntimeOptions = {
  session: Session;
  selected: boolean;
  contextWindow: ContextWindow | null;
  workspaceOptions: string[];
  backgroundActive: boolean;
  providers: ModelOption[];
  provider: string;
  onProviderChange: (provider: string) => void;
  onBusyChange: (busy: boolean) => void;
  onContextWindowChange: (window: ContextWindow | null) => void;
  onSessionAvailable: () => void;
  onTurnEnd: () => void;
  onWorkspaceChange?: (workspace: string) => void;
};

export function useSessionRuntime({
  session,
  selected,
  contextWindow,
  workspaceOptions,
  backgroundActive,
  providers,
  provider,
  onProviderChange,
  onBusyChange,
  onContextWindowChange,
  onSessionAvailable,
  onTurnEnd,
  onWorkspaceChange,
}: SessionRuntimeOptions) {
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
  const [permissionPreset, setPermissionPreset] = useState<PermissionPreset>(session.permission_preset);
  const [pendingPermissionPreset, setPendingPermissionPreset] = useState<PermissionPreset | null>(null);
  const [pendingProvider, setPendingProvider] = useState<string | null>(null);
  const [questionDraft, setQuestionDraft] = useState("");
  const [pendingFiles, setPendingFiles] = useState<File[]>([]);
  const [uploadingAttachments, setUploadingAttachments] = useState(false);
  const [workspaceDraft, setWorkspaceDraft] = useState(session.workspace ?? "");
  const [pendingWorkspace, setPendingWorkspace] = useState<string | null>(null);
  const socketRef = useRef<SessionSocket | null>(null);
  const socketModelRef = useRef("");
  const reconnectTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const mountedRef = useRef(true);
  const toastTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const turnCounter = useRef(0);
  const steerCounter = useRef(0);
  const activeTurnIdRef = useRef<string | null>(null);
  const eventCounterRef = useRef(session.event_sequence ?? 0);
  const [attachmentId] = useState(() => crypto.randomUUID());
  // Socket callbacks fire outside React's render, so the transcript and
  // turn state they fold onto are kept in refs.
  const itemsRef = useRef(items);
  const runningRef = useRef(false);
  const attachmentReplacedRef = useRef(false);
  const attachmentSubmitRef = useRef(false);
  const awaitingSessionActivityRef = useRef(false);

  const messages = useMemo(() => toMessages(items, session.session_id), [items, session.session_id]);
  const turnPreviews = useMemo(() => messages
    .filter((message) => message.role === "user")
    .map((message) => userMessagePreview(message)), [messages]);
  const permissionSaving = pendingPermissionPreset !== null;
  const providerSaving = pendingProvider !== null;
  const workspaceSaving = pendingWorkspace !== null;
  const configurationPending = permissionSaving || providerSaving || workspaceSaving;
  const displayedProvider = pendingProvider ?? provider;
  const displayedWorkspace = pendingWorkspace ?? workspaceDraft;
  const interactionActive = approval !== null || question !== null;
  const backgroundDisconnected = backgroundActive && socketRef.current === null;
  const composerGate = composerConnectionGate({
    attaching: attaching || uploadingAttachments,
    configurationPending,
    attachmentReplaced,
    interactionActive,
    backgroundDisconnected,
  });
  const controlsDisabled = attachmentReplaced || backgroundDisconnected;
  const sessionRunning = running || backgroundActive;

  useEffect(() => {
    if (session.workspace) setWorkspaceDraft(session.workspace);
  }, [session.workspace]);

  const showItems = useCallback((next: TranscriptItem[]) => {
    itemsRef.current = next;
    setItems(next);
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      if (reconnectTimeoutRef.current) clearTimeout(reconnectTimeoutRef.current);
      socketRef.current?.close();
      socketRef.current = null;
      if (toastTimeoutRef.current) clearTimeout(toastTimeoutRef.current);
    };
  }, []);

  useEffect(() => {
    const releaseOnPageLeave = () => {
      if (!runningRef.current) {
        void releaseActiveSession(session.session_id, socketModelRef.current || provider, attachmentId).catch(() => undefined);
      }
    };
    window.addEventListener("pagehide", releaseOnPageLeave);
    return () => window.removeEventListener("pagehide", releaseOnPageLeave);
  }, [attachmentId, provider, session.session_id]);

  // A selected Chat owns a lightweight Session connection. Hidden Chats keep
  // one only while their turn remains active.
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
        showToast({ kind: "toast", level: "info", text: "会话正在执行任务，本次模型或工作区切换未应用，请在任务结束后重试。" });
      },
      onMessage: (message) => {
        // A socket that was replaced during a model switch may still have
        // messages queued in the browser event loop. Ignore those messages
        // so stale transcript, feedback, usage, and turn callbacks cannot
        // mutate the active connection's state.
        if (socketRef.current !== socket) return;
        if (message.type === "attachment_replaced" || message.type === "fatal") clearPendingSettings();
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
            showAlert({ kind: "alert", id: "runtime-warnings", level: "warning", text: message.runtime_warnings.join("\n") });
          }
          return;
        }
        if (typeof message.event_sequence === "number") eventCounterRef.current = message.event_sequence;
        if (message.type === "user_steer_applied" || message.type === "user_steer_rejected") {
          setPendingSteers((count) => Math.max(0, count - 1));
        }
        if (message.type === "job_status") {
          setJobs((current) => updateBackgroundJobs(current, message));
          if (message.status === "failed") {
            showAlert({ kind: "alert", id: `job:${message.job_id}`, level: "error", text: `${jobKindLabel(message.kind)}任务失败。` });
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
              connect({ attachOnly: true });
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
        if (!runningRef.current) showAlert({ kind: "alert", id: "connection", level: "error", text: "无法连接 Nosis。" });
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

  async function submitMessage(text: string) {
    if (!text && pendingFiles.length === 0) return;
    if (attachmentSubmitRef.current) return;
    if (runningRef.current) {
      const turnId = activeTurnIdRef.current;
      if (pendingFiles.length > 0) {
        showToast({ kind: "toast", level: "info", text: "任务运行中不能发送附件；待发送内容已保留，请停止或等待当前任务结束。" });
        return;
      }
      if (!turnId) return;
      steerCounter.current += 1;
      socketRef.current?.send({ type: "user_steer", turn_id: turnId, steer_id: `steer-${steerCounter.current}`, text });
      setPendingSteers((count) => count + 1);
      return;
    }
    const startsSession = !itemsRef.current.some((item) => item.role === "user" && item.origin !== "tool_media");
    let attachments: UserAttachment[] = [];
    if (pendingFiles.length) {
      attachmentSubmitRef.current = true;
      setUploadingAttachments(true);
      try {
        attachments = await uploadAttachments(pendingFiles, session.session_id);
      } catch (error) {
        showAlert({ kind: "alert", id: "attachment-upload", level: "error", text: String(error) });
        return;
      } finally {
        attachmentSubmitRef.current = false;
        setUploadingAttachments(false);
      }
      setPendingFiles([]);
    }
    showItems([...itemsRef.current, { role: "user", content: attachments.length ? [{ type: "text", text }, ...attachments] : text }]);
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
    socket.send({ type: "user_turn", turn_id: activeTurnIdRef.current, text, ...(attachments.length ? { attachments } : {}) });
  }

  async function onNew(message: AppendMessage) {
    const text = message.content.filter((part) => part.type === "text").map((part) => part.text).join("\n").trim();
    await submitMessage(text);
  }

  function respondToApproval(approved: boolean) {
    if (!approval) return;
    socketRef.current?.send({ type: "approval_response", request_id: approval.requestId, approved });
    setApproval(null);
  }

  function changePermissionPreset(preset: PermissionPreset) {
    if (preset === permissionPreset) return;
    setPendingPermissionPreset(preset);
    (socketRef.current ?? connect()).send({ type: "permission_set", preset });
  }

  function changeProvider(nextProvider: string) {
    setPendingProvider(nextProvider);
    (socketRef.current ?? connect()).send({ type: "provider_set", provider: nextProvider });
  }

  function answerQuestion(answer: { option_id: string } | { text: string }) {
    if (!question) return;
    socketRef.current?.send({ type: "user_question_response", request_id: question.request_id, ...answer });
    setQuestion(null);
    setQuestionDraft("");
  }

  async function saveWorkspace(value = workspaceDraft) {
    const nextWorkspace = value.trim();
    if (!nextWorkspace || nextWorkspace === session.workspace) return;
    setPendingWorkspace(nextWorkspace);
    try {
      (socketRef.current ?? connect()).send({ type: "workspace_set", workspace: nextWorkspace });
    } catch (error) {
      showAlert({ kind: "alert", id: "workspace-update", level: "error", text: String(error) });
      setPendingWorkspace(null);
    }
  }

  async function chooseNewWorkspace() {
    try {
      const selectedWorkspace = await selectWorkspace();
      if (selectedWorkspace) await saveWorkspace(selectedWorkspace);
    } catch (error) {
      showAlert({ kind: "alert", id: "workspace-picker", level: "error", text: String(error) });
    }
  }

  async function chooseScratchWorkspace() {
    try {
      await saveWorkspace(await createScratchWorkspace(session.session_id));
    } catch (error) {
      showAlert({ kind: "alert", id: "scratch-workspace", level: "error", text: String(error) });
    }
  }

  function stop() {
    const turnId = activeTurnIdRef.current;
    if (turnId) socketRef.current?.send({ type: "cancel", turn_id: turnId });
  }

  const assistantRuntime = useExternalStoreRuntime({
    messages,
    convertMessage: (message) => message,
    isRunning: running,
    isDisabled: composerGate.inputDisabled,
    isSendDisabled: composerGate.sendDisabled,
    onNew,
    queue: { items: [], steerItems: [], enqueue: onNew, steer: onNew, move: () => {}, edit: () => {}, remove: () => {} },
  });

  return {
    assistantRuntime,
    turnPreviews,
    running,
    runtimePhase,
    pendingSteers,
    jobs: Object.values(jobs),
    attaching,
    attachmentReplaced,
    toast,
    alerts: Object.values(alerts),
    reconnecting,
    approval,
    question,
    questionDraft,
    setQuestionDraft,
    plan: shouldShowPlan(plan) ? plan : null,
    displayedPermissionPreset: pendingPermissionPreset ?? permissionPreset,
    displayedProvider,
    displayedWorkspace,
    permissionSaving,
    providerSaving,
    workspaceSaving,
    uploadingAttachments,
    pendingFiles,
    composerGate,
    controlsDisabled,
    sessionRunning,
    availableWorkspaces: Array.from(new Set([...workspaceOptions, displayedWorkspace].filter(Boolean))),
    addPendingFiles: (files: File[]) => setPendingFiles((current) => [...current, ...files]),
    removePendingFile: (index: number) => setPendingFiles((current) => current.filter((_, itemIndex) => itemIndex !== index)),
    notifyRunningAttachmentBlocked: () => showToast({ kind: "toast", level: "info", text: "任务运行中不能发送附件；待发送内容已保留，请停止或等待当前任务结束。" }),
    dismissAlert: (id: string) => setAlerts((current) => {
      const next = { ...current };
      delete next[id];
      return next;
    }),
    submitMessage,
    takeOverAttachment,
    respondToApproval,
    answerQuestion,
    changePermissionPreset,
    changeProvider,
    saveWorkspace,
    chooseNewWorkspace,
    chooseScratchWorkspace,
    stop,
    contextWindow,
    providers,
  };
}

export type SessionRuntime = ReturnType<typeof useSessionRuntime>;
