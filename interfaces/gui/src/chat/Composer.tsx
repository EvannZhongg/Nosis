import { useEffect, useId, useLayoutEffect, useRef, useState, type ClipboardEvent, type FormEvent, type KeyboardEvent } from "react";
import { ComposerPrimitive, unstable_useComposerInput } from "@assistant-ui/react";
import { AlertTriangle, ArrowUp, Check, ChevronDown, Folder, LoaderCircle, MessageCircleQuestion, Paperclip, Plus, RefreshCw, ShieldCheck, X } from "lucide-react";
import type { PermissionPreset } from "@nosis/protocol";
import { shouldBlockRunningAttachmentSubmit, shouldSubmitAttachmentOnly, shouldSubmitComposerEnter } from "./composerState";
import { PendingAttachment } from "./Messages/Attachments";
import { ContextWindowIndicator, RuntimeIndicators } from "./RuntimeIndicators";
import type { SessionRuntime } from "./useSessionRuntime";

export function Composer({ runtime }: { runtime: SessionRuntime }) {
  const { value: composerText, setText: setComposerText, canSend } = unstable_useComposerInput();
  const [commandDismissed, setCommandDismissed] = useState(false);
  const commandMenuId = useId();
  const showRefreshCommand = !commandDismissed && composerText.startsWith("/") && "/refresh".startsWith(composerText);
  const refreshDisabled = !runtime.workspaceSelected || runtime.controlsDisabled || runtime.sessionRunning
    || runtime.attaching || runtime.providerSaving || runtime.workspaceSaving || runtime.permissionSaving || runtime.refreshing;
  const [workspaceEditing, setWorkspaceEditing] = useState(false);
  const [workspaceMenuBounds, setWorkspaceMenuBounds] = useState({ maxHeight: 360, maxWidth: 320 });
  const workspaceMenuId = useId();
  const workspaceGroups = [
    { label: "正式工作区", options: runtime.availableWorkspaces.filter((option) => !option.scratch) },
    { label: "临时工作区", options: runtime.availableWorkspaces.filter((option) => option.scratch) },
  ];
  const workspacePickerRef = useRef<HTMLDivElement | null>(null);
  const feedbackRef = useRef<HTMLDivElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const compositionEndTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const composingRef = useRef(false);
  const questionFocusOptionId = runtime.question?.options.find((option) => option.recommended)?.id
    ?? runtime.question?.options[0]?.id;

  useEffect(() => setCommandDismissed(false), [composerText]);

  useLayoutEffect(() => {
    const feedback = feedbackRef.current!;
    const thread = feedback.parentElement!.parentElement!;
    const updateHeight = () => thread.style.setProperty("--composer-feedback-height", `${feedback.offsetHeight}px`);
    updateHeight();
    const observer = new ResizeObserver(updateHeight);
    observer.observe(feedback);
    return () => {
      observer.disconnect();
      thread.style.removeProperty("--composer-feedback-height");
    };
  }, []);

  function runRefreshCommand() {
    if (runtime.refreshRuntime()) setComposerText("");
  }

  useLayoutEffect(() => {
    if (!workspaceEditing) return;
    const picker = workspacePickerRef.current!;
    const updateBounds = () => {
      const bounds = picker.getBoundingClientRect();
      setWorkspaceMenuBounds({ maxHeight: Math.max(0, bounds.top - 12), maxWidth: window.innerWidth - bounds.left - 12 });
    };
    updateBounds();
    const observer = new ResizeObserver(updateBounds);
    observer.observe(picker.closest(".composer-area")!);
    window.addEventListener("resize", updateBounds);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", updateBounds);
    };
  }, [workspaceEditing]);

  useEffect(() => {
    if (!workspaceEditing) return;
    const closeOnOutsidePointer = (event: PointerEvent) => {
      if (!workspacePickerRef.current?.contains(event.target as Node)) setWorkspaceEditing(false);
    };
    document.addEventListener("pointerdown", closeOnOutsidePointer);
    return () => document.removeEventListener("pointerdown", closeOnOutsidePointer);
  }, [workspaceEditing]);

  useEffect(() => {
    if (runtime.initialPromptDraft !== null) {
      setComposerText(runtime.initialPromptDraft);
    }
  }, [runtime.initialPromptDraft, setComposerText]);

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
    if (composingRef.current || nativeEvent.isComposing || nativeEvent.keyCode === 229) return;
    if (showRefreshCommand) {
      if (event.key === "Tab" && event.shiftKey) return;
      if (event.key === "Escape") {
        event.preventDefault();
        setCommandDismissed(true);
        return;
      }
      if (event.key === "ArrowUp" || event.key === "ArrowDown" || event.key === "Tab") {
        event.preventDefault();
        if (event.key === "Tab") setComposerText("/refresh");
        return;
      }
      if (shouldSubmitComposerEnter(nativeEvent, false)) {
        event.preventDefault();
        if (!refreshDisabled) runRefreshCommand();
        return;
      }
    }
    if (!shouldSubmitComposerEnter(nativeEvent, composingRef.current)) return;
    event.preventDefault();
    event.currentTarget.closest("form")?.requestSubmit();
  }

  function onComposerSubmit(event: FormEvent<HTMLFormElement>) {
    const text = event.currentTarget.querySelector("textarea")?.value ?? "";
    if (text.trim() === "/refresh" || showRefreshCommand) {
      event.preventDefault();
      if (!refreshDisabled) runRefreshCommand();
      return;
    }
    if (runtime.composerGate.sendDisabled) {
      event.preventDefault();
      return;
    }
    if (shouldBlockRunningAttachmentSubmit(runtime.running, runtime.pendingFiles.length)) {
      // Stop assistant-ui before it clears the draft; attachments cannot be steered
      // into an already running turn, so the whole pending message stays intact.
      event.preventDefault();
      runtime.notifyRunningAttachmentBlocked();
      return;
    }
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
    <div ref={feedbackRef} className="composer-feedback">
      {runtime.alerts.length > 0 && <div className="feedback-alerts">{runtime.alerts.map((item) => <div className={`feedback-alert ${item.level}`} role="alert" key={item.id}><AlertTriangle size={15} /><span>{item.text}</span><button type="button" aria-label="关闭提示" onClick={() => runtime.dismissAlert(item.id)}><X size={13} /></button></div>)}</div>}
      {runtime.toast && <div className={`feedback-toast ${runtime.toast.level}`} role="status">{runtime.toast.text}</div>}
      {!runtime.attachmentReplaced && <RuntimeIndicators
        running={runtime.running || runtime.refreshing}
        refreshing={runtime.refreshing}
        reconnecting={runtime.reconnecting}
        runtimePhase={runtime.runtimePhase}
        pendingSteers={runtime.pendingSteers}
        approval={runtime.approval}
        question={runtime.question}
        plan={runtime.plan}
        jobs={runtime.jobs}
        onStop={runtime.stop}
      />}
    </div>
    {runtime.pendingFiles.length > 0 && <div className="attachment-list" aria-label="待发送附件">{runtime.pendingFiles.map((file, index) => <PendingAttachment key={`${file.name}-${file.lastModified}-${index}`} file={file} onRemove={() => runtime.removePendingFile(index)} />)}</div>}
    <ComposerPrimitive.Root className="composer" onSubmit={onComposerSubmit}>
      {showRefreshCommand && <div className="composer-command-menu" id={commandMenuId} role="listbox" aria-label="命令">
        <button type="button" id={`${commandMenuId}-refresh`} role="option" aria-selected="true" disabled={refreshDisabled} onMouseDown={(event) => event.preventDefault()} onClick={runRefreshCommand}>
          <RefreshCw size={16} />
          <span><strong>/refresh</strong><small>{runtime.refreshing ? "正在重新加载…" : runtime.sessionRunning ? "当前任务结束后可用" : "重新加载配置、Skill、插件与 MCP"}</small></span>
          <kbd>Enter</kbd>
        </button>
      </div>}
      <div ref={workspacePickerRef} className="workspace-picker-wrap" onKeyDown={(event) => {
        if (event.key === "Escape" && workspaceEditing) {
          event.preventDefault();
          setWorkspaceEditing(false);
          workspacePickerRef.current?.querySelector("button")?.focus();
        }
      }}>
        <button type="button" className="workspace-picker" onClick={() => setWorkspaceEditing((value) => !value)} disabled={runtime.controlsDisabled || runtime.sessionRunning} aria-expanded={workspaceEditing} aria-haspopup="menu" aria-controls={workspaceEditing ? workspaceMenuId : undefined}>
          <span className="workspace-picker-icon">⌂</span><span className="workspace-picker-value">{runtime.displayedWorkspace || "选择项目"}</span>
          {runtime.workspaceSaving ? <LoaderCircle size={14} className="spin" aria-label="正在切换工作区" /> : <ChevronDown size={14} />}
        </button>
        {workspaceEditing && <div className="workspace-menu" id={workspaceMenuId} role="menu" aria-label="选择工作区" style={workspaceMenuBounds}>
          <div className="workspace-menu-list">
            {workspaceGroups.filter((group) => group.options.length > 0).map((group) => <div className="workspace-menu-group" role="group" aria-label={group.label} key={group.label}>
              <div className="workspace-menu-heading">{group.label}</div>
              {group.options.map(({ workspace }) => <button type="button" role="menuitem" className={`workspace-option ${workspace === runtime.displayedWorkspace ? "selected" : ""}`} key={workspace} onClick={() => { void runtime.saveWorkspace(workspace); setWorkspaceEditing(false); }} disabled={runtime.controlsDisabled || runtime.sessionRunning || runtime.workspaceSaving} title={workspace}>
                <Folder size={16} /><span className="workspace-option-path">{workspace.split(/[\\/]/).pop() || workspace}</span>
                {workspace === runtime.displayedWorkspace && <Check size={14} />}
              </button>)}
            </div>)}
          </div>
          <div className="workspace-menu-divider" />
          <button type="button" role="menuitem" className="workspace-new-option" onClick={() => { setWorkspaceEditing(false); void runtime.chooseNewWorkspace(); }} disabled={runtime.controlsDisabled || runtime.sessionRunning || runtime.workspaceSaving}><Plus size={16} />新建工作区</button>
          <button type="button" role="menuitem" className="workspace-new-option" onClick={() => { setWorkspaceEditing(false); void runtime.chooseScratchWorkspace(); }} disabled={runtime.controlsDisabled || runtime.sessionRunning || runtime.workspaceSaving}><Plus size={16} />临时工作区</button>
        </div>}
      </div>
      <ComposerPrimitive.Input
        placeholder={!runtime.workspaceSelected ? "请先选择工作区…" : runtime.question ? "请先回答上方问题…" : runtime.approval ? "请先处理上方确认…" : "Ask Nosis…"}
        aria-label="消息"
        aria-autocomplete="list"
        aria-expanded={showRefreshCommand}
        aria-controls={showRefreshCommand ? commandMenuId : undefined}
        aria-activedescendant={showRefreshCommand ? `${commandMenuId}-refresh` : undefined}
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
        <button type="button" className="attachment-button" aria-label="添加附件" title="添加附件" disabled={!runtime.workspaceSelected || runtime.controlsDisabled || runtime.sessionRunning || runtime.uploadingAttachments} onClick={() => fileInputRef.current?.click()}><Paperclip size={15} /></button>
        <input ref={fileInputRef} className="attachment-input" type="file" multiple onChange={(event) => {
          runtime.addPendingFiles(Array.from(event.target.files ?? []));
          event.currentTarget.value = "";
        }} />
        <label className="model-selector" title={runtime.providers.find((option) => option.id === runtime.displayedProvider)?.model}>
          <select aria-label="选择模型" value={runtime.displayedProvider} disabled={!runtime.workspaceSelected || runtime.controlsDisabled || runtime.sessionRunning || runtime.providerSaving} onChange={(event) => runtime.changeProvider(event.target.value)}>
            {runtime.providers.map((option) => <option key={option.id} value={option.id}>{option.model}</option>)}
          </select>{runtime.providerSaving ? <LoaderCircle size={12} className="spin" aria-label="正在切换模型" /> : <ChevronDown size={12} />}
        </label>
        <label className="permission-selector" title="权限模式">
          <ShieldCheck size={13} />
          <select aria-label="权限模式" value={runtime.displayedPermissionPreset} disabled={!runtime.workspaceSelected || runtime.controlsDisabled || runtime.permissionSaving} onChange={(event) => runtime.changePermissionPreset(event.target.value as PermissionPreset)}>
            <option value="ask_for_approval">请求批准</option>
            <option value="workspace_access">工作区访问</option>
            <option value="full_access">完全访问</option>
          </select>{runtime.permissionSaving ? <LoaderCircle size={12} className="spin" aria-label="正在保存权限" /> : <ChevronDown size={12} />}
        </label>
        <div className="composer-actions"><ContextWindowIndicator window={runtime.contextWindow} cacheUsage={runtime.cacheUsage} />
          <button type="submit" className="send-button" aria-label={runtime.running ? "发送引导" : "发送消息"} disabled={runtime.composerGate.sendDisabled || (runtime.pendingFiles.length > 0 ? runtime.sessionRunning : !canSend)}><ArrowUp size={19} /></button>
        </div>
      </div>
    </ComposerPrimitive.Root>
    <div className="composer-footer">Nosis · 你的项目搭档</div>
  </div>;
}
