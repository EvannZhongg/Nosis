import { useEffect, useRef, useState } from "react";
import { Check, Copy } from "lucide-react";

export function CopyTextButton({ getText, label, className = "" }: {
  getText: () => string;
  label: string;
  className?: string;
}) {
  const [copied, setCopied] = useState(false);
  const resetRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => () => {
    if (resetRef.current) clearTimeout(resetRef.current);
  }, []);

  const copy = () => {
    const text = getText();
    if (!text || !navigator.clipboard) return;
    void navigator.clipboard.writeText(text).then(() => {
      setCopied(true);
      if (resetRef.current) clearTimeout(resetRef.current);
      resetRef.current = setTimeout(() => {
        resetRef.current = null;
        setCopied(false);
      }, 2000);
    }, () => undefined);
  };

  const title = copied ? "已复制" : label;
  return <button type="button" className={`copy-button ${className}`.trim()} aria-label={title} title={title} data-copied={copied} onClick={copy}>
    {copied ? <Check size={13} /> : <Copy size={13} />}
  </button>;
}
