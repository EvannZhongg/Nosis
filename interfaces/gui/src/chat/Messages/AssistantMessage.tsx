import { useCallback, useEffect, useMemo, useRef, useState, type PropsWithChildren } from "react";
import { ActionBarPrimitive, MessagePrimitive, useAuiState, type PartState, type ReasoningMessagePartProps } from "@assistant-ui/react";
import { Check, ChevronRight, Copy } from "lucide-react";
import { TURN_PROCESS_GROUP, turnProcessPartIndexes } from "../../transcript";
import { SignedImage } from "./Attachments";
import { MarkdownText, MessageTimestamp } from "./messagePrimitives";
import { ToolCard } from "./ToolCard";

/** Model reasoning, shown the way the TUI shows it: dim and italic. */
function ReasoningText({ text }: ReasoningMessagePartProps) {
  return <div className="reasoning-text">{text}</div>;
}

function TurnPartGroup({ children }: PropsWithChildren) {
  const active = useAuiState((state) => state.thread.isRunning && state.message.isLast);
  const [expanded, setExpanded] = useState(active);
  const wasActiveRef = useRef(active);

  useEffect(() => {
    if (active) setExpanded(true);
    else if (wasActiveRef.current) setExpanded(false);
    wasActiveRef.current = active;
  }, [active]);

  return <details className="turn-process" open={active || expanded} onToggle={(event) => {
    if (!active) setExpanded(event.currentTarget.open);
  }}>
    <summary onClick={(event) => { if (active) event.preventDefault(); }}><ChevronRight size={14} className="turn-process-chevron" />推理与执行过程</summary>
    <div className="turn-process-content">{children}</div>
  </details>;
}

function AssistantParts() {
  const parts = useAuiState((state) => state.message.parts);
  const processParts = useMemo(() => new Set(
    turnProcessPartIndexes(parts).map((index) => parts[index]),
  ), [parts]);
  const groupBy = useCallback((part: PartState) => (
    processParts.has(part) ? [TURN_PROCESS_GROUP] as const : []
  ), [processParts]);

  return <MessagePrimitive.GroupedParts groupBy={groupBy} indicator="never">
    {({ part, children }) => {
      switch (part.type) {
        case TURN_PROCESS_GROUP:
          return <TurnPartGroup>{children}</TurnPartGroup>;
        case "text":
          return <MarkdownText />;
        case "reasoning":
          return <ReasoningText {...part} />;
        case "image":
          return <SignedImage {...part} />;
        case "tool-call":
          return part.toolUI ?? <ToolCard {...part} />;
        default:
          return null;
      }
    }}
  </MessagePrimitive.GroupedParts>;
}

export function AssistantMessage() {
  const running = useAuiState((state) => state.message.status?.type === "running");
  const hasText = useAuiState((state) => state.message.parts.some((part) => part.type === "text" && part.text.length > 0));
  const copied = useAuiState((state) => state.message.isCopied);

  return <MessagePrimitive.Root className="assistant-message">
    <div className="assistant-label"><span className="assistant-avatar"><img src="/nosis-avatar-128.png" alt="" /></span>Nosis</div>
    <div className="assistant-content"><AssistantParts /><div className="assistant-message-meta"><MessageTimestamp />{!running && hasText && <ActionBarPrimitive.Copy className="copy-button message-copy-button" aria-label={copied ? "已复制完整回答" : "复制完整回答"} title={copied ? "已复制" : "复制完整回答"} copiedDuration={2000}>{copied ? <Check size={13} /> : <Copy size={13} />}</ActionBarPrimitive.Copy>}</div></div>
  </MessagePrimitive.Root>;
}
