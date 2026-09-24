import { useEffect, useRef, useState, type ClipboardEvent, type FormEvent, type KeyboardEvent } from "react";
import { ComposerPrimitive } from "@assistant-ui/react";
import { AlertTriangle, ArrowUp, ChevronDown, LoaderCircle, MessageCircleQuestion, Paperclip, ShieldCheck, X } from "lucide-react";
import type { PermissionPreset } from "@nosis/protocol";
import { shouldBlockRunningAttachmentSubmit, shouldSubmitAttachmentOnly, shouldSubmitComposerEnter } from "./composerState";
import { PendingAttachment } from "./Messages/Attachments";
import { ContextWindowIndicator, RuntimeIndicators } from "./RuntimeIndicators";
import type { SessionRuntime } from "./useSessionRuntime";

export function Composer({ runtime }: { runtime: SessionRuntime }) {
  const [workspaceEditing, setWorkspaceEditing] = useState(false);
  const workspacePickerRef = useRef<HTMLDivElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const compositionEndTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const composingRef = useRef(false);
  const questionFocusOptionId = runtime.question?.options.find((option) => option.recommended)?.id
    ?? runtime.question?.options[0]?.id;

  useEffect(() => {
    if (!workspaceEditing) return;
    const closeOnOutsidePointer = (event: PointerEvent) => {
      if (!workspacePickerRef.current?.contains(event.target as Node)) setWorkspaceEditing(false);
    };
    document.addEventListener("pointerdown", closeOnOutsidePointer);
    return () => document.removeEventListener("pointerdown", closeOnOutsidePointer);
  }, [workspaceEditing]);

  useEffect(() => () => {
    if (compositionEndTimeoutRef.current) clearTimeout(compositionEndTimeoutRef.current);
  }, []);

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
    runtime.addPendingFiles(files);
    if (!event.clipboardData.getData("text/plain")) event.preventDefault();
  }

  function onComposerKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    const nativeEvent = event.nativeEvent as globalThis.KeyboardEvent;
    if (!shouldSubmitComposerEnter(nativeEvent, composingRef.current)) return;
    event.preventDefault();
    event.currentTarget.closest("form")?.requestSubmit();
  }

  function onComposerSubmit(event: FormEvent<HTMLFormElement>) {
    if (shouldBlockRunningAttachmentSubmit(runtime.running, runtime.pendingFiles.length)) {
      // Stop assistant-ui before it clears the draft; attachments cannot be steered
      // into an already running turn, so the whole pending message stays intact.
      event.preventDefault();
      runtime.notifyRunningAttachmentBlocked();
      return;
    }
    const text = event.currentTarget.querySelector("textarea")?.value ?? "";
    if (!shouldSubmitAttachmentOnly(text, runtime.pendingFiles.length)) return;
    event.preventDefault();
    void runtime.submitMessage("");
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

  return <div className="composer-area">
    {runtime.attachmentReplaced && <div className="attachment-replaced" role="status"><span>此会话由另一个页面控制，可在此页面手动接管。</span><button type="button" onClick={runtime.takeOverAttachment} disabled={runtime.attaching}>{runtime.attaching ? "正在接管…" : "在此页面接管"}</button></div>}
    {!runtime.attachmentReplaced && runtime.approval && <div className="approval-card" role="region" aria-label="工具执行确认"><div className="approval-title"><ShieldCheck size={17} /> 允许执行此工具调用？</div><pre>{runtime.approval.command}</pre><div className="approval-actions"><button onClick={() => runtime.respondToApproval(false)}>拒绝</button><button className="approve-button" onClick={() => runtime.respondToApproval(true)}>允许执行</button></div></div>}
    {!runtime.attachmentReplaced && runtime.question && <div className="question-card" role="region" aria-label="需要你的选择">
      <div className="question-title"><MessageCircleQuestion size={18} /><span>{runtime.question.question}</span></div>
      <div className="question-options">{runtime.question.options.map((option) => <button type="button" className="question-option" key={option.id} autoFocus={option.id === questionFocusOptionId} onClick={() => runtime.answerQuestion({ option_id: option.id })}>
        <span className="question-option-heading"><span>{option.label}</span>{option.recommended && <span className="recommended-badge">推荐</span>}</span>
        {option.description && <span className="question-option-description">{option.description}</span>}
      </button>)}</div>
      {runtime.question.allow_free_text && <form className="question-free-text" onSubmit={(event) => {
        event.preventDefault();
        const text = runtime.questionDraft.trim();
        if (text) runtime.answerQuestion({ text });
      }}>
        <input value={runtime.questionDraft} onChange={(event) => runtime.setQuestionDraft(event.target.value)} placeholder="输入其他答案…" aria-label="其他答案" />
        <button type="submit" disabled={!runtime.questionDraft.trim()}>提交</button>
      </form>}
    </div>}
    {runtime.alerts.length > 0 && <div className="feedback-alerts">{runtime.alerts.map((item) => <div className={`feedback-alert ${item.level}`} role="alert" key={item.id}><AlertTriangle size={15} /><span>{item.text}</span><button type="button" aria-label="关闭提示" onClick={() => runtime.dismissAlert(item.id)}><X size={13} /></button></div>)}</div>}
    {runtime.toast && <div className={`feedback-toast ${runtime.toast.level}`} role="status">{runtime.toast.text}</div>}
    {!runtime.attachmentReplaced && <RuntimeIndicators
      running={runtime.running}
      reconnecting={runtime.reconnecting}
      runtimePhase={runtime.runtimePhase}
      pendingSteers={runtime.pendingSteers}
      approval={runtime.approval}
      question={runtime.question}
      plan={runtime.plan}
      jobs={runtime.jobs}
      onStop={runtime.stop}
    />}
    {runtime.pendingFiles.length > 0 && <div className="attachment-list" aria-label="待发送附件">{runtime.pendingFiles.map((file, index) => <PendingAttachment key={`${file.name}-${file.lastModified}-${index}`} file={file} onRemove={() => runtime.removePendingFile(index)} />)}</div>}
    <ComposerPrimitive.Root className="composer" onSubmit={onComposerSubmit}>
      <div ref={workspacePickerRef} className="workspace-picker-wrap">
        <button type="button" className="workspace-picker" onClick={() => setWorkspaceEditing((value) => !value)} disabled={runtime.controlsDisabled || runtime.sessionRunning} aria-expanded={workspaceEditing}>
          <span className="workspace-picker-icon">⌂</span><span className="workspace-picker-value">{runtime.displayedWorkspace || "选择项目"}</span>
          {runtime.workspaceSaving ? <LoaderCircle size={14} className="spin" aria-label="正在切换工作区" /> : <ChevronDown size={14} />}
        </button>
        {workspaceEditing && <div className="workspace-menu" role="menu">
          <div className="workspace-menu-heading">选择工作区</div>
          {runtime.availableWorkspaces.map((path) => <button type="button" role="menuitem" className={`workspace-option ${path === runtime.displayedWorkspace ? "selected" : ""}`} key={path} onClick={() => { void runtime.saveWorkspace(path); setWorkspaceEditing(false); }} disabled={runtime.controlsDisabled || runtime.sessionRunning || runtime.workspaceSaving} title={path}><span className="workspace-option-path">{path}</span></button>)}
          <div className="workspace-menu-divider" />
          <button type="button" role="menuitem" className="workspace-new-option" onClick={() => { setWorkspaceEditing(false); void runtime.chooseNewWorkspace(); }} disabled={runtime.controlsDisabled || runtime.sessionRunning || runtime.workspaceSaving}>＋ 新建工作区</button>
          <button type="button" role="menuitem" className="workspace-new-option" onClick={() => { setWorkspaceEditing(false); void runtime.chooseScratchWorkspace(); }} disabled={runtime.controlsDisabled || runtime.sessionRunning || runtime.workspaceSaving}>＋ 临时工作区</button>
        </div>}
      </div>
      <ComposerPrimitive.Input
        placeholder={runtime.question ? "请先回答上方问题…" : runtime.approval ? "请先处理上方确认…" : "Ask Nosis…"}
        aria-label="消息"
        rows={2}
        autoFocus
        submitMode="none"
        disabled={runtime.composerGate.inputDisabled}
        onKeyDown={onComposerKeyDown}
        onCompositionStart={onCompositionStart}
        onCompositionEnd={onCompositionEnd}
        onPaste={onPaste}
      />
      <div className="composer-bottom">
        <button type="button" className="attachment-button" aria-label="添加附件" title="添加附件" disabled={runtime.controlsDisabled || runtime.sessionRunning || runtime.uploadingAttachments} onClick={() => fileInputRef.current?.click()}><Paperclip size={15} /></button>
        <input ref={fileInputRef} className="attachment-input" type="file" multiple onChange={(event) => {
          runtime.addPendingFiles(Array.from(event.target.files ?? []));
          event.currentTarget.value = "";
        }} />
        <label className="model-selector" title={runtime.providers.find((option) => option.id === runtime.displayedProvider)?.model}>
          <select aria-label="选择模型" value={runtime.displayedProvider} disabled={runtime.controlsDisabled || runtime.sessionRunning || runtime.providerSaving} onChange={(event) => runtime.changeProvider(event.target.value)}>
            {runtime.providers.map((option) => <option key={option.id} value={option.id}>{option.model}</option>)}
          </select>{runtime.providerSaving ? <LoaderCircle size={12} className="spin" aria-label="正在切换模型" /> : <ChevronDown size={12} />}
        </label>
        <label className="permission-selector" title="权限模式">
          <ShieldCheck size={13} />
          <select aria-label="权限模式" value={runtime.displayedPermissionPreset} disabled={runtime.controlsDisabled || runtime.permissionSaving} onChange={(event) => runtime.changePermissionPreset(event.target.value as PermissionPreset)}>
            <option value="ask_for_approval">请求批准</option>
            <option value="workspace_access">工作区访问</option>
            <option value="full_access">完全访问</option>
          </select>{runtime.permissionSaving ? <LoaderCircle size={12} className="spin" aria-label="正在保存权限" /> : <ChevronDown size={12} />}
        </label>
        <div className="composer-actions"><ContextWindowIndicator window={runtime.contextWindow} />{runtime.pendingFiles.length > 0
          ? <button type="submit" className="send-button" aria-label="发送消息" disabled={runtime.composerGate.sendDisabled || runtime.sessionRunning}><ArrowUp size={19} /></button>
          : <ComposerPrimitive.Send className="send-button" aria-label={runtime.running ? "发送引导" : "发送消息"}><ArrowUp size={19} /></ComposerPrimitive.Send>}
        </div>
      </div>
    </ComposerPrimitive.Root>
    <div className="composer-footer">Nosis · 你的项目搭档</div>
  </div>;
}
