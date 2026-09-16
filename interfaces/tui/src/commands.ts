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
    description: 'Choose between Ask for approval and Full Access',
  },
  {
    name: '/sessions',
    description: 'Switch to another conversation in this workspace',
  },
];

export function findCommand(name: string): Command | undefined {
  return COMMANDS.find((command) => command.name === name);
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
