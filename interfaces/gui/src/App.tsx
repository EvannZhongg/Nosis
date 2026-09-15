import { useCallback, useEffect, useState } from "react";
import { ChevronDown, ChevronRight, MessageSquare, Plus, Trash2 } from "lucide-react";
import { Chat } from "./Chat";
import { Workspace } from "./Workspace";
import type { Usage } from "@nosis/protocol";
import { deleteSession, get, sessionUrl, type ModelOption, type ModelOptions, type Session, type WorkspaceSessions } from "./api";
import { addSessionSummary } from "./sessions";

export function App() {
  const [sessionGroups, setSessionGroups] = useState<WorkspaceSessions[]>([]);
  const [session, setSession] = useState<Session>(() => ({ session_id: crypto.randomUUID(), items: [] }));
  const [chatSessions, setChatSessions] = useState<Session[]>([]);
  const [busyBySession, setBusyBySession] = useState<Record<string, boolean>>({});
  const [usage, setUsage] = useState<Usage | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [workspaceVersion, setWorkspaceVersion] = useState(0);
  const [models, setModels] = useState<ModelOption[]>([]);
  const [model, setModel] = useState("");
  const [collapsedWorkspaces, setCollapsedWorkspaces] = useState<Set<string>>(new Set());

  const refreshSessions = useCallback(async () => {
    try {
      setSessionGroups(await get<WorkspaceSessions[]>("/api/sessions"));
    } catch (error) {
      setError(String(error));
    }
  }, []);

  useEffect(() => { void refreshSessions(); }, [refreshSessions]);
  useEffect(() => {
    get<{ root: string }>("/api/workspace").then(({ root }) => {
      setSession((current) => current.workspace ? current : { ...current, workspace: root });
    }).catch(() => undefined);
  }, []);
  // Usage belongs to the shown conversation, so switching sessions clears it.
  useEffect(() => { setUsage(null); }, [session.session_id]);
  useEffect(() => {
    get<ModelOptions>("/api/models").then((options) => {
      setModels(options.models);
      setModel(options.default);
    }).catch((error) => setError(String(error)));
  }, []);

  async function selectSession(id: string) {
    setLoading(true);
    setError("");
    try {
      const loaded = await get<Session>(sessionUrl(id));
      setSession(loaded);
      setChatSessions((all) => all.some((item) => item.session_id === id) ? all.map((item) => item.session_id === id ? loaded : item) : [...all, loaded]);
    } catch (error) {
      setError(String(error));
    } finally {
      setLoading(false);
    }
  }

  async function removeSession(id: string) {
    if (busyBySession[id] || !window.confirm("确定删除这个会话吗？此操作无法撤销。")) return;
    setLoading(true);
    setError("");
    try {
      await deleteSession(id);
      setChatSessions((all) => all.filter((item) => item.session_id !== id));
      if (session.session_id === id) {
        const next = sessions.find((item) => item.session_id !== id);
        if (next) await selectSession(next.session_id);
        else setSession({ session_id: crypto.randomUUID(), items: [], workspace: session.workspace });
      }
      await refreshSessions();
    } catch (error) {
      setError(String(error));
    } finally {
      setLoading(false);
    }
  }

  const sessions = sessionGroups.flatMap((group) => group.sessions);
  const selectedTitle = sessions.find((item) => item.session_id === session.session_id)?.title ?? "New chat";
  const busy = Boolean(busyBySession[session.session_id]);

  return (
    <div className="app-shell">
      <aside className="sessions-panel" aria-label="Sessions">
        <div className="brand"><span>Nosis<span className="brand-dot">.</span></span></div>
        <button className="new-chat" disabled={loading} onClick={() => {
          const fresh = { session_id: crypto.randomUUID(), items: [], workspace: session.workspace };
          setSession(fresh);
          setChatSessions((all) => [...all, fresh]);
          setError("");
        }}><Plus size={17} /> New chat</button>
        <div className="section-label">Projects <span>{sessionGroups.length}</span></div>
        <nav className="session-list">
          {sessionGroups.map((group) => {
            const collapsed = collapsedWorkspaces.has(group.workspace);
            return <section className="workspace-group" key={group.workspace}>
              <button className="workspace-group-title" title={group.workspace} aria-expanded={!collapsed} onClick={() => setCollapsedWorkspaces((previous) => {
                const next = new Set(previous);
                if (collapsed) next.delete(group.workspace); else next.add(group.workspace);
                return next;
              })}><span className="workspace-chevron">{collapsed ? <ChevronRight size={13} /> : <ChevronDown size={13} />}</span><span className="workspace-group-name">{group.workspace.split(/[\\/]/).pop() || group.workspace}</span><small>{group.workspace}</small><span className="workspace-session-count">{group.sessions.length}</span></button>
              {!collapsed && group.sessions.map((item) => <div className={`session-row ${item.session_id === session.session_id ? "selected" : ""}`} key={item.session_id}><button className="session-button" title={item.title} disabled={loading} onClick={() => void selectSession(item.session_id)}><MessageSquare size={15} /><span>{item.title}</span></button><button className="delete-session" aria-label={`删除会话 ${item.title}`} title="删除会话" disabled={loading || Boolean(busyBySession[item.session_id])} onClick={(event) => { event.stopPropagation(); void removeSession(item.session_id); }}><Trash2 size={14} /></button></div>)}
            </section>;
          })}
          {sessionGroups.length === 0 && <p className="session-empty">从一段对话开始。</p>}
        </nav>
        <div className="sidebar-footer"><span className="status-dot" /> Personal workspace</div>
      </aside>

      <main className="chat-panel">
        <header className="chat-header"><div className="breadcrumb">Chat <ChevronRight size={14} /><span>{selectedTitle}</span></div><span className="status-label"><span className={`status-dot ${busy ? "working" : ""}`} />{busy ? "Working" : "Ready"}{usage?.total_tokens ? ` · ${usage.total_tokens} tokens` : ""}</span></header>
        {error && <div className="error-banner" role="alert">{error}</div>}
        {/** Keep every mounted chat/socket alive so switching sessions does not cancel work. */}
        {(chatSessions.length ? chatSessions : [session]).map((current) => <div key={current.session_id} style={{ display: current.session_id === session.session_id ? "contents" : "none" }}><Chat session={current} workspaceOptions={sessionGroups.map((group) => group.workspace)} disabled={loading || !model}
          models={models} model={model} onModelChange={setModel} onBusyChange={(value) => setBusyBySession((all) => ({ ...all, [current.session_id]: value }))} onUsageChange={(value) => { if (current.session_id === session.session_id) setUsage(value); }} onTurnEnd={() => {
          void refreshSessions();
          setWorkspaceVersion((value) => value + 1);
        }} onSessionStart={(title) => {
          if (!current.workspace) return;
          setSessionGroups((groups) => addSessionSummary(groups, current.workspace!, {
            session_id: current.session_id,
            title,
          }));
        }} onWorkspaceChange={(workspace) => {
          setChatSessions((all) => all.map((item) => item.session_id === current.session_id ? { ...item, workspace } : item));
          if (current.session_id === session.session_id) setSession((item) => ({ ...item, workspace }));
          setWorkspaceVersion((value) => value + 1);
        }} /></div>)}
      </main>
      <Workspace version={workspaceVersion} sessionId={session.session_id} />
    </div>
  );
}
