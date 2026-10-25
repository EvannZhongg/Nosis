export type Command = {
  name: string;
  description: string;
};

/**
 * Commands the TUI handles itself instead of sending them to the agent.
 *
 * This list is what the input offers, so an entry without a branch in the
 * submission path would be a command that does nothing.
 */
export const COMMANDS: Command[] = [
  {
    name: '/permissions',
    description: 'Choose the current permission mode',
  },
  {
    name: '/sessions',
    description: 'Switch to another conversation in this workspace',
  },
  {
    name: '/workspaces',
    description: 'Start a new conversation in an existing workspace',
  },
  {
    name: '/model',
    description: 'Switch this session to another configured provider',
  },
  {
    name: '/provider',
    description: 'Inspect provider, skill, plugin and MCP configuration',
  },
  {
    name: '/refresh',
    description: 'Reload this session’s runtime, skills, plugins and MCP connections',
  },
];

export function parseCommand(value: string): { command: Command; argument: string } | undefined {
  const [name, ...rest] = value.trim().split(/\s+/);
  const command = COMMANDS.find((item) => item.name === name);
  return command ? { command, argument: rest.join(' ') } : undefined;
}

/**
 * Commands from `commands` that the menu offers for the current draft, in
 * display order.
 *
 * Empty unless the draft is a command name being typed: a leading slash opens
 * the list and the first whitespace closes it, because whatever follows the
 * name is an argument rather than part of it.
 */
export function matchCommands(value: string, commands: Command[]): Command[] {
  if (!value.startsWith('/')) return [];
  const query = value.slice(1);
  if (/\s/.test(query)) return [];
  return commands.filter((command) => command.name.slice(1).startsWith(query));
}
