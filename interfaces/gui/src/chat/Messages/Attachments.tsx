import { createContext, useContext, useEffect, useState, type ComponentPropsWithoutRef } from "react";
import type { FileMessagePartProps, ImageMessagePartProps } from "@assistant-ui/react";
import { FileText, X } from "lucide-react";
import { createSignedImageUrl } from "../../api";

export const SessionIdContext = createContext<string | undefined>(undefined);

export function isRemoteMarkdownImage(src: string | undefined): boolean {
  return Boolean(src && /^https?:\/\//i.test(src));
}

export function FileAttachmentPart({ data, filename, mimeType }: FileMessagePartProps) {
  return <a className="message-file" href={data} download={filename} title={mimeType}>
    <FileText size={18} />
    <span>{filename || "附件"}</span>
  </a>;
}

function WorkspaceImage({ path, sessionId, alt, className, ...props }: {
  path: string;
  sessionId?: string;
} & Omit<ComponentPropsWithoutRef<"img">, "src">) {
  const [url, setUrl] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    setUrl(null);
    setFailed(false);
    void createSignedImageUrl(path, sessionId, controller.signal)
      .then(setUrl)
      .catch((error: unknown) => {
        if (!(error instanceof DOMException && error.name === "AbortError")) setFailed(true);
      });
    return () => controller.abort();
  }, [path, sessionId]);

  if (failed) return <span className="message-image-status">图片无法加载</span>;
  if (!url) return <span className="message-image-status">图片加载中…</span>;
  return <img {...props} className={["message-image", className].filter(Boolean).join(" ")} src={url} alt={alt || "图片"} onError={() => setFailed(true)} />;
}

export function SignedImage({ image, filename, providerMetadata }: ImageMessagePartProps) {
  const metadata = providerMetadata?.nosis;
  const sessionId = metadata && !Array.isArray(metadata)
    && typeof metadata.session_id === "string"
    ? metadata.session_id
    : undefined;
  return <WorkspaceImage path={image} sessionId={sessionId} alt={filename || "图片"} />;
}

export function MarkdownImage({ node: _node, src, alt, ...props }: ComponentPropsWithoutRef<"img"> & { node?: unknown }) {
  const sessionId = useContext(SessionIdContext);
  if (!src) return null;
  if (isRemoteMarkdownImage(src)) return <img {...props} src={src} alt={alt ?? ""} />;
  return <WorkspaceImage {...props} path={src} sessionId={sessionId} alt={alt ?? "图片"} />;
}

export function PendingAttachment({ file, onRemove }: { file: File; onRemove: () => void }) {
  const [url, setUrl] = useState("");
  const image = file.type.startsWith("image/");

  useEffect(() => {
    if (!image) return;
    const next = URL.createObjectURL(file);
    setUrl(next);
    return () => URL.revokeObjectURL(next);
  }, [file, image]);

  return <span className={`attachment-chip ${image ? "image" : "file"}`}>
    {image
      ? <img src={url} alt={file.name} />
      : <><FileText size={20} /><span><strong>{file.name}</strong><small>{formatFileSize(file.size)}</small></span></>}
    <button type="button" aria-label={`移除 ${file.name}`} onClick={onRemove}><X size={12} /></button>
  </span>;
}

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
