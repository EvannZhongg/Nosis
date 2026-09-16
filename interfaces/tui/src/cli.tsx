#!/usr/bin/env node
import { render } from 'ink';
import { homedir } from 'node:os';
import { join, resolve } from 'node:path';
import { App } from './app.js';

type Options = {
  workspace: string;
  providerConfigPath: string;
  agentConfigPath: string;
};

const USAGE = `Usage: nosis [options]

Options:
  --workspace <path>      Workspace directory (default: current directory)
  --config <path>         Provider configuration file
  --agent-config <path>   Agent behaviour configuration file
  -h, --help              Show this message
`;

function parseArguments(argv: string[]): Options {
  const configDirectory = join(homedir(), '.nosis');
  const options: Options = {
    workspace: process.cwd(),
    providerConfigPath: join(configDirectory, 'provider_config.json'),
    agentConfigPath: join(configDirectory, 'agent_config.json'),
  };

  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (argument === '--help' || argument === '-h') {
      process.stdout.write(USAGE);
      process.exit(0);
    }
    const value = argv[index + 1];
    if (value === undefined) {
      process.stderr.write(`Missing value for ${argument}\n`);
      process.exit(2);
    }
    index += 1;
    switch (argument) {
      case '--workspace':
        options.workspace = resolve(value);
        break;
      case '--config':
        options.providerConfigPath = resolve(value);
        break;
      case '--agent-config':
        options.agentConfigPath = resolve(value);
        break;
      default:
        process.stderr.write(`Unknown option: ${argument}\n${USAGE}`);
        process.exit(2);
    }
  }

  return options;
}

const options = parseArguments(process.argv.slice(2));

const python = process.env.NOSIS_PYTHON;
if (!python) {
  process.stderr.write(
    'NOSIS_PYTHON is not set. Run Nosis through the "nosis" command.\n',
  );
  process.exit(2);
}

/**
 * Whether to ask Ink for the kitty keyboard protocol, which is what makes
 * Shift+Enter distinguishable from Enter.
 *
 * Ink's own 'auto' mode probes the terminal with `CSI ? u` and waits 200ms for
 * a reply. Terminals that answer late — Apple Terminal does — answer after Ink
 * stopped listening, and the reply is then typed into the UI as `[?0u`. So the
 * protocol is only requested where support is known from the environment, and
 * never probed.
 */
function supportsKittyKeyboard(): boolean {
  const env = process.env;
  if (env['KITTY_WINDOW_ID']) return true;
  if (env['GHOSTTY_RESOURCES_DIR'] || env['TERM'] === 'xterm-ghostty') return true;
  if (env['WEZTERM_PANE'] !== undefined) return true;
  if (env['TERM_PROGRAM'] === 'WezTerm' || env['TERM_PROGRAM'] === 'ghostty') return true;
  return false;
}

render(
  <App
    python={python}
    workspace={options.workspace}
    providerConfigPath={options.providerConfigPath}
    agentConfigPath={options.agentConfigPath}
  />,
  {
    // Ink would otherwise unmount on Ctrl+C; we use it to cancel a turn.
    exitOnCtrlC: false,
    patchConsole: false,
    // Lets these terminals report Shift+Enter as a distinct key so it can
    // insert a newline. Elsewhere Ctrl+J is the portable way to add one.
    ...(supportsKittyKeyboard()
      ? { kittyKeyboard: { mode: 'enabled' as const, flags: ['disambiguateEscapeCodes' as const] } }
      : {}),
  },
);
