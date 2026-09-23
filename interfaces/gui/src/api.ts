import type { ContextWindow, PermissionPreset, PlanSnapshot, SessionItem, SessionSummary, UserAttachment } from "@nosis/protocol";
import type { SettingsSnapshot } from "@nosis/protocol";

export type { SessionItem, SessionSummary };

export type Session = {
  session_id: string;
  items: SessionItem[];
  workspace?: string | null;
  permission_preset: PermissionPreset;
  provider?: string | null;
  context_window?: ContextWindow | null;
  event_sequence?: number;
  plan?: PlanSnapshot | null;
};
export type WorkspaceSessions = { workspace: string; scratch?: boolean; sessions: SessionSummary[] };
export type ScheduleTrigger =
  | { type: "once"; at: string }
  | { type: "interval"; seconds: number; start_at?: string | null }
  | { type: "cron"; expression: string; timezone: string };
export type ScheduleSummary = {
  schedule_id: string;
  prompt: string;
  trigger: ScheduleTrigger;
  workspace: string;
  execution_scope: "workspace" | "host";
  origin_session_id: string;
  schedule_session_id: string;
  session_available: boolean;
  enabled: boolean;
  end_at?: string | null;
  next_run_at?: string | null;
  latest_run?: {
    run_id: string;
    status: string;
    scheduled_for: string;
    started_at?: string | null;
    finished_at?: string | null;
    error?: string | null;
  } | null;
};
export type ModelOption = { id: string; model: string };
export type ModelOptions = { default: string; models: ModelOption[] };
export type MemoryDocument = {
  preferences: string[];
  facts: string[];
  decisions: string[];
};
export type MemorySnapshot = {
  global: MemoryDocument;
  workspaces: { workspace: string; memory: MemoryDocument }[];
};
export type { UserAttachment };
export type Directory = {
  root: string;
  path: string;
  entries: { name: string; type: "directory" | "file" | "symlink" | "other" }[];
  has_more: boolean;
  next_cursor: string | null;
};

export async function get<T>(path: string): Promise<T> {
  const response = await fetch(path);
  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail ?? `请求失败 (${response.status})`);
  }
  return response.json();
}

export async function put<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(path, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail ?? `保存失败 (${response.status})`);
  }
  return response.json();
}

export type { SettingsSnapshot };

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

export async function releaseActiveSession(
  sessionId: string,
  provider?: string,
  attachmentId?: string,
): Promise<void> {
  const query = new URLSearchParams();
  if (provider) query.set("provider", provider);
  if (attachmentId) query.set("attachment_id", attachmentId);
  const suffix = query.size ? `?${query}` : "";
  const response = await fetch(`/api/active-sessions/${encodeURIComponent(sessionId)}${suffix}`, {
    method: "DELETE",
    keepalive: true,
  });
  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail ?? `会话连接释放失败 (${response.status})`);
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

export async function createScratchWorkspace(sessionId: string): Promise<string> {
  const response = await fetch("/api/workspaces/scratch", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId }),
  });
  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail ?? `临时工作区创建失败 (${response.status})`);
  }
  return (await response.json() as { workspace: string }).workspace;
}

export async function uploadAttachments(files: File[], sessionId?: string): Promise<UserAttachment[]> {
  const body = new FormData();
  for (const file of files) body.append("files", file, file.name);
  const query = sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : "";
  const response = await fetch(`/api/attachments${query}`, { method: "POST", body });
  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail ?? `上传失败 (${response.status})`);
  }
  const data = await response.json() as { attachments: UserAttachment[] };
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

export function attachmentDownloadUrl(path: string, filename: string, sessionId?: string): string {
  const query = new URLSearchParams();
  if (sessionId) query.set("session_id", sessionId);
  query.set("download_name", filename);
  const prefix = ".nosis/attachments/";
  if (!path.startsWith(prefix)) return path;
  return `/api/attachments/${encodeURIComponent(path.slice(prefix.length))}?${query}`;
}
