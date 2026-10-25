import { useEffect, useRef, useState, type RefObject } from "react";
import { ThreadPrimitive, useAuiState } from "@assistant-ui/react";
import { Terminal } from "lucide-react";
import { AssistantMessage, UserMessage } from "./Messages";
import { TurnNavigation } from "./TurnNavigation";

export function Transcript({ previews, viewportRef, active, navigation }: {
  previews: string[];
  viewportRef: RefObject<HTMLDivElement | null>;
  active: boolean;
  navigation?: { requestId: string; messageId?: string };
}) {
  const [completedNavigation, setCompletedNavigation] = useState<string | null>(null);
  const [readingSearch, setReadingSearch] = useState(Boolean(navigation));
  const initializeAtBottom = useRef(!navigation);
  const running = useAuiState((state) => state.thread.isRunning);
  const wasRunning = useRef(running);
  const pendingNavigation = Boolean(navigation && navigation.requestId !== completedNavigation);
  const requestId = navigation?.requestId;
  const messageId = navigation?.messageId;

  useEffect(() => {
    if (!active || !pendingNavigation || !messageId || !requestId) return;
    const viewport = viewportRef.current;
    if (!viewport) return;
    let frame: number | null = null;
    const navigate = () => {
      const message = viewport.querySelector<HTMLElement>(`[data-message-id="${messageId}"]`);
      if (!message || frame !== null) return;
      frame = requestAnimationFrame(() => {
        observer.disconnect();
        message.scrollIntoView({ block: "center" });
        message.classList.remove("search-message-highlight");
        // Restart the highlight when the same result is selected again.
        void message.offsetWidth;
        message.classList.add("search-message-highlight");
        setReadingSearch(true);
        setCompletedNavigation(requestId);
      });
    };
    // assistant-ui commits external-store messages after the parent render.
    const observer = new MutationObserver(navigate);
    observer.observe(viewport, { childList: true, subtree: true });
    navigate();
    return () => { observer.disconnect(); if (frame !== null) cancelAnimationFrame(frame); };
  }, [active, messageId, pendingNavigation, requestId, viewportRef]);

  useEffect(() => {
    if (!navigation || (running && !wasRunning.current)) setReadingSearch(false);
    wasRunning.current = running;
  }, [navigation, running]);

  return <div className="thread-scroll-area">
    <ThreadPrimitive.Viewport ref={viewportRef} className="thread-viewport" autoScroll={!pendingNavigation && !readingSearch} scrollToBottomOnInitialize={initializeAtBottom.current} scrollToBottomOnThreadSwitch={!pendingNavigation} onScroll={(event) => {
      const viewport = event.currentTarget;
      if (readingSearch && viewport.scrollTop + viewport.clientHeight >= viewport.scrollHeight - 2) setReadingSearch(false);
    }}>
      <ThreadPrimitive.Empty>
        <div className="welcome"><div className="welcome-symbol"><Terminal size={26} /></div><div className="eyebrow">YOUR PERSONAL AGENT</div><h1>一起，把想法变成现实。</h1><p>聊聊你的项目，或者交给 Nosis 一个任务。</p></div>
      </ThreadPrimitive.Empty>
      <div className="messages"><ThreadPrimitive.Messages components={{ UserMessage, AssistantMessage }} /></div>
    </ThreadPrimitive.Viewport>
    <TurnNavigation previews={previews} viewportRef={viewportRef} active={active} />
  </div>;
}
