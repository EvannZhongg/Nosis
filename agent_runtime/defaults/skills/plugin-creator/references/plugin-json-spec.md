# Plugin manifest spec

`plugin.json` sits at the plugin root and is the only file Bridge needs to discover the package.
Everything else is referenced from `components`.

```json
{
  "name": "example-plugin",
  "version": "0.1.0",
  "description": "Example capability package: one skill, one sub-agent role, one MCP server",
  "enabled": true,
  "capabilities": ["skills", "agents", "mcp"],
  "components": {
    "skills": ["skills"],
    "agents": ["agents/reviewer.md"],
    "mcp": ["mcp/servers.json"]
  }
}
```

## Field guide

- `name` (string, required): Plugin identifier and the namespace prefix of every component. Must
  match `[A-Za-z0-9][A-Za-z0-9._-]*`.
- `version` (string, optional): Free-form version string shown in Plugin settings.
- `description` (string, optional): Non-empty string shown in Plugin settings.
- `enabled` (boolean, optional, default `true`): When `false`, the plugin is still discovered but
  none of its components are routed to the Runtime.
- `capabilities` (array of strings, optional): Declaration of what the package provides, shown in
  Plugin settings. Keep it consistent with `components`.
- `components` (object, optional): Maps a component kind to an array of paths, relative to the
  plugin root. Keys are limited to `skills`, `mcp`, and `agents`.

Arrays must contain unique, non-empty strings. An unknown field anywhere in the manifest makes the
whole plugin invalid, and the plugin is then skipped at discovery with a warning instead of
partially loading.

## Component kinds

### `skills`

Each entry is a **directory**. Every direct child directory that contains a `SKILL.md` is
registered as `<plugin>:<skill-name from front matter>`. Children without a `SKILL.md` are ignored,
and a declared directory that does not exist produces a discovery warning.

```text
skills/
`-- release-notes/
    |-- SKILL.md          Required; front matter needs `name` and `description`
    `-- references/       Optional resources loaded on demand
```

Skill front matter follows the Skill contract: `name` and `description` are required, and
`metadata.nosis.register: false` hides an entrypoint from the skill catalog. Author skill content
with the `skill-creator` skill.

### `agents`

Each entry is a **file**, not a directory. The file is Markdown with YAML front matter; the body is
the role's standing instructions and must not be empty. The role registers as `<plugin>:<name>`.

```markdown
---
name: reviewer
description: Review changed files and report concrete findings.
tools:
  - read_file
  - search_files
  - mcp__example-plugin_analyzer__analyze_code
model: inherit
---

Read the requested files, then report findings ordered by severity.
```

- `name` (required), `description` (required): Non-empty strings; `name` must match
  `[A-Za-z0-9][A-Za-z0-9._-]*`. `description` is the text the parent model reads when it chooses
  a role, so state when the role applies, not only what it does.
- `tools` (optional): Array of tool names, without duplicates. Omitted means every built-in role
  tool, without implicitly granting MCP tools; `[]` selects no declared tools. Image and Skill
  tools remain derived from Runtime capabilities. Allowed built-in values: `read_file`,
  `apply_patch`, `edit_file`, `write_file`, `search_files`, `list_directory`,
  `shell`, `web_search`, `web_fetch`, `scheduled_task`.
  Explicit MCP references use the full model-facing name, for example
  `mcp__example-plugin_analyzer__analyze_code`. Each character outside `[A-Za-z0-9_-]`
  in `<plugin>:<server>` and the remote tool name is replaced with `_`.
  References may name tools from another enabled plugin or standalone MCP server.
  Referenced tools must be available after MCP discovery and `tools.enabled` filtering;
  unavailable references prevent that role from loading.
  `subagent` and other main-agent-only tools remain disallowed.
- `model` (optional, string): `inherit`, or a provider name from `provider_config.json`. A name that
  is not a configured provider falls back to the role's configured provider.

Enable `main_agent.tools.subagent` in `agent_config.json` to use Plugin roles.

### `mcp`

Each entry is a **file** holding a server map. Keys are server names and register as
`<plugin>:<server-name>`; stdio servers default their working directory to the plugin root.

```json
{
  "analyzer": {
    "type": "stdio",
    "command": "python",
    "args": ["mcp/server.py"]
  },
  "remote": {
    "type": "http",
    "url": "https://example.com/mcp"
  }
}
```

- Server names may contain letters, numbers, `_`, and `-`; a server's tools reach the model as
  `mcp__<plugin>_<server>__<tool>`.
- `type` accepts `stdio` or `http`; `transport` accepts `stdio` or `streamable_http`. Do not set
  both.
- stdio fields: `command` (required), `args`, `cwd`, `env`.
- http fields: `url` (required), `headers`.
- `env` and `headers` values written as `${NAME}` are resolved from the environment.
- Shared fields: `enabled`, `tools`, `startup_timeout_seconds`, `call_timeout_seconds`, `scope`.
- `scope` defaults to `workspace`. Use `session` to isolate connection state per Session, or
  `global` to share across workspaces. Sharing requires matching connection settings and credentials.
  Global stdio requires an absolute working directory; plugin-relative `cwd` is resolved first.
- `tools.enabled` is an optional array of remote tool names (omitted exposes all; `[]`
  exposes none). `tools.approval` is `always` by default, `never`, or
  `{"always": ["analyze_code"]}`. Approval names must be included in `tools.enabled`
  when that allowlist is present. Selecting an MCP tool in an Agent does not bypass approval.
- Timeouts are positive integers; `enabled` is a boolean. `args` is an array of strings;
  `env` and `headers` are maps of strings.

MCP components are only loaded when `"mcp": {"enabled": true}` is set in `agent_config.json`.

## Path rules

- Paths are relative to the plugin root and must resolve inside it. Absolute paths and `../` escapes
  are rejected when the manifest is parsed.
- Duplicate entries inside one component array are rejected.
- Components must be listed in `components` to load.
- The plugin's own directory name is not part of the manifest contract; keeping it equal to `name`
  is a convention that makes installed plugins easy to identify.
