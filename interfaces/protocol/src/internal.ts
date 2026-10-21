/** First-party settings and GUI Host extensions; no public stability promise. */
import type {
  Incoming as BridgeIncoming, Outgoing as BridgeOutgoing, JSONValue,
  ProtocolError, RuntimePhase, RuntimeState, TranscriptCheckpoint,
} from './protocol.js';
export * from './protocol.js';

export type ProviderSetting = {
  id: string;
  model: string;
  url: string | null;
  max_context_tokens: number | null;
  reasoning_effort?: string | null;
  reasoning_parameters?: { readonly [key: string]: JSONValue };
  api?: 'responses' | 'chat_completions' | null;
  credential: {
    source: 'env' | 'inline' | 'none';
    env_name: string | null;
    configured: boolean;
  };
};

export type AgentSettings = {
  max_same_tool_calls: number;
  output_reserve_tokens: number;
  max_generation_tokens: number | null;
  workspace_instruction_files: string[];
  tools: Record<string, boolean>;
  context: { compression: { enabled: boolean; trigger_ratio: number | null; keep_recent_units: number; user_anchor_token_budget: number } };
  memory: { enabled: boolean; global_max_tokens: number; workspace_max_tokens: number };
  subagent_roles: Record<string, { enabled: boolean; description: string; tools: Record<string, boolean>; mcp: Record<string, boolean | Record<string, boolean>> }>;
  mcp_enabled: boolean;
};

export type SettingsSnapshot = {
  revision: string;
  config_directory: string;
  default_provider: string;
  providers: ProviderSetting[];
  routing: {
    main_agent: string;
    fast_model: string | null;
    vision_provider: string | null;
    subagent: string | null;
    subagent_vision_provider: string | null;
    roles: Record<string, { provider?: string; vision_provider?: string }>;
  };
  agent: AgentSettings;
  skills: { id: string; name: string; description: string; source: string; path: string; content: string }[];
  plugins: { name: string; version: string | null; description: string | null; enabled: boolean; capabilities: string[]; path: string; components: { skills: string[]; mcp: string[]; agents: string[] } }[];
  mcp_servers: { id: string; source: string; enabled: boolean; transport: string; command: string | null; args: string[]; cwd: string | null; url: string | null; env_names: string[]; header_names: string[]; startup_timeout_seconds: number; call_timeout_seconds: number }[];
  plugin_agents: { name: string; description: string; model: string | null; source: string }[];
  warnings: string[];
};

type HostEnvelope = {
  event_sequence?: number;
  /** GUI delivery cursor, including undelivered opening/fatal errors. */
  resume_after?: number;
  runtime?: RuntimeState;
  transcript?: TranscriptCheckpoint;
  replayed?: boolean;
};

type HostEvent<T> = T extends { type: string }
  ? Omit<T, 'event_sequence'> & HostEnvelope
  : never;

export type Incoming = HostEvent<BridgeIncoming> | ((
  | { type: 'attachment_replaced'; phase: RuntimePhase }
  | { type: 'settings_snapshot'; request_id?: string; settings: SettingsSnapshot }
  | { type: 'settings_update_failed'; request_id?: string; error: ProtocolError }
) & HostEnvelope);

export type Outgoing =
  | Exclude<BridgeOutgoing, { type: 'open_session' }>
  | (Extract<BridgeOutgoing, { type: 'open_session' }> & {
      attach_only?: boolean;
      after_event?: number;
      attachment_id?: string;
      takeover?: boolean;
    })
  | { type: 'settings_get'; request_id?: string }
  | { type: 'settings_provider_save'; request_id?: string; expected_revision?: string; provider: string; settings: JSONValue }
  | { type: 'settings_agent_save'; request_id?: string; expected_revision?: string; settings: JSONValue }
  | { type: 'settings_routing_save'; request_id?: string; expected_revision?: string; settings: JSONValue };
