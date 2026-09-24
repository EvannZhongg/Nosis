import { useEffect, useRef, useState, type RefObject } from "react";

export function TurnNavigation({ previews, viewportRef, active }: {
  previews: string[];
  viewportRef: RefObject<HTMLDivElement | null>;
  active: boolean;
}) {
  const [activeTurn, setActiveTurn] = useState(Math.max(0, previews.length - 1));
  const [preview, setPreview] = useState<{ index: number; top: number } | null>(null);
  const navigationRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport || !active || previews.length === 0) return;

    let frame: number | null = null;
    const updateActiveTurn = () => {
      frame = null;
      const turns = Array.from(viewport.querySelectorAll<HTMLElement>(".user-turn"));
      if (turns.length === 0) return;
      if (viewport.scrollTop + viewport.clientHeight >= viewport.scrollHeight - 2) {
        setActiveTurn(turns.length - 1);
        return;
      }
      const marker = viewport.getBoundingClientRect().top + Math.min(viewport.clientHeight * 0.3, 180);
      let next = 0;
      turns.forEach((turn, index) => {
        if (turn.getBoundingClientRect().top <= marker) next = index;
      });
      setActiveTurn(next);
    };
    const scheduleUpdate = () => {
      if (frame === null) frame = requestAnimationFrame(updateActiveTurn);
    };

    updateActiveTurn();
    viewport.addEventListener("scroll", scheduleUpdate, { passive: true });
    window.addEventListener("resize", scheduleUpdate);
    return () => {
      viewport.removeEventListener("scroll", scheduleUpdate);
      window.removeEventListener("resize", scheduleUpdate);
      if (frame !== null) cancelAnimationFrame(frame);
    };
  }, [active, previews.length, viewportRef]);

  if (previews.length === 0) return null;
  const jumpToTurn = (index: number) => {
    const turn = viewportRef.current?.querySelectorAll<HTMLElement>(".user-turn")[index];
    if (!turn) return;
    setActiveTurn(index);
    turn.scrollIntoView({ block: "start" });
  };
  const showPreview = (index: number, mark: HTMLElement) => {
    const navigation = navigationRef.current;
    if (!navigation) return;
    const navigationRect = navigation.getBoundingClientRect();
    const markRect = mark.getBoundingClientRect();
    setPreview({ index, top: markRect.top - navigationRect.top + markRect.height / 2 });
  };

  return <nav ref={navigationRef} className="turn-navigation" aria-label="对话轮次导航" onMouseLeave={() => setPreview(null)}>
    <div className="turn-navigation-list">
      {previews.map((text, index) => <button
        type="button"
        className={`turn-navigation-mark ${index === activeTurn ? "active" : ""}`}
        aria-label={`跳转到第 ${index + 1} 轮对话：${text}`}
        aria-current={index === activeTurn ? "true" : undefined}
        key={index}
        onClick={() => jumpToTurn(index)}
        onMouseEnter={(event) => showPreview(index, event.currentTarget)}
        onFocus={(event) => showPreview(index, event.currentTarget)}
        onBlur={() => setPreview(null)}
      />)}
    </div>
    {preview && <div className="turn-navigation-preview" style={{ top: preview.top }} role="tooltip">
      <span>第 {preview.index + 1} 轮</span>{previews[preview.index]}
    </div>}
  </nav>;
}
