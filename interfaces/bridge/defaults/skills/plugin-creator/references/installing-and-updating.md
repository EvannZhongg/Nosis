# Installing and updating plugins

Use this reference when a plugin already exists on disk, or when the user asks where a plugin lives
and how Nosis picks it up.

## Where plugins live

```text
~/.nosis/plugins/
`-- <plugin-name>/
    |-- plugin.json
    |-- skills/
    |-- agents/
    `-- mcp/
```

Bridge discovers **direct child directories** of `~/.nosis/plugins/` that contain a `plugin.json`.
Nosis ships no plugins of its own and never overwrites a directory that already exists, so an
installed plugin keeps its local edits.

A plugin that lives elsewhere (for example a repository checkout) is installed by copying it to
`~/.nosis/plugins/<plugin-name>/`.

## When changes take effect

The Runtime binds plugins while assembling an execution plane, together with skills and prompts.
Configuration changes to `provider_config.json`, `agent_config.json`, or `.env` trigger a
reassembly automatically; plugin, skill, and prompt files do not. After adding, editing, moving, or
removing a plugin, tell the user to restart Nosis rather than implying the change is already live.

## Discovery failures

Discovery never blocks startup. Each of these produces a warning and skips the affected package:

- a `plugin.json` that is unreadable or not a JSON object
- an unknown manifest field, a missing `name`, a bad name, or a wrong field type
- a component path that is absolute or escapes the plugin root
- a second plugin with a name already discovered in the same directory

The failure mode to communicate is silence: the plugin loads nothing and Nosis reports success.

## Disabling and removing

- `"enabled": false` keeps the package discovered but routes no component, including its MCP
  servers and agent roles.
- Removing a plugin means deleting `~/.nosis/plugins/<plugin-name>/` and restarting Nosis. If the
  plugin's agent roles are named in `provider_config.json`, remove those entries in the same change:
  an unknown configured role is a hard error at Runtime assembly.

## Verifying an installed plugin

Run the skill's validator against the installed directory before restarting:

```bash
python3 scripts/validate_plugin.py ~/.nosis/plugins/<plugin-name>
```

The validator mirrors the loader's contract; it does not import Nosis, so it works anywhere the
manifest can be read.
