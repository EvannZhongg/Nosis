# Plugin Manifest Reference

Complete reference for `plugin.json` configuration.

## File Location

**Required path**: `~/.nosis/plugins/<plugin-name>/plugin.json`

Nosis discovers plugins by scanning the directories directly under `~/.nosis/plugins/`. A directory counts as a plugin only when it contains `plugin.json`; the manifest must sit at the plugin root. A nested or renamed manifest is never read.

## Complete Field Reference

The manifest is validated strictly: every field must be one of the seven below.

**Unknown fields are an error, not a warning.** A manifest carrying a field Nosis does not know makes the whole plugin fail to load, so no part of it is applied. See [Common Validation Errors](#common-validation-errors).

### name (required)

**Type**: String
**Example**: `"test-automation-suite"`

The unique identifier for the plugin. Everything the plugin contributes is namespaced with it: Skills register as `<plugin-name>:<skill-name>`, Agents as `<plugin-name>:<agent-name>`, MCP servers as `<plugin-name>:<server-name>` and their tools as `mcp__<plugin>_<server>__<tool>`.

**Requirements** (enforced):
- Must be a non-empty string
- May contain only letters, numbers, `.`, `_`, and `-`
- Must start with a letter or number
- Must be unique: when two plugin directories declare the same name, the second one is skipped

**Validation**: `^[A-Za-z0-9][A-Za-z0-9._-]*$`

**Examples**:
- ✅ Good: `api-tester`, `code-review`, `git-workflow-automation`
- ❌ Bad: `API Tester` (space), `-git-workflow` (leading hyphen), `code/review` (slash)

### version

**Type**: String
**Example**: `"1.2.0"`
**Default**: none

Version of the plugin. Nothing in Nosis resolves, compares, or updates versions, so this field is documentation for whoever installs the plugin; the semantic-versioning conventions below are worth following anyway.

**Semantic versioning guidelines**:
- **MAJOR**: incompatible changes, breaking changes
- **MINOR**: new functionality, backward-compatible
- **PATCH**: bug fixes, backward-compatible

**Pre-release versions**:
- `"1.0.0-alpha.1"` - Alpha release
- `"1.0.0-beta.2"` - Beta release
- `"1.0.0-rc.1"` - Release candidate

**Examples**:
- `"0.1.0"` - Initial development
- `"1.0.0"` - First stable release
- `"1.2.3"` - Patch update to 1.2
- `"2.0.0"` - Major version with breaking changes

### description

**Type**: String
**Length**: 50-200 characters recommended
**Example**: `"Automates code review workflows with style checks and automated feedback"`

Brief explanation of what the plugin does. Shown to users when the plugin is listed, so it should read as a one-line answer to "what do I get if I enable this?".

**Best practices**:
- Describe what the plugin does, not how it is built
- Use active voice
- Mention the key capability
- Keep it under 200 characters

**Examples**:
- ✅ `"Generates test suites from coverage gaps and runs them on demand"`
- ❌ `"A plugin that contains skills and agents that were written to help with tests"`

### enabled

**Type**: Boolean
**Default**: `true`

Whether the plugin's components load. A disabled plugin is still discovered and still has to be valid, but none of its Skills, Agents, or MCP servers are registered.

Use this to park a plugin you want to keep installed but temporarily inactive, instead of moving or deleting its directory.

### dependencies

**Type**: Array of strings
**Default**: `[]`
**Example**: `["shared-tooling", "code-review"]`

Names of other plugins this one builds on.

**Declarative only**: Nosis records these names and nothing more. It does not install, order, or verify them, and a missing dependency does not stop the plugin from loading. Write them down for the reader; make the README explain what is actually required.

**Rules**: non-empty strings, no duplicates.

### capabilities

**Type**: Array of strings
**Default**: `[]`
**Example**: `["skills", "agents"]`

Short labels describing what the plugin offers.

**Free-form**: Nosis does not validate these against a fixed list; by convention they name the component kinds the plugin ships (`skills`, `agents`, `mcp`, `hooks`).

**Rules**: non-empty strings, no duplicates.

### components

**Type**: Object
**Default**: `{}`
**Allowed keys**: `skills`, `mcp`, `agents`, `hooks`

Maps each component kind to the paths that provide it. Any other key is rejected as an unknown field.

**Path rules for every value**:
- Array of strings, no duplicates, no empty entries
- Paths are resolved against the plugin root and may be `dir/file.md` or `./dir`
- Absolute paths are rejected
- Paths that resolve outside the plugin root (for example `../elsewhere`) are rejected
- Omit a kind the plugin does not use, or set it to `[]`

#### components.skills

Paths to Skill source directories. Each **direct child directory** of such a path must contain `SKILL.md` to be registered; children without one are skipped. Registered as `<plugin-name>:<skill-name>`.

`~/.nosis/skills/<skill-name>/SKILL.md` keeps working as an unnamespaced standalone Skill, so a Skill can live either place.

**Example**:
```json
{
  "components": {
    "skills": ["skills"]
  }
}
```

**Directory layout this implies**:
```
skills/
├── my-skill/SKILL.md          ✅ registered as my-plugin:my-skill
└── docs/notes.md              ❌ no SKILL.md, ignored
```

#### components.agents

Paths to Markdown Agent definitions, one file per Agent. Registered as `<plugin-name>:<agent-name>`.

Frontmatter requirements are covered in the agent-development skill; in short: `name` and `description` are required, `tools` (Nosis tool names) and `model` (a provider name, or `inherit`) are optional, and other frontmatter fields are ignored.

```json
{
  "components": {
    "agents": ["agents/test-writer.md"]
  }
}
```

#### components.mcp

Paths to `.mcp.json` server maps. The file format is the standard MCP configuration.

**Load condition**: declared MCP servers load only when the main `agent_config.json` has `mcp.enabled` set to `true`. The plugin declares servers; whether MCP runs at all stays the user's decision.

Server identity becomes `<plugin-name>:<server-name>`, and for `stdio` servers the working directory defaults to the plugin root, with relative commands and paths resolved against it.

```json
{
  "components": {
    "mcp": [".mcp.json"]
  }
}
```

#### components.hooks

Paths to hook declarations. **Reserved**: Nosis has no hook runtime, so these paths are recorded and nothing executes them. Declare them only if you maintain the same plugin for a tool that does run hooks.

## Configuration Layout

```text
~/.nosis/
├── plugins/
│   └── my-plugin/
│       ├── plugin.json          ← manifest (required)
│       ├── skills/              ← components.skills
│       │   └── my-skill/SKILL.md
│       ├── agents/              ← components.agents
│       │   └── reviewer.md
│       ├── .mcp.json            ← components.mcp
│       └── scripts/
└── skills/                      ← standalone Skills, no namespace
    └── my-skill/SKILL.md
```

There is no project-level plugin directory: plugins are installed once per user, and every plugin loads for every workspace. A plugin that only makes sense in one workspace should say so in its Skill descriptions.

## Validation

### What Nosis checks

Manifest level, in order:

1. `plugin.json` parses as a JSON object
2. No unknown top-level field, and no unknown key under `components`
3. `name` present, and matching the allowed pattern
4. `enabled` (when present) is a boolean
5. `version`, `description` are non-empty strings when present
6. `dependencies`, `capabilities`, and every `components` value are arrays of non-empty strings without duplicates
7. Every `components` path is relative and stays inside the plugin root

Component level:

- `components.skills`: a declared directory that does not exist produces a warning; child directories without `SKILL.md` are ignored
- `components.agents`: frontmatter must parse, `name` and `description` must be present, tool names must be Nosis tool names
- `components.mcp`: the file must parse and hold a valid server map

**Failure handling**: a manifest-level error skips the whole plugin with a warning; a component-level error skips only that component, and the rest of the plugin still loads.

### Common Validation Errors

**Unknown field** (the manifest fails to load):
```json
{
  "name": "my-plugin",
  "author": "Jane Developer",
  "commands": ["./commands"]
}
```
`author` and `commands` are not Nosis fields. Move attribution into `README.md` and describe extra directories in prose. The only allowed top-level fields are `name`, `version`, `description`, `enabled`, `dependencies`, `capabilities`, `components`.

**Absolute component path**:
```json
{
  "components": {
    "skills": ["/Users/jane/skills"]
  }
}
```
Use `"skills"` (relative to the plugin root).

**Escaping the plugin root**:
```json
{
  "components": {
    "agents": ["../shared/reviewer.md"]
  }
}
```
Copy the file into the plugin instead.

**Wrong component kind**:
```json
{
  "components": {
    "commands": ["commands"]
  }
}
```
The four kinds are `skills`, `mcp`, `agents`, `hooks`.

**Shape instead of array**:
```json
{
  "components": {
    "skills": "skills"
  }
}
```
Values must be arrays: `"skills": ["skills"]`.

### Checking a manifest before installing

Validate the JSON syntax:
```bash
python3 -m json.tool ~/.nosis/plugins/my-plugin/plugin.json
```

Then compare the field names against the seven allowed fields above, or ask the plugin-validator agent to review the whole plugin directory.

## Examples

### Minimal Plugin

```json
{
  "name": "my-plugin",
  "version": "0.1.0",
  "description": "Adds a formatting Skill for this workspace",
  "components": {
    "skills": ["skills"]
  }
}
```

### Recommended Plugin

Every field Nosis accepts, with the ones you actually need filled in:

```json
{
  "name": "test-automation-suite",
  "version": "1.0.0",
  "description": "Generates test suites from coverage gaps and runs them on demand",
  "enabled": true,
  "dependencies": [],
  "capabilities": ["skills", "agents", "mcp"],
  "components": {
    "skills": ["skills"],
    "agents": ["agents/test-writer.md", "agents/coverage-analyst.md"],
    "mcp": [".mcp.json"],
    "hooks": []
  }
}
```

### Parked Plugin

```json
{
  "name": "my-plugin",
  "version": "1.0.0",
  "enabled": false
}
```

## Best Practices

### Fields

- Keep the manifest inside the seven allowed fields; every additional fact belongs in `README.md`
- List only the components the plugin actually ships
- Keep `description` to one concrete sentence
- Bump `version` when you change what the plugin ships

### Paths

- Keep component paths one level deep (`skills`, `agents`) unless a plugin really needs several groups
- Never hardcode absolute paths; they break as soon as the plugin directory moves

### Maintenance

- Re-read this reference after a Nosis upgrade - the allowed field set is a whitelist, so a manifest that loads today can stop loading if it carries fields the new version rejects
- Run the manifest through the JSON check above before installing it
