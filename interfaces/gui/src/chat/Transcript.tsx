import type { RefObject } from "react";
import { ThreadPrimitive } from "@assistant-ui/react";
import { Terminal } from "lucide-react";
import { AssistantMessage, UserMessage } from "./Messages";
import { TurnNavigation } from "./TurnNavigation";

export function Transcript({ previews, viewportRef, active }: {
  previews: string[];
  viewportRef: RefObject<HTMLDivElement | null>;
  active: boolean;
}) {
  return <div className="thread-scroll-area">
    <ThreadPrimitive.Viewport ref={viewportRef} className="thread-viewport">
      <ThreadPrimitive.Empty>
        <div className="welcome"><div className="welcome-symbol"><Terminal size={26} /></div><div className="eyebrow">YOUR PERSONAL AGENT</div><h1>一起，把想法变成现实。</h1><p>聊聊你的项目，或者交给 Nosis 一个任务。</p></div>
      </ThreadPrimitive.Empty>
      <div className="messages"><ThreadPrimitive.Messages components={{ UserMessage, AssistantMessage }} /></div>
    </ThreadPrimitive.Viewport>
    <TurnNavigation previews={previews} viewportRef={viewportRef} active={active} />
  </div>;
}
