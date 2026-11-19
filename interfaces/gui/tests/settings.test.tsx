// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";
import type { SettingsSnapshot } from "@nosis/protocol/internal";
import { Settings } from "../src/Settings";
import { get, put } from "../src/api";

vi.mock("../src/api", () => ({ get: vi.fn(), put: vi.fn() }));

afterEach(() => {
  vi.unstubAllGlobals();
  vi.resetAllMocks();
});

it("loads and saves the default permissions for new sessions", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const settings: SettingsSnapshot = {
    revision: "revision-1", config_directory: "/config", default_provider: "test",
    providers: [],
    routing: {
      main_agent: "test", fast_model: null, vision_provider: null,
      subagent: null, subagent_vision_provider: null, roles: {},
    },
    agent: {
      max_same_tool_calls: 5, output_reserve_tokens: 100, max_generation_tokens: null,
      generate_session_title: true, default_permission_preset: "workspace_access",
      workspace_instruction_files: ["AGENTS.md"], tools: {},
      context: { compression: { enabled: true, trigger_ratio: null, keep_recent_units: 4, user_anchor_token_budget: 2000 } },
      memory: { enabled: true, global_max_tokens: 2000, workspace_max_tokens: 3000 },
      subagent_roles: {}, mcp_enabled: false,
    },
    skills: [], plugins: [], mcp_servers: [], plugin_agents: [], warnings: [],
  };
  vi.mocked(get).mockResolvedValue(settings);
  vi.mocked(put).mockResolvedValue({
    ...settings, revision: "revision-2",
    agent: { ...settings.agent, default_permission_preset: "full_access" },
  });
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  try {
    await act(async () => root.render(
      <Settings section="agent" onClose={() => {}} onChanged={() => {}} onOpenSession={() => {}} />,
    ));
    const select = container.querySelector<HTMLSelectElement>("select")!;
    expect(select.labels?.[0].textContent).toContain("Default permissions");
    expect(select.value).toBe("workspace_access");
    expect(Array.from(select.options, (option) => option.value)).toEqual([
      "ask_for_approval", "workspace_access", "full_access",
    ]);
    await act(async () => {
      select.value = "full_access";
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await act(async () => {
      container.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    });
    expect(put).toHaveBeenCalledWith("/api/settings/agent", {
      ...settings.agent, default_permission_preset: "full_access", expected_revision: "revision-1",
    });
    expect(container.querySelector("select")!.value).toBe("full_access");
  } finally {
    await act(async () => root.unmount());
    container.remove();
  }
});
