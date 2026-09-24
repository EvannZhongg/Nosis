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

export function shouldSubmitAttachmentOnly(
  text: string,
  pendingFileCount: number,
): boolean {
  return !text.trim() && pendingFileCount > 0;
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
