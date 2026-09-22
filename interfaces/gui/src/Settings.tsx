import { useEffect, useMemo, useState, type ReactNode } from "react";
import { ArrowLeft, Boxes, Check, ChevronRight, CircleAlert, KeyRound, MessageSquare, Plus, RefreshCw, Save, ServerCog } from "lucide-react";
import { get, put, type SettingsSnapshot, type ScheduleSummary, type ScheduleTrigger } from "./api";

export type SettingsSection = "providers" | "agent" | "skills" | "plugins" | "mcp" | "schedules";

const sectionLabels: Record<SettingsSection, string> = {
  providers: "Providers",
  agent: "Agent",
  skills: "Skills",
  plugins: "Plugins",
  mcp: "MCP",
  schedules: "Schedules",
};

export function Settings({ section, onClose, onChanged, onOpenSession }: { section: SettingsSection; onClose: () => void; onChanged: () => void; onOpenSession: (sessionId: string) => void }) {
  const [settings, setSettings] = useState<SettingsSnapshot>();
  const [selected, setSelected] = useState("");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [scheduleRefresh, setScheduleRefresh] = useState(0);

  async function refresh() {
    try {
      const value = await get<SettingsSnapshot>("/api/settings");
      setSettings(value);
      if (!selected && value.providers[0]) setSelected(value.providers[0].id);
      setError("");
    } catch (reason) {
      setError(String(reason));
    }
  }

  useEffect(() => { void refresh(); }, []);

  async function saveProvider(id: string, payload: unknown) {
    setSaving(true);
    try {
      const value = await put<SettingsSnapshot>(`/api/settings/providers/${encodeURIComponent(id)}`, { ...(payload as object), expected_revision: settings?.revision });
      setSettings(value);
      setSelected(id);
      setError("");
      onChanged();
    } catch (reason) {
      setError(String(reason));
    } finally {
      setSaving(false);
    }
  }

  async function saveAgent(payload: unknown) {
    setSaving(true);
    try {
      setSettings(await put<SettingsSnapshot>("/api/settings/agent", { ...(payload as object), expected_revision: settings?.revision }));
      setError("");
    } catch (reason) {
      setError(String(reason));
    } finally {
      setSaving(false);
    }
  }

  async function saveRouting(payload: unknown) {
    setSaving(true);
    try {
      setSettings(await put<SettingsSnapshot>("/api/settings/routing", { ...(payload as object), expected_revision: settings?.revision }));
      setError("");
      onChanged();
    } catch (reason) {
      setError(String(reason));
    } finally {
      setSaving(false);
    }
  }

  return <section className="settings-shell">
    <header className="settings-header">
      <h1>{sectionLabels[section]}</h1>
      <div className="settings-header-actions"><button className="secondary-button" onClick={() => section === "schedules" ? setScheduleRefresh((value) => value + 1) : void refresh()} disabled={saving}><RefreshCw size={14} /> 刷新</button><button className="settings-back" onClick={onClose}><ArrowLeft size={14} /> 返回对话</button></div>
    </header>
    {error && <div className="settings-error" role="alert"><CircleAlert size={15} /><span>{error}</span></div>}
    <div className="settings-layout"><main className="settings-content">
        {!settings && section !== "schedules" ? <LoadingState /> : section === "schedules" ? <ScheduleSettings refreshVersion={scheduleRefresh} onOpenSession={onOpenSession} /> : section === "providers" ? <ProviderSettings settings={settings!} selected={selected} onSelect={setSelected} saving={saving} onSave={saveProvider} onSaveRouting={saveRouting} />
          : section === "agent" ? <AgentSettingsForm key={settings!.revision} settings={settings!} saving={saving} onSave={saveAgent} />
          : section === "skills" ? <DetailCollection eyebrow="Skill library" items={settings!.skills.map((item) => ({ id: item.id, title: item.name, subtitle: `${item.source} · ${item.path}`, body: item.content, status: "Available" }))} selected={selected} onSelect={setSelected} empty="没有发现 Skill。" />
          : section === "plugins" ? <DetailCollection eyebrow="Plugin catalog" items={settings!.plugins.map((item) => ({ id: item.name, title: item.name, subtitle: item.description ?? item.path, status: item.enabled ? "Enabled" : "Disabled", body: JSON.stringify({ version: item.version, capabilities: item.capabilities, components: item.components, path: item.path }, null, 2) }))} selected={selected} onSelect={setSelected} empty="没有发现 Plugin。" />
          : <DetailCollection eyebrow="MCP registry" items={settings!.mcp_servers.map((item) => ({ id: item.id, title: item.id, subtitle: `${item.transport} · ${item.source}`, status: item.enabled ? "Enabled" : "Disabled", body: JSON.stringify(item, null, 2) }))} selected={selected} onSelect={setSelected} empty={settings!.agent.mcp_enabled ? "没有配置 MCP Server。" : "MCP 当前已关闭。"} />}
        {settings && settings.warnings.length > 0 && <details className="settings-warnings"><summary><CircleAlert size={14} /> {settings.warnings.length} 条配置警告</summary>{settings.warnings.map((warning) => <p key={warning}>{warning}</p>)}</details>}
      </main>
    </div>
  </section>;
}

