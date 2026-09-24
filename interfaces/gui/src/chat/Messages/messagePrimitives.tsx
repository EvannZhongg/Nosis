import { useRef, type ComponentPropsWithoutRef } from "react";
import { useAuiState } from "@assistant-ui/react";
import { MarkdownTextPrimitive } from "@assistant-ui/react-markdown";
import remarkGfm from "remark-gfm";
import { CopyTextButton } from "../../CopyTextButton";
import { MarkdownImage } from "./Attachments";

const MARKDOWN_PLUGINS = [remarkGfm];

function CopyablePre({ node: _node, children, ...props }: ComponentPropsWithoutRef<"pre"> & { node?: unknown }) {
  const contentRef = useRef<HTMLPreElement | null>(null);
  const running = useAuiState((state) => state.message.status?.type === "running");
  return <div className="markdown-copy-block markdown-code-block">
    {!running && <CopyTextButton className="markdown-copy-button" label="复制代码" getText={() => contentRef.current?.innerText ?? ""} />}
    <pre {...props} ref={contentRef}>{children}</pre>
  </div>;
}

function CopyableTable({ node: _node, children, ...props }: ComponentPropsWithoutRef<"table"> & { node?: unknown }) {
  const tableRef = useRef<HTMLTableElement | null>(null);
  const running = useAuiState((state) => state.message.status?.type === "running");
  const getText = () => Array.from(tableRef.current?.rows ?? [])
    .map((row) => Array.from(row.cells).map((cell) => cell.innerText).join("\t"))
    .join("\n");
  return <div className="markdown-copy-block markdown-table-block">
    {!running && <CopyTextButton className="markdown-copy-button" label="复制表格" getText={getText} />}
    <table {...props} ref={tableRef}>{children}</table>
  </div>;
}

const MARKDOWN_COMPONENTS = { img: MarkdownImage, pre: CopyablePre, table: CopyableTable };

export function MessageTimestamp({ className = "" }: { className?: string } = {}) {
  const createdAt = useAuiState((state) => state.message.createdAt);
  if (!createdAt) return null;
  return <time className={`message-timestamp ${className}`.trim()} dateTime={createdAt.toISOString()}>{createdAt.toLocaleString()}</time>;
}

export function MarkdownText() {
  return <MarkdownTextPrimitive remarkPlugins={MARKDOWN_PLUGINS} components={MARKDOWN_COMPONENTS} />;
}
