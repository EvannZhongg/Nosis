import { useMemo, useRef } from "react";
import { AssistantRuntimeProvider, ThreadPrimitive } from "@assistant-ui/react";
import type { ContextWindow } from "@nosis/protocol";
import type { ModelOption, Session, WorkspaceOption } from "./api";
import { Composer } from "./chat/Composer";
import { SessionIdContext, UserMessageEditContext } from "./chat/Messages";
import { Transcript } from "./chat/Transcript";
import { useSessionRuntime } from "./chat/useSessionRuntime";

export function Chat({
  session,
  selected,
  searchTarget,
  forkPreview,
  contextWindow,
  workspaceOptions = [],
  backgroundActive = false,
  providers,
  provider,
  onProviderChange,
  onBusyChange,
  onContextWindowChange,
  onSessionAvailable,
  onTitleChange,
  onTurnEnd,
  onWorkspaceChange,
  onEditMessage,
  initialPrompt,
  onInitialPromptConsumed,
}: {
  session: Session;
  selected: boolean;
  searchTarget?: { itemIndex: number; requestId: string };
  forkPreview?: { itemIndex: number; text: string };
  contextWindow: ContextWindow | null;
  workspaceOptions?: WorkspaceOption[];
  backgroundActive?: boolean;
  providers: ModelOption[];
  provider?: string;
  onProviderChange: (provider: string) => void;
  onBusyChange: (busy: boolean) => void;
  onContextWindowChange: (window: ContextWindow | null) => void;
  onSessionAvailable: () => void;
  onTitleChange?: (sessionId: string, title: string) => void;
  onTurnEnd: () => void;
  onWorkspaceChange?: (workspace: string) => void;
  onEditMessage: (anchorId: string, content: string) => Promise<void>;
  initialPrompt?: string;
  onInitialPromptConsumed: () => void;
}) {
  const viewportRef = useRef<HTMLDivElement | null>(null);
  const runtime = useSessionRuntime({
    session,
    selected,
    forkPreview,
    contextWindow,
    workspaceOptions,
    backgroundActive,
    providers,
    provider,
    onProviderChange,
    onBusyChange,
    onContextWindowChange,
    onSessionAvailable,
    onTitleChange,
    onTurnEnd,
    onWorkspaceChange,
    initialPrompt,
    onInitialPromptConsumed,
  });
  const editContext = useMemo(() => ({
    anchors: new Map(
      (session.editable_user_messages ?? []).map((target) => [target.item_index, target.anchor_id]),
    ),
    disabled: runtime.sessionRunning,
    previewing: Boolean(forkPreview),
    submit: onEditMessage,
  }), [forkPreview, onEditMessage, runtime.sessionRunning, session.editable_user_messages]);
  const targetMessage = searchTarget && runtime.messages.find((message) => {
    const indexes = message.metadata?.custom?.itemIndexes;
    return Array.isArray(indexes) && indexes.includes(searchTarget.itemIndex);
  });

  return <SessionIdContext.Provider value={session.session_id}>
    <UserMessageEditContext.Provider value={editContext}>
      <AssistantRuntimeProvider runtime={runtime.assistantRuntime}>
        <ThreadPrimitive.Root className="thread">
          <Transcript previews={runtime.turnPreviews} viewportRef={viewportRef} active={selected || Boolean(forkPreview)} navigation={searchTarget && { requestId: searchTarget.requestId, messageId: targetMessage?.id }} />
          <Composer runtime={runtime} />
        </ThreadPrimitive.Root>
      </AssistantRuntimeProvider>
    </UserMessageEditContext.Provider>
  </SessionIdContext.Provider>;
}
