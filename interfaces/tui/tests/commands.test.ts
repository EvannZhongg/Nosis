import { describe, expect, it } from 'vitest';
import { COMMANDS, findCommand, matchCommands, parseCommand } from '../src/commands.js';

const names = (value: string): string[] =>
  matchCommands(value, COMMANDS).map((command) => command.name);

describe('matchCommands', () => {
  it('offers every command for a bare slash', () => {
    expect(names('/')).toEqual(COMMANDS.map((command) => command.name));
  });

  it('narrows by the name being typed', () => {
    expect(names('/perm')).toEqual(['/permissions']);
  });

  it('closes once the name is followed by an argument', () => {
    expect(names('/permissions ')).toEqual([]);
    expect(names('/permissions\n')).toEqual([]);
  });

  it('ignores drafts that are not a command name', () => {
    expect(names('ask about /permissions')).toEqual([]);
    expect(names('/unknown')).toEqual([]);
  });
});

describe('parseCommand', () => {
  it('returns the command and its argument', () => {
    expect(parseCommand('/model openai-main')).toEqual({
      command: expect.objectContaining({ name: '/model' }),
      argument: 'openai-main',
    });
  });
});

describe('findCommand', () => {
  it('resolves the exact name a submission dispatches on', () => {
    expect(findCommand('/permissions')?.name).toBe('/permissions');
    expect(findCommand('/permission')).toBeUndefined();
  });
});