function formatScheduleTime(value?: string | null) {
  if (!value) return null;
  return new Intl.DateTimeFormat("zh-CN", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

function formatInterval(seconds: number) {
  if (seconds % 86400 === 0) return `${seconds / 86400} 天`;
  if (seconds % 3600 === 0) return `${seconds / 3600} 小时`;
  if (seconds % 60 === 0) return `${seconds / 60} 分钟`;
  return `${seconds} 秒`;
}

function formatScheduleTrigger(trigger: ScheduleTrigger) {
  if (trigger.type === "once") {
    return `一次性 · ${formatScheduleTime(trigger.at)}`;
  }
  if (trigger.type === "interval") {
    const start = formatScheduleTime(trigger.start_at);
    return `每 ${formatInterval(trigger.seconds)}${start ? ` · 起始 ${start}` : ""}`;
  }
  return `Cron ${trigger.expression} · ${trigger.timezone}`;
}

function scheduleStatus(item: ScheduleSummary) {
  const status = item.latest_run?.status;
  if (status === "running") return item.session_available
    ? { label: "执行中", tone: "running" }
    : { label: "执行未完成", tone: "failed" };
  if (status === "failed") return { label: "执行失败", tone: "failed" };
  if (status === "skipped") return { label: "已跳过", tone: "muted" };
  if (status === "completed" && !item.enabled) return { label: "已完成", tone: "complete" };
  return item.enabled
    ? { label: "已启用", tone: "active" }
    : { label: "已停用", tone: "muted" };
}

function ScheduleSettings({ refreshVersion, onOpenSession }: { refreshVersion: number; onOpenSession: (sessionId: string) => void }) {
  const [items, setItems] = useState<ScheduleSummary[]>([]);
  const [error, setError] = useState("");
  const refresh = () => get<ScheduleSummary[]>("/api/schedules").then((value) => { setItems(value); setError(""); }).catch((reason) => setError(String(reason)));
  useEffect(() => { void refresh(); }, [refreshVersion]);
  return <div className="schedule-settings">
    {error && <p className="schedule-error">{error}</p>}
    {items.length === 0 ? <div className="empty-settings"><strong>暂无定时任务</strong></div> : <div className="schedule-list">{items.map((item) => {
      const status = scheduleStatus(item);
      const time = formatScheduleTime(item.next_run_at ?? item.latest_run?.scheduled_for);
      const end = formatScheduleTime(item.end_at);
      return <section className="schedule-row" key={item.schedule_id}>
        <span className={`schedule-state ${status.tone}`} aria-hidden="true" />
        <div className="schedule-copy"><strong title={item.prompt}>{item.prompt}</strong><small title={item.workspace}>{formatScheduleTrigger(item.trigger)} · {item.workspace}{time ? ` · ${item.next_run_at ? "下次" : "最近"} ${time}` : ""}{end ? ` · 截止 ${end}` : ""}</small></div>
        <span className={`schedule-status ${status.tone}`} title={item.latest_run?.error ?? undefined}>{status.label}</span>
        <button className="schedule-session-button" disabled={!item.session_available} title={item.session_available ? `打开 Session ${item.schedule_session_id}` : "本次执行尚未生成会话记录"} onClick={() => onOpenSession(item.schedule_session_id)}><MessageSquare size={13} />{item.session_available ? "会话" : "无记录"}</button>
      </section>;
    })}</div>}
  </div>;
}

function LoadingState() {
  return <div className="settings-loading"><span className="settings-loading-mark" /><span>Loading configuration</span></div>;
}

function SettingsCard({ title, description, action, children, className = "" }: { title: string; description?: string; action?: ReactNode; children: ReactNode; className?: string }) {
  return <section className={`settings-card ${className}`}><header className="settings-card-header"><div><h3>{title}</h3>{description && <p>{description}</p>}</div>{action}</header><div className="settings-card-body">{children}</div></section>;
}

function ProviderSettings({ settings, selected, onSelect, saving, onSave, onSaveRouting }: { settings: SettingsSnapshot; selected: string; onSelect: (id: string) => void; saving: boolean; onSave: (id: string, payload: unknown) => Promise<void>; onSaveRouting: (payload: unknown) => Promise<void> }) {
  const provider = settings.providers.find((item) => item.id === selected) ?? settings.providers[0];
  const [creating, setCreating] = useState(false);
  const editKey = creating ? "__new__" : provider?.id ?? "";
  return <div className="provider-layout"><aside className="provider-catalog"><button className="provider-add" onClick={() => { setCreating(true); onSelect(""); }}><Plus size={15} /><span>New provider</span></button><div className="provider-count">{settings.providers.length} configured</div><div className="provider-list">{settings.providers.map((item) => <button key={item.id} className={!creating && item.id === provider?.id ? "selected" : ""} onClick={() => { setCreating(false); onSelect(item.id); }}><span className={`provider-status ${item.credential.configured || item.url?.includes("localhost") ? "ready" : ""}`} /><span className="provider-list-copy"><strong>{item.id}</strong><small>{item.model}</small></span>{item.id === settings.default_provider && <span className="default-badge">Default</span>}</button>)}</div></aside><div className="provider-editor"><ProviderForm key={`${settings.revision}:${editKey}`} provider={creating ? undefined : provider} isDefault={!creating && provider?.id === settings.default_provider} saving={saving} onSave={async (id, payload) => { await onSave(id, payload); setCreating(false); }} /><RoutingForm key={`routes:${settings.revision}`} settings={settings} saving={saving} onSave={onSaveRouting} /></div></div>;
}

function RoutingForm({ settings, saving, onSave }: { settings: SettingsSnapshot; saving: boolean; onSave: (payload: unknown) => Promise<void> }) {
  const [routing, setRouting] = useState(() => structuredClone(settings.routing));
  const providerOptions = (optional: boolean) => <>{optional && <option value="">Inherit / Not configured</option>}{settings.providers.map((provider) => <option key={provider.id} value={provider.id}>{provider.id} · {provider.model}</option>)}</>;
  return <form onSubmit={(event) => { event.preventDefault(); void onSave(routing); }}><SettingsCard title="Provider routing" description="Choose the defaults used by new sessions, vision tasks, and sub-agents." action={<button className="secondary-button" disabled={saving}><Save size={14} /> Save routing</button>}><div className="settings-fields-grid"><Field label="Main agent"><select value={routing.main_agent} onChange={(event) => setRouting({ ...routing, main_agent: event.target.value })}>{providerOptions(false)}</select></Field><Field label="Vision provider"><select value={routing.vision_provider ?? ""} onChange={(event) => setRouting({ ...routing, vision_provider: event.target.value || null })}>{providerOptions(true)}</select></Field><Field label="Sub-agent"><select value={routing.subagent ?? ""} onChange={(event) => setRouting({ ...routing, subagent: event.target.value || null })}>{providerOptions(true)}</select></Field><Field label="Sub-agent vision"><select value={routing.subagent_vision_provider ?? ""} onChange={(event) => setRouting({ ...routing, subagent_vision_provider: event.target.value || null })}>{providerOptions(true)}</select></Field></div>{Object.entries(routing.roles).map(([name, route]) => <div className="routing-role" key={name}><div className="routing-role-name"><ServerCog size={14} />{name}</div><div className="settings-fields-grid"><Field label="Provider"><select value={route.provider ?? ""} onChange={(event) => setRouting({ ...routing, roles: { ...routing.roles, [name]: { ...route, provider: event.target.value } } })}>{providerOptions(true)}</select></Field><Field label="Vision provider"><select value={route.vision_provider ?? ""} onChange={(event) => setRouting({ ...routing, roles: { ...routing.roles, [name]: { ...route, vision_provider: event.target.value } } })}>{providerOptions(true)}</select></Field></div></div>)}</SettingsCard></form>;
}

function ProviderForm({ provider, isDefault, saving, onSave }: { provider?: SettingsSnapshot["providers"][number]; isDefault: boolean; saving: boolean; onSave: (id: string, payload: unknown) => Promise<void> }) {
  const [id, setId] = useState(provider?.id ?? "");
  const [model, setModel] = useState(provider?.model ?? "");
  const [url, setUrl] = useState(provider?.url ?? "");
  const [limit, setLimit] = useState(provider?.max_context_tokens?.toString() ?? "");
  const [key, setKey] = useState("");
  const [clearKey, setClearKey] = useState(false);
  const [setDefault, setSetDefault] = useState(isDefault);
  const credentialReady = provider?.credential.configured;
  return <form onSubmit={(event) => { event.preventDefault(); void onSave(id.trim(), { model: model.trim(), url: url.trim() || null, max_context_tokens: limit ? Number(limit) : null, api_key: clearKey ? { action: "clear" } : key ? { action: "set", value: key } : { action: "keep" }, set_default: setDefault }); }}><SettingsCard className="provider-form-card" title={provider ? provider.id : "Create provider"} description={provider ? "Connection details and credentials for this model endpoint." : "Add a reusable model endpoint to Nosis."} action={<button className="primary-button" disabled={saving || !id.trim() || !model.trim()}><Save size={14} /> Save provider</button>}><div className="credential-banner"><span className={`credential-icon ${credentialReady ? "ready" : ""}`}>{credentialReady ? <Check size={14} /> : <KeyRound size={14} />}</span><span><strong>{credentialReady ? "Credential configured" : "Credential required"}</strong><small>{provider?.credential.env_name ? `Stored as ${provider.credential.env_name}` : "Keys are written to ~/.nosis/.env and never returned to the browser."}</small></span></div><div className="settings-fields-grid"><Field label="Provider ID" hint="Stable identifier used by sessions"><input value={id} onChange={(event) => setId(event.target.value)} disabled={Boolean(provider)} placeholder="openai-main" /></Field><Field label="Model" hint="LiteLLM model identifier"><input value={model} onChange={(event) => setModel(event.target.value)} placeholder="openai/gpt-5" /></Field><Field label="Base URL" hint="Optional OpenAI-compatible endpoint"><input value={url} onChange={(event) => setUrl(event.target.value)} placeholder="https://api.openai.com/v1" /></Field><Field label="Context window" hint="Leave empty for automatic detection"><input type="number" min="1" value={limit} onChange={(event) => setLimit(event.target.value)} placeholder="Automatic" /></Field></div><Field label="API key" hint={credentialReady ? "Leave blank to keep the current key" : "Saved locally and never shown again"}><input type="password" autoComplete="new-password" value={key} onChange={(event) => { setKey(event.target.value); setClearKey(false); }} placeholder={credentialReady ? "••••••••••••••••" : "Enter API key"} /></Field><div className="provider-options">{provider?.credential.configured && <Toggle checked={clearKey} onChange={(checked) => { setClearKey(checked); if (checked) setKey(""); }} label="Clear saved key" description="Remove the environment variable value on save." danger />}<Toggle checked={setDefault} disabled={isDefault} onChange={setSetDefault} label={isDefault ? "Default provider" : "Make default"} description="Used when a new session does not choose a provider." /></div></SettingsCard></form>;
}

function AgentSettingsForm({ settings, saving, onSave }: { settings: SettingsSnapshot; saving: boolean; onSave: (payload: unknown) => Promise<void> }) {
  const [agent, setAgent] = useState(() => structuredClone(settings.agent));
  const update = (changes: Partial<typeof agent>) => setAgent((current) => ({ ...current, ...changes }));
  return <form className="agent-settings" onSubmit={(event) => { event.preventDefault(); void onSave(agent); }}><div className="settings-toolbar"><p>Changes apply when the next execution environment is created.</p><button className="primary-button" disabled={saving}><Save size={14} /> Save changes</button></div><SettingsCard title="Runtime limits" description="Keep loops bounded and reserve enough room for a complete response."><div className="settings-fields-grid three-columns"><Field label="Repeated tool calls"><input type="number" min="1" value={agent.max_same_tool_calls} onChange={(event) => update({ max_same_tool_calls: Number(event.target.value) })} /></Field><Field label="Output reserve"><input type="number" min="1" value={agent.output_reserve_tokens} onChange={(event) => update({ output_reserve_tokens: Number(event.target.value) })} /></Field><Field label="Generation limit"><input type="number" min="1" value={agent.max_generation_tokens ?? ""} onChange={(event) => update({ max_generation_tokens: event.target.value ? Number(event.target.value) : null })} placeholder="Unlimited" /></Field></div></SettingsCard><SettingsCard title="Context" description="Control when older work is compressed and which workspace instructions are loaded."><div className="settings-fields-grid"><Field label="Keep recent units"><input type="number" min="1" value={agent.context.compression.keep_recent_units} onChange={(event) => update({ context: { compression: { ...agent.context.compression, keep_recent_units: Number(event.target.value) } } })} /></Field><Field label="Trigger ratio"><input type="number" min="0.01" max="0.99" step="0.01" value={agent.context.compression.trigger_ratio ?? ""} onChange={(event) => update({ context: { compression: { ...agent.context.compression, trigger_ratio: event.target.value ? Number(event.target.value) : null } } })} placeholder="Default" /></Field></div><Field label="Instruction files"><input value={agent.workspace_instruction_files.join(", ")} onChange={(event) => update({ workspace_instruction_files: event.target.value.split(",").map((item) => item.trim()).filter(Boolean) })} /></Field><div className="toggle-list"><Toggle checked={agent.context.compression.enabled} onChange={(checked) => update({ context: { compression: { ...agent.context.compression, enabled: checked } } })} label="Context compression" description="Summarize older context as the session approaches its limit." /><Toggle checked={agent.memory.enabled} onChange={(checked) => update({ memory: { ...agent.memory, enabled: checked } })} label="Long-term memory" description="Retain durable preferences, facts, and workspace decisions across sessions." /><Toggle checked={agent.mcp_enabled} onChange={(checked) => update({ mcp_enabled: checked })} label="MCP servers" description="Allow configured MCP servers to join the runtime." /></div><div className="settings-fields-grid"><Field label="Global memory limit"><input type="number" min="1" value={agent.memory.global_max_tokens} onChange={(event) => update({ memory: { ...agent.memory, global_max_tokens: Number(event.target.value) } })} /></Field><Field label="Workspace memory limit"><input type="number" min="1" value={agent.memory.workspace_max_tokens} onChange={(event) => update({ memory: { ...agent.memory, workspace_max_tokens: Number(event.target.value) } })} /></Field></div></SettingsCard><SettingsCard title="Main agent tools" description="Choose the capabilities available to the primary agent."><div className="tool-grid">{Object.entries(agent.tools).map(([name, enabled]) => <Toggle key={name} compact checked={enabled} onChange={(checked) => update({ tools: { ...agent.tools, [name]: checked } })} label={name.replaceAll("_", " ")} />)}</div></SettingsCard><SettingsCard title="Sub-agent roles" description="Enable specialist roles and control their tool access.">{Object.entries(agent.subagent_roles).length ? Object.entries(agent.subagent_roles).map(([name, role]) => <div className="settings-role" key={name}><div className="role-heading"><Toggle checked={role.enabled} onChange={(checked) => update({ subagent_roles: { ...agent.subagent_roles, [name]: { ...role, enabled: checked } } })} label={name} description={role.description} /></div><Field label="Description"><input value={role.description} onChange={(event) => update({ subagent_roles: { ...agent.subagent_roles, [name]: { ...role, description: event.target.value } } })} /></Field><div className="tool-grid">{Object.entries(role.tools).map(([tool, enabled]) => <Toggle key={tool} compact checked={enabled} onChange={(checked) => update({ subagent_roles: { ...agent.subagent_roles, [name]: { ...role, tools: { ...role.tools, [tool]: checked } } } })} label={tool.replaceAll("_", " ")} />)}</div></div>) : <p className="settings-muted">没有配置 Sub-agent role。</p>}</SettingsCard></form>;
}

function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return <label className="settings-field"><span>{label}</span>{children}{hint && <small>{hint}</small>}</label>;
}

function Toggle({ checked, disabled = false, onChange, label, description, compact = false, danger = false }: { checked: boolean; disabled?: boolean; onChange: (checked: boolean) => void; label: string; description?: string; compact?: boolean; danger?: boolean }) {
  return <label className={`settings-toggle ${compact ? "compact" : ""} ${danger ? "danger" : ""}`}><input type="checkbox" checked={checked} disabled={disabled} onChange={(event) => onChange(event.target.checked)} /><span className="toggle-control" /><span className="toggle-copy"><strong>{label}</strong>{description && <small>{description}</small>}</span></label>;
}

function DetailCollection({ eyebrow, items, selected, onSelect, empty }: { eyebrow: string; items: { id: string; title: string; subtitle: string; body: string; status: string }[]; selected: string; onSelect: (id: string) => void; empty: string }) {
  const item = useMemo(() => items.find((entry) => entry.id === selected) ?? items[0], [items, selected]);
  if (!item) return <div className="empty-settings"><Boxes size={24} /><strong>{empty}</strong><span>配置目录中还没有可展示的内容。</span></div>;
  return <div className="detail-layout"><div className="detail-list"><div className="provider-count">{items.length} discovered</div>{items.map((entry) => <button key={entry.id} className={entry.id === item.id ? "selected" : ""} onClick={() => onSelect(entry.id)}><span><strong>{entry.title}</strong><small>{entry.subtitle}</small></span><ChevronRight size={13} /></button>)}</div><SettingsCard key={item.id} title={item.title} description={item.subtitle} action={<span className="status-badge"><span className="status-dot" />{item.status}</span>}><div className="detail-eyebrow">{eyebrow}</div><pre className="settings-code">{item.body}</pre></SettingsCard></div>;
}
