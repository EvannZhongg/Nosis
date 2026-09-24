import { useRef } from "react";
import { AssistantRuntimeProvider, ThreadPrimitive } from "@assistant-ui/react";
import type { ContextWindow } from "@nosis/protocol";
import type { ModelOption, Session } from "./api";
import { Composer } from "./chat/Composer";
import { SessionIdContext } from "./chat/Messages";
import { Transcript } from "./chat/Transcript";
import { useSessionRuntime } from "./chat/useSessionRuntime";

export function Chat({
  session,
  selected,
  contextWindow,
  workspaceOptions = [],
  backgroundActive = false,
  providers,
  provider,
  onProviderChange,
  onBusyChange,
  onContextWindowChange,
  onSessionAvailable,
  onTurnEnd,
  onWorkspaceChange,
}: {
  session: Session;
  selected: boolean;
  contextWindow: ContextWindow | null;
  workspaceOptions?: string[];
  backgroundActive?: boolean;
  providers: ModelOption[];
  provider: string;
  onProviderChange: (provider: string) => void;
  onBusyChange: (busy: boolean) => void;
  onContextWindowChange: (window: ContextWindow | null) => void;
  onSessionAvailable: () => void;
  onTurnEnd: () => void;
  onWorkspaceChange?: (workspace: string) => void;
}) {
  const viewportRef = useRef<HTMLDivElement | null>(null);
  const runtime = useSessionRuntime({
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
  });

  return <SessionIdContext.Provider value={session.session_id}>
    <AssistantRuntimeProvider runtime={runtime.assistantRuntime}>
      <ThreadPrimitive.Root className="thread">
        <Transcript previews={runtime.turnPreviews} viewportRef={viewportRef} active={selected} />
        <Composer runtime={runtime} />
      </ThreadPrimitive.Root>
    </AssistantRuntimeProvider>
  </SessionIdContext.Provider>;
}
