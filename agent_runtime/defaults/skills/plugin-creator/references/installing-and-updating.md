# Installing and updating plugins

## Where plugins live

```text
~/.nosis/plugins/
`-- <plugin-name>/
    |-- plugin.json
    |-- skills/
    |-- agents/
    `-- mcp/
```

Copy the package into `~/.nosis/plugins/<plugin-name>/`. Nosis discovers direct child directories
containing `plugin.json`. Existing plugin directories are not overwritten by bundled plugins.

## When changes take effect

Restart Nosis after adding, editing, disabling or removing a plugin. Plugin files are not watched,
and opening another Session does not guarantee a reload.

Each execution plane owns its MCP connections and stdio subprocesses. Reassembling the plane
closes its connections and starts fresh servers, so restarting Nosis also reloads server code.

## Discovery failures

If the plugin is missing, check discovery warnings for:

- a `plugin.json` that is unreadable or not a JSON object
- an unknown manifest field, a missing `name`, a bad name, or a wrong field type
- a component path that is absolute or escapes the plugin root
- a second plugin with a name already discovered in the same directory

An invalid Agent or MCP file skips that component. Duplicate Agent or MCP server names keep the
first declaration. If a role's MCP tool is unavailable, check its full tool name, MCP enablement,
server status and `tools.enabled` allowlist. Other components can still load.

## Disabling and removing

- `"enabled": false` keeps the package discovered but routes no component, including its MCP
  servers and agent roles.
- To remove a plugin, delete `~/.nosis/plugins/<plugin-name>/` and restart Nosis. When disabling
  or removing it, also remove any role routes for that plugin from `provider_config.json`.

## Verifying an installed plugin

Run the skill's validator against the installed directory before restarting:

```bash
python3 scripts/validate_plugin.py ~/.nosis/plugins/<plugin-name>
```

The validator checks package files and declarations, including disabled packages. Verify MCP
startup, environment values, tool availability and approval in Nosis separately.
