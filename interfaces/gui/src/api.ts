import type { ToolCall } from "@nosis/protocol";

export type SessionItem = {
  role: "user" | "assistant" | "tool";
  // Text or structured content parts for multimodal turns.
  content: any;
  timestamp_utc?: string;
  tool_calls?: ToolCall[];
  tool_call_id?: string;
  reasoning?: string | null;
  // "tool_media" marks a user-role item the runtime synthesized to carry
  // images a tool loaded. It is not something the person said, so the
  // transcript renders it as part of the assistant's work.
  origin?: "conversation" | "tool_media";
};

export type Session = { session_id: string; items: SessionItem[]; workspace?: string | null };
export type SessionSummary = { session_id: string; title: string };
export type WorkspaceSessions = { workspace: string; sessions: SessionSummary[] };
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

export async function deleteSession(sessionId: string): Promise<void> {
  const response = await fetch(sessionUrl(sessionId), { method: "DELETE" });
  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail ?? `会话删除失败 (${response.status})`);
  }
}

export async function selectWorkspace(): Promise<string | null> {
  const response = await fetch("/api/select-workspace");
  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail ?? `选择工作区失败 (${response.status})`);
  }
  return (await response.json() as { workspace: string | null }).workspace;
}

export async function updateSessionWorkspace(sessionId: string, workspace: string): Promise<string> {
  const response = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/workspace`, {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ workspace }),
  });
  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail ?? `工作区更新失败 (${response.status})`);
  }
  return (await response.json() as { workspace: string }).workspace;
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
  const query = sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : "";
  const prefix = ".nosis/attachments/";
  if (path.startsWith(prefix)) {
    return `/api/attachments/${encodeURIComponent(path.slice(prefix.length))}${query}`;
  }
  // An image the agent read itself can live anywhere in the workspace,
  // so it is fetched by path. An absolute path belongs to a session
  // artifact outside the workspace and has no route.
  if (path && !path.startsWith("/") && !/^[a-zA-Z]:[\\/]/.test(path)) {
    const separator = query ? "&" : "?";
    return `/api/workspace-image${query}${separator}path=${encodeURIComponent(path)}`;
  }
  return path;
}
