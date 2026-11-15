#!/usr/bin/env node
import { render } from 'ink';
import { App } from './app.js';

const workspace = process.argv[2];
const python = process.env.NOSIS_PYTHON;
if (!python || !workspace) {
  process.stderr.write(
    'Run the terminal UI through the "nosis" command.\n',
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
    workspace={workspace}
    configDirectory={process.env.NOSIS_CONFIG_DIRECTORY}
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
