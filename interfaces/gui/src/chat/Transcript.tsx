import { useCallback, useEffect, useRef, useState, type RefObject } from "react";
import { ThreadPrimitive, useAuiEvent } from "@assistant-ui/react";
import { Terminal } from "lucide-react";
import { AssistantMessage, UserMessage } from "./Messages";
import { ScrollToTurnContext, TurnNavigation } from "./TurnNavigation";

export function Transcript({ previews, viewportRef, active }: {
  previews: string[];
  viewportRef: RefObject<HTMLDivElement | null>;
  active: boolean;
}) {
  const [autoScroll, setAutoScroll] = useState(true);
  const scrollFrame = useRef<number | null>(null);
  const scrollToTurn = useCallback((turn: HTMLElement) => {
    setAutoScroll(false);
    if (scrollFrame.current !== null) cancelAnimationFrame(scrollFrame.current);
    // Wait for folding and the viewport's content-size update before navigating.
    scrollFrame.current = requestAnimationFrame(() => {
      scrollFrame.current = null;
      turn.scrollIntoView({ block: "start" });
    });
  }, []);
  useAuiEvent("thread.runStart", () => setAutoScroll(true));
  useEffect(() => () => {
    if (scrollFrame.current !== null) cancelAnimationFrame(scrollFrame.current);
  }, []);

  return <ScrollToTurnContext.Provider value={scrollToTurn}><div className="thread-scroll-area">
    <ThreadPrimitive.Viewport ref={viewportRef} className="thread-viewport" autoScroll={autoScroll}>
      <ThreadPrimitive.Empty>
        <div className="welcome"><div className="welcome-symbol"><Terminal size={26} /></div><div className="eyebrow">YOUR PERSONAL AGENT</div><h1>一起，把想法变成现实。</h1><p>聊聊你的项目，或者交给 Nosis 一个任务。</p></div>
      </ThreadPrimitive.Empty>
      <div className="messages"><ThreadPrimitive.Messages components={{ UserMessage, AssistantMessage }} /></div>
    </ThreadPrimitive.Viewport>
    <TurnNavigation previews={previews} viewportRef={viewportRef} active={active} />
  </div></ScrollToTurnContext.Provider>;
}
