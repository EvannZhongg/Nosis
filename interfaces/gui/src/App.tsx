import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Bot, Boxes, CalendarClock, ChevronDown, ChevronRight, KeyRound, LoaderCircle, MessageSquare, Plug, Plus, Settings as SettingsIcon, Sparkles, Trash2 } from "lucide-react";
import { Chat } from "./Chat";
import { Settings, type SettingsSection } from "./Settings";
import { Workspace } from "./Workspace";
import { runtimeIsActive, type ContextWindow, type RuntimePhase } from "@nosis/protocol";
import { deleteSession, get, sessionUrl, type ModelOption, type ModelOptions, type Session, type WorkspaceSessions } from "./api";

type ActiveSession = Session & { provider: string | null; phase: RuntimePhase };
const SELECTED_SESSION_KEY = "nosis.selectedSessionId";

function newSession(workspace?: string | null): Session {
  return {
    session_id: crypto.randomUUID(),
    items: [],
    workspace,
    permission_preset: "ask_for_approval",
  };
}

export function App() {
  const [rememberedSessionId] = useState(() => localStorage.getItem(SELECTED_SESSION_KEY));
  const [sessionGroups, setSessionGroups] = useState<WorkspaceSessions[]>([]);
  const [chatSessions, setChatSessions] = useState<Session[]>([]);
  const [selectedSessionId, setSelectedSessionId] = useState(rememberedSessionId ?? "");
  const [restoringSession, setRestoringSession] = useState(true);
  const [busyBySession, setBusyBySession] = useState<Record<string, boolean>>({});
  const [contextBySession, setContextBySession] = useState<Record<string, ContextWindow | null>>({});
  const [loadingSessionId, setLoadingSessionId] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [workspaceVersion, setWorkspaceVersion] = useState(0);
  const [providers, setProviders] = useState<ModelOption[]>([]);
  const [defaultProvider, setDefaultProvider] = useState("");
  const [providerBySession, setProviderBySession] = useState<Record<string, string>>({});
  const [activeTurnIds, setActiveTurnIds] = useState<Set<string>>(new Set());
  const [collapsedWorkspaces, setCollapsedWorkspaces] = useState<Set<string>>(new Set());
  const [settingsSection, setSettingsSection] = useState<SettingsSection | null>(null);
  const [settingsMenuOpen, setSettingsMenuOpen] = useState(false);
  const settingsMenuRef = useRef<HTMLDivElement>(null);

  const refreshSessions = useCallback(async () => {
    try {
      setSessionGroups(await get<WorkspaceSessions[]>("/api/sessions"));
    } catch (error) {
      setError(String(error));
    }
  }, []);

  useEffect(() => { void refreshSessions(); }, [refreshSessions]);
  useEffect(() => {
    let cancelled = false;
    const restoreSession = async () => {
      try {
        const { root } = await get<{ root: string }>("/api/workspace");
        const restored = rememberedSessionId
          ? await get<Session>(sessionUrl(rememberedSessionId))
          : newSession(root);
        if (cancelled) return;
        const selected = restored.workspace ? restored : { ...restored, workspace: root };
        setChatSessions((all) => [
          selected,
          ...all.filter((item) => item.session_id !== selected.session_id),
        ]);
        setSelectedSessionId(selected.session_id);
        if (selected.provider) {
          setProviderBySession((all) => ({ ...all, [selected.session_id]: selected.provider! }));
        }
        if (selected.context_window) {
          setContextBySession((all) => ({ ...all, [selected.session_id]: selected.context_window ?? null }));
        }
      } catch (error) {
        if (cancelled) return;
        const fresh = newSession();
        setChatSessions((all) => [fresh, ...all]);
        setSelectedSessionId(fresh.session_id);
        setError(String(error));
      } finally {
        if (!cancelled) setRestoringSession(false);
      }
    };
    void restoreSession();
    return () => { cancelled = true; };
  }, [rememberedSessionId]);
  useEffect(() => {
    if (!restoringSession && selectedSessionId) {
      localStorage.setItem(SELECTED_SESSION_KEY, selectedSessionId);
    }
  }, [restoringSession, selectedSessionId]);
  const refreshModels = useCallback(() => {
    get<ModelOptions>("/api/models").then((options) => {
      setProviders(options.models);
      setDefaultProvider(options.default);
    }).catch((error) => setError(String(error)));
  }, []);
  useEffect(() => { refreshModels(); }, [refreshModels]);
  useEffect(() => {
    if (!settingsMenuOpen) return;
    const closeMenu = (event: MouseEvent) => {
      if (!settingsMenuRef.current?.contains(event.target as Node)) setSettingsMenuOpen(false);
    };
    document.addEventListener("mousedown", closeMenu);
    return () => document.removeEventListener("mousedown", closeMenu);
  }, [settingsMenuOpen]);
  useEffect(() => {
    get<ActiveSession[]>("/api/active-sessions").then(async (activeSessions) => {
      setActiveTurnIds(new Set(
        activeSessions
          .filter((session) => runtimeIsActive(session.phase))
          .map((session) => session.session_id),
      ));
      setBusyBySession((all) => ({
        ...all,
        ...Object.fromEntries(activeSessions.map((session) => [session.session_id, runtimeIsActive(session.phase)])),
      }));
      setProviderBySession((all) => ({
        ...all,
        ...Object.fromEntries(activeSessions.flatMap((session) => session.provider ? [[session.session_id, session.provider]] : [])),
      }));
      setContextBySession((all) => ({
        ...all,
        ...Object.fromEntries(activeSessions.map((session) => [session.session_id, session.context_window ?? null])),
      }));
      setChatSessions((all) => [
        ...all,
        ...activeSessions.filter((active) => !all.some((item) => item.session_id === active.session_id)),
      ]);
    }).catch(() => undefined);
  }, []);

  const selectedSession = useMemo(
    () => chatSessions.find((item) => item.session_id === selectedSessionId) ?? chatSessions[0],
    [chatSessions, selectedSessionId],
  );

  async function selectSession(id: string) {
    setSettingsSection(null);
    setSettingsMenuOpen(false);
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
      if (loaded.provider) {
        setProviderBySession((all) => ({ ...all, [id]: loaded.provider! }));
      }
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

  function createNewChat(workspace = selectedSession?.workspace) {
    const fresh = newSession(workspace);
    // Add the new chat to the stable mounted set before making it visible.
    setChatSessions((all) => [...all, fresh]);
    setSelectedSessionId(fresh.session_id);
    setSettingsSection(null);
    setSettingsMenuOpen(false);
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
      setProviderBySession((all) => { const next = { ...all }; delete next[id]; return next; });
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
  const settingsItems: { id: SettingsSection; label: string; description: string; icon: typeof Bot }[] = [
    { id: "providers", label: "Providers", description: "模型、密钥和路由", icon: KeyRound },
    { id: "agent", label: "Agent", description: "工具和运行时设置", icon: Bot },
    { id: "skills", label: "Skills", description: "查看可用技能", icon: Sparkles },
    { id: "plugins", label: "Plugins", description: "查看已安装扩展", icon: Boxes },
    { id: "mcp", label: "MCP", description: "查看外部工具服务", icon: Plug },
    { id: "schedules", label: "Schedules", description: "查看和调整定时任务", icon: CalendarClock },
  ];

  return (
    <div className="app-shell">
      <aside className="sessions-panel" aria-label="Sessions">
        <div className="brand"><span>Nosis<span className="brand-dot">.</span></span></div>
        <button className="new-chat" onClick={() => createNewChat()}><Plus size={17} /> New chat</button>
        <div className="section-label">Projects</div>
        <nav className="session-list">
          {sessionGroups.map((group) => {
            const collapsed = collapsedWorkspaces.has(group.workspace);
            return <section className="workspace-group" key={group.workspace}>
              <div className="workspace-group-header">
                <button className="workspace-group-title" title={group.workspace} aria-expanded={!collapsed} onClick={() => setCollapsedWorkspaces((previous) => {
                  const next = new Set(previous);
                  if (collapsed) next.delete(group.workspace); else next.add(group.workspace);
                  return next;
                })}><span className="workspace-chevron">{collapsed ? <ChevronRight size={13} /> : <ChevronDown size={13} />}</span><span className="workspace-group-name">{group.workspace.split(/[\\/]/).pop() || group.workspace}</span><small>{group.workspace}</small></button>
                <button className="workspace-new-session" aria-label={`在 ${group.workspace} 中新建会话`} title="在此工作区新建会话" onClick={() => createNewChat(group.workspace)}><Plus size={14} /></button>
              </div>
              {!collapsed && group.sessions.map((item) => <div className={`session-row ${item.session_id === selectedSessionId ? "selected" : ""}`} key={item.session_id}><button className="session-button" title={item.title} disabled={loadingSessionId === item.session_id} onClick={() => void selectSession(item.session_id)}>{busyBySession[item.session_id] ? <LoaderCircle size={15} className="spin" role="img" aria-label="正在执行任务" /> : <MessageSquare size={15} />}<span>{item.title}</span></button><button className="delete-session" aria-label={`删除会话 ${item.title}`} title="删除会话" disabled={loadingSessionId === item.session_id || Boolean(busyBySession[item.session_id])} onClick={(event) => { event.stopPropagation(); void removeSession(item.session_id); }}><Trash2 size={14} /></button></div>)}
            </section>;
          })}
          {sessionGroups.length === 0 && <p className="session-empty">从一段对话开始。</p>}
        </nav>
        <div className="settings-menu-wrap" ref={settingsMenuRef}>
          {settingsMenuOpen && <div className="settings-popover" role="menu">
            <div className="settings-popover-label">Settings</div>
            {settingsItems.map((item) => { const Icon = item.icon; return <button type="button" role="menuitem" key={item.id} onClick={() => { setSettingsSection(item.id); setSettingsMenuOpen(false); }}><Icon size={15} /><span><strong>{item.label}</strong><small>{item.description}</small></span><ChevronRight size={13} /></button>; })}
          </div>}
          <button className={`sidebar-footer ${settingsSection ? "selected" : ""}`} aria-expanded={settingsMenuOpen} onClick={() => setSettingsMenuOpen((open) => !open)}><SettingsIcon size={14} /> Settings<ChevronDown size={12} /></button>
        </div>
      </aside>

      {settingsSection && <Settings section={settingsSection} onClose={() => { setSettingsSection(null); void refreshSessions(); }} onChanged={refreshModels} onOpenSession={(sessionId) => void selectSession(sessionId)} />}
      <main className="chat-panel" style={{ display: settingsSection ? "none" : undefined }}>
        <header className="chat-header"><div className="breadcrumb">Chat <ChevronRight size={14} /><span>{restoringSession ? "…" : selectedTitle}</span></div><span className="status-label"><span className={`status-dot ${busy ? "working" : ""}`} />{busy ? "Working" : "Ready"}</span></header>
        {error && <div className="error-banner" role="alert">{error}</div>}
        {!restoringSession && chatSessions.map((current) => {
          const provider = providerBySession[current.session_id] ?? defaultProvider;
          return <div key={current.session_id} style={{ display: current.session_id === selectedSessionId ? "contents" : "none" }}><Chat session={current} selected={current.session_id === selectedSessionId} contextWindow={current.session_id === selectedSessionId ? contextWindow : contextBySession[current.session_id] ?? current.context_window ?? null} workspaceOptions={sessionGroups.map((group) => group.workspace)} backgroundActive={activeTurnIds.has(current.session_id)}
            providers={providers} provider={provider} onProviderChange={(value) => setProviderBySession((all) => ({ ...all, [current.session_id]: value }))} onBusyChange={(value) => {
              setBusyBySession((all) => ({ ...all, [current.session_id]: value }));
              setActiveTurnIds((all) => { const next = new Set(all); if (value) next.add(current.session_id); else next.delete(current.session_id); return next; });
            }} onContextWindowChange={(value) => setContextBySession((all) => ({ ...all, [current.session_id]: value }))} onTurnEnd={() => {
            void refreshSessions();
            setWorkspaceVersion((value) => value + 1);
          }} onSessionAvailable={() => { void refreshSessions(); }} onWorkspaceChange={(workspace) => {
            setChatSessions((all) => all.map((item) => item.session_id === current.session_id ? { ...item, workspace } : item));
            setWorkspaceVersion((value) => value + 1);
          }} /></div>;
        })}
      </main>
      {!settingsSection && selectedSession && <Workspace version={workspaceVersion} sessionId={selectedSession.session_id} />}
    </div>
  );
}
