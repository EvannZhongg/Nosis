import type { ToolCall } from "@nosis/protocol";

export type SessionItem = {
  role: "user" | "assistant" | "tool";
  // Text or structured content parts for multimodal turns.
  content: any;
  timestamp_utc?: string;
  tool_calls?: ToolCall[];
  tool_call_id?: string;
  reasoning?: string | null;
};

export type Session = { session_id: string; items: SessionItem[]; workspace?: string | null };
export type SessionSummary = { session_id: string; title: string };
export type ModelOption = { id: string; model: string };
export type ModelOptions = { default: string; models: ModelOption[] };
export type ImageAttachment = { type: "image"; path: string; mime_type: string };
export type Directory = {
  root: string;
  path: string;
  entries: { name: string; type: "directory" | "file" | "symlink" | "other" }[];
};

export async function get<T>(path: string): Promise<T> {
  const response = await fetch(path);
  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail ?? `请求失败 (${response.status})`);
  }
  return response.json();
}

export function sessionUrl(sessionId: string): string {
  return `/api/sessions/${encodeURIComponent(sessionId)}`;
}

export async function uploadAttachments(files: File[], sessionId?: string): Promise<ImageAttachment[]> {
  const body = new FormData();
  for (const file of files) body.append("files", file, file.name);
  const query = sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : "";
  const response = await fetch(`/api/attachments${query}`, { method: "POST", body });
  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail ?? `上传失败 (${response.status})`);
  }
  const data = await response.json() as { attachments: ImageAttachment[] };
  return data.attachments;
}

export function attachmentUrl(path: string, sessionId?: string): string {
  const prefix = ".nosis/attachments/";
  return path.startsWith(prefix)
    ? `/api/attachments/${encodeURIComponent(path.slice(prefix.length))}${sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : ""}`
    : path;
}
