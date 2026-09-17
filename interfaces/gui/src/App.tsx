import { useCallback, useEffect, useMemo, useState } from "react";
import { ChevronDown, ChevronRight, MessageSquare, Plus, Trash2 } from "lucide-react";
import { Chat } from "./Chat";
import { Workspace } from "./Workspace";
import type { ContextWindow } from "@nosis/protocol";
import { deleteSession, get, sessionUrl, type ModelOption, type ModelOptions, type Session, type WorkspaceSessions } from "./api";

type ActiveRuntime = Session & { provider: string | null; running: boolean };

function newSession(workspace?: string | null): Session {
  return {
    session_id: crypto.randomUUID(),
    items: [],
    workspace,
    permission_preset: "ask_for_approval",
  };
}

export function App() {
  const [initialSession] = useState<Session>(() => newSession());
  const [sessionGroups, setSessionGroups] = useState<WorkspaceSessions[]>([]);
  const [chatSessions, setChatSessions] = useState<Session[]>([initialSession]);
  const [selectedSessionId, setSelectedSessionId] = useState(initialSession.session_id);
  const [busyBySession, setBusyBySession] = useState<Record<string, boolean>>({});
  const [contextBySession, setContextBySession] = useState<Record<string, ContextWindow | null>>({});
  const [loadingSessionId, setLoadingSessionId] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [workspaceVersion, setWorkspaceVersion] = useState(0);
  const [models, setModels] = useState<ModelOption[]>([]);
  const [defaultModel, setDefaultModel] = useState("");
  const [modelBySession, setModelBySession] = useState<Record<string, string>>({});
  const [activeRuntimeIds, setActiveRuntimeIds] = useState<Set<string>>(new Set());
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
      setChatSessions((all) => all.map((item) => item.session_id === initialSession.session_id && !item.workspace ? { ...item, workspace: root } : item));
    }).catch(() => undefined);
  }, [initialSession.session_id]);
  useEffect(() => {
    get<ModelOptions>("/api/models").then((options) => {
      setModels(options.models);
      setDefaultModel(options.default);
    }).catch((error) => setError(String(error)));
  }, []);
  useEffect(() => {
    get<ActiveRuntime[]>("/api/runtimes").then(async (runtimes) => {
      setActiveRuntimeIds(new Set(runtimes.map((runtime) => runtime.session_id)));
      setBusyBySession((all) => ({
        ...all,
        ...Object.fromEntries(runtimes.map((runtime) => [runtime.session_id, runtime.running])),
      }));
      setModelBySession((all) => ({
        ...all,
        ...Object.fromEntries(runtimes.flatMap((runtime) => runtime.provider ? [[runtime.session_id, runtime.provider]] : [])),
      }));
      setContextBySession((all) => ({
        ...all,
        ...Object.fromEntries(runtimes.map((runtime) => [runtime.session_id, runtime.context_window ?? null])),
      }));
      setChatSessions((all) => [
        ...all,
        ...runtimes.filter((active) => !all.some((item) => item.session_id === active.session_id)),
      ]);
    }).catch(() => undefined);
  }, []);

  const selectedSession = useMemo(
    () => chatSessions.find((item) => item.session_id === selectedSessionId) ?? chatSessions[0],
    [chatSessions, selectedSessionId],
  );

  async function selectSession(id: string) {
    if (chatSessions.some((item) => item.session_id === id)) {
      setSelectedSessionId(id);
      setError("");
      return;
    }
    setLoadingSessionId(id);
    setError("");
    try {
      const loaded = await get<Session>(sessionUrl(id));
      setChatSessions((all) => all.some((item) => item.session_id === id) ? all : [...all, loaded]);
      if (loaded.context_window) {
        setContextBySession((all) => ({ ...all, [id]: loaded.context_window ?? null }));
      }
      setSelectedSessionId(id);
    } catch (error) {
      setError(String(error));
    } finally {
      setLoadingSessionId(null);
    }
  }

  function createNewChat() {
    const fresh = newSession(selectedSession?.workspace);
    // Add the new chat to the stable mounted set before making it visible.
    setChatSessions((all) => [...all, fresh]);
    setSelectedSessionId(fresh.session_id);
    setError("");
  }

  async function removeSession(id: string) {
    if (busyBySession[id] || !window.confirm("确定删除这个会话吗？此操作无法撤销。")) return;
    setLoadingSessionId(id);
    setError("");
    try {
      await deleteSession(id);
      const remaining = chatSessions.filter((item) => item.session_id !== id);
      if (selectedSessionId === id) {
        const next = remaining[0] ?? newSession(selectedSession?.workspace);
        setChatSessions(remaining.length ? remaining : [next]);
        setSelectedSessionId(next.session_id);
      } else {
        setChatSessions(remaining);
      }
      setBusyBySession((all) => { const next = { ...all }; delete next[id]; return next; });
      setContextBySession((all) => { const next = { ...all }; delete next[id]; return next; });
      setModelBySession((all) => { const next = { ...all }; delete next[id]; return next; });
      await refreshSessions();
    } catch (error) {
      setError(String(error));
    } finally {
      setLoadingSessionId(null);
    }
  }

  const sessions = sessionGroups.flatMap((group) => group.sessions);
  const selectedTitle = sessions.find((item) => item.session_id === selectedSessionId)?.title ?? "New chat";
  const busy = Boolean(busyBySession[selectedSessionId]);
  const contextWindow = contextBySession[selectedSessionId] ?? selectedSession?.context_window ?? null;

  return (
    <div className="app-shell">
      <aside className="sessions-panel" aria-label="Sessions">
        <div className="brand"><span>Nosis<span className="brand-dot">.</span></span></div>
        <button className="new-chat" onClick={createNewChat}><Plus size={17} /> New chat</button>
        <div className="section-label">Projects</div>
        <nav className="session-list">
          {sessionGroups.map((group) => {
            const collapsed = collapsedWorkspaces.has(group.workspace);
            return <section className="workspace-group" key={group.workspace}>
              <button className="workspace-group-title" title={group.workspace} aria-expanded={!collapsed} onClick={() => setCollapsedWorkspaces((previous) => {
                const next = new Set(previous);
                if (collapsed) next.delete(group.workspace); else next.add(group.workspace);
                return next;
              })}><span className="workspace-chevron">{collapsed ? <ChevronRight size={13} /> : <ChevronDown size={13} />}</span><span className="workspace-group-name">{group.workspace.split(/[\\/]/).pop() || group.workspace}</span><small>{group.workspace}</small><span className="workspace-session-count">{group.sessions.length}</span></button>
              {!collapsed && group.sessions.map((item) => <div className={`session-row ${item.session_id === selectedSessionId ? "selected" : ""}`} key={item.session_id}><button className="session-button" title={item.title} disabled={loadingSessionId === item.session_id} onClick={() => void selectSession(item.session_id)}><MessageSquare size={15} /><span>{item.title}</span></button><button className="delete-session" aria-label={`删除会话 ${item.title}`} title="删除会话" disabled={loadingSessionId === item.session_id || Boolean(busyBySession[item.session_id])} onClick={(event) => { event.stopPropagation(); void removeSession(item.session_id); }}><Trash2 size={14} /></button></div>)}
            </section>;
          })}
          {sessionGroups.length === 0 && <p className="session-empty">从一段对话开始。</p>}
        </nav>
        <div className="sidebar-footer"><span className="status-dot" /> Personal workspace</div>
      </aside>

      <main className="chat-panel">
        <header className="chat-header"><div className="breadcrumb">Chat <ChevronRight size={14} /><span>{selectedTitle}</span></div><span className="status-label"><span className={`status-dot ${busy ? "working" : ""}`} />{busy ? "Working" : "Ready"}</span></header>
        {error && <div className="error-banner" role="alert">{error}</div>}
        {chatSessions.map((current) => {
          const model = modelBySession[current.session_id] ?? defaultModel;
          return <div key={current.session_id} style={{ display: current.session_id === selectedSessionId ? "contents" : "none" }}><Chat session={current} contextWindow={current.session_id === selectedSessionId ? contextWindow : contextBySession[current.session_id] ?? current.context_window ?? null} workspaceOptions={sessionGroups.map((group) => group.workspace)} inputDisabled={!model} runtimeActive={activeRuntimeIds.has(current.session_id)}
            models={models} model={model} onModelChange={(value) => setModelBySession((all) => ({ ...all, [current.session_id]: value }))} onBusyChange={(value) => {
              setBusyBySession((all) => ({ ...all, [current.session_id]: value }));
              setActiveRuntimeIds((all) => { const next = new Set(all); if (value) next.add(current.session_id); else next.delete(current.session_id); return next; });
            }} onContextWindowChange={(value) => setContextBySession((all) => ({ ...all, [current.session_id]: value }))} onTurnEnd={() => {
            void refreshSessions();
            setWorkspaceVersion((value) => value + 1);
          }} onSessionAvailable={() => { void refreshSessions(); }} onWorkspaceChange={(workspace) => {
            setChatSessions((all) => all.map((item) => item.session_id === current.session_id ? { ...item, workspace } : item));
            setWorkspaceVersion((value) => value + 1);
          }} /></div>;
        })}
      </main>
      {selectedSession && <Workspace version={workspaceVersion} sessionId={selectedSession.session_id} />}
    </div>
  );
}
