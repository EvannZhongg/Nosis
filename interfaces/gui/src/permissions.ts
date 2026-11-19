import type { PermissionPreset } from "@nosis/protocol";

export const PERMISSION_LABELS: Record<PermissionPreset, string> = {
  ask_for_approval: "请求批准",
  workspace_access: "工作区访问",
  full_access: "完全访问",
};

export const PERMISSION_OPTIONS = (Object.keys(PERMISSION_LABELS) as PermissionPreset[])
  .map((value) => ({ value, label: PERMISSION_LABELS[value] }));
