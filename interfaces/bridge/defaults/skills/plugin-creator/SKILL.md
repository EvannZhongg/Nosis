---
name: plugin-creator
description: Create and validate Nosis Plugin packages — one directory with a `plugin.json` manifest declaring the skills, sub-agent roles, and MCP servers it contributes. Use when a Nosis Plugin must be scaffolded, when components are added to an existing plugin, or when a `plugin.json` needs to be checked against the manifest contract the Runtime accepts.
---

# Plugin Creator

A Nosis Plugin is a directory whose `plugin.json` declares the components it contributes. Bridge
discovers plugins under `~/.nosis/plugins/` while assembling the Runtime and routes every enabled
component into the matching subsystem, so a manifest must satisfy the contract implemented by
`interfaces/bridge/plugins.py`.

## Quick Start

1. Scaffold the package:

```bash
# Run from the skill root (the directory containing this `SKILL.md`).
# Names are normalized to lower-case hyphen-case and must match
# [A-Za-z0-9][A-Za-z0-9._-]* and be <= 64 chars.
python3 scripts/create_basic_plugin.py <plugin-name> --with-skills --with-agents --with-mcp
```

The default target is `~/.nosis/plugins/<plugin-name>`; pass `--path <parent-directory>` only when
the package belongs somewhere else, such as a source tree that is later copied into
`~/.nosis/plugins/`.

2. Replace the scaffold's placeholder metadata in `<plugin-path>/plugin.json` with the real
   `description` and `version`. The scaffold is valid JSON, not finished content.

3. Fill the declared components. Every component entry is a path relative to the plugin root:

| Component | Points at | Registers as |
| --- | --- | --- |
| `skills` | A directory whose children are skill folders containing `SKILL.md` | `<plugin>:<skill>` |
| `agents` | A Markdown file whose front matter declares the role | `<plugin>:<agent>` sub-agent role |
| `mcp` | A JSON file mapping server names to server configs | `<plugin>:<server>` |

Read `references/plugin-json-spec.md` for the exact manifest, agent, MCP, and skill shapes.

4. Validate before handing the plugin back:

```bash
python3 scripts/validate_plugin.py <plugin-path>
```

## Required Behavior

- Keep the manifest at `<plugin-root>/plugin.json` with exactly that name.
- The manifest `name` is the namespace prefix of every component, so it must stay unique across
  `~/.nosis/plugins/`; a duplicate plugin name is skipped at discovery with a warning.
- Declare only components that exist and stay inside the plugin root. Absolute paths and `../`
  escapes are rejected, and a missing component makes the plugin silently contribute nothing.
- Do not add fields the loader rejects. `author`, `license`, `homepage`, `interface`, `mcpServers`,
  `keywords`, and other marketplace-style keys are not part of this contract.
- Keep `capabilities` in agreement with `components`. `capabilities` states what the plugin
  provides; it enables nothing by itself.
- Author skill content with the `skill-creator` skill instead of inventing a competing layout.
- Treat discovery warnings as failures. A malformed plugin does not stop Nosis: it is skipped, so
  the user sees no error until the plugin does nothing.
- After adding or editing a plugin, tell the user to restart Nosis. Plugins are bound when the
  Runtime assembles an execution plane, and plugin files are not part of the configuration
  fingerprint that triggers reassembly.

## References

- `references/plugin-json-spec.md`: manifest fields, component path rules, agent front matter, MCP
  server map, and the canonical sample.
- `references/installing-and-updating.md`: where plugins live, how the built-in plugins are
  installed, and how edits take effect.

## Validation

After editing `SKILL.md`, run:

```bash
python3 ../skill-creator/scripts/quick_validate.py .
```

Before handing back a generated plugin, run:

```bash
python3 scripts/validate_plugin.py <plugin-path>
```
