import type { SessionSummary, WorkspaceSessions } from "./api";

/** Add a newly-started session to the stored-session projection. */
export function addSessionSummary(
  groups: WorkspaceSessions[],
  workspace: string,
  session: SessionSummary,
): WorkspaceSessions[] {
  if (groups.some((group) => group.sessions.some((item) => item.session_id === session.session_id))) {
    return groups;
  }

  const group = groups.find((item) => item.workspace === workspace);
  if (!group) return [{ workspace, sessions: [session] }, ...groups];

  return [
    { ...group, sessions: [session, ...group.sessions] },
    ...groups.filter((item) => item !== group),
  ];
}

