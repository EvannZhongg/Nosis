import React from 'react';
import { Box, Static, Text } from 'ink';
import Spinner from 'ink-spinner';
import { formatArguments } from '@nosis/protocol';
import type { ApprovalChoice, Entry, State } from './state.js';

function EntryView({ entry }: { entry: Entry }): React.ReactElement {
  if (entry.kind === 'user') {
    return (
      <Box marginTop={1}>
        <Text color="cyan" bold>
          {'> '}
        </Text>
        <Text>{entry.text}</Text>
      </Box>
    );
  }

  if (entry.kind === 'assistant') {
    return (
      <Box marginTop={1}>
        <Text>{entry.text}</Text>
        {entry.timestamp_utc ? <Text dimColor>  {new Date(entry.timestamp_utc).toLocaleString()}</Text> : null}
      </Box>
    );
  }

  if (entry.kind === 'reasoning') {
    return (
      <Box>
        <Text dimColor italic>
          {entry.text}
        </Text>
      </Box>
    );
  }

  if (entry.kind === 'notice') {
    return (
      <Box marginTop={1}>
        <Text color={entry.level === 'error' ? 'red' : 'yellow'}>{entry.text}</Text>
      </Box>
    );
  }

  const args = formatArguments(entry.args);
  const marker =
    entry.state === 'running' ? (
      <Text color="yellow">
        <Spinner type="dots" />
      </Text>
    ) : entry.state === 'ok' ? (
      <Text color="green">✓</Text>
    ) : (
      <Text color="red">✗</Text>
    );

  // Single calls pad to the width of an index tag so markers line up.
  const index = (entry.count > 1 ? `[${entry.index}/${entry.count}] ` : '').padEnd(6);

  return (
    <Box>
      <Box flexShrink={0}>
        <Text dimColor>{index}</Text>
        {marker}
        <Text> {entry.name}</Text>
      </Box>
      {args ? (
        <Text dimColor wrap="truncate-end">
          {' '}
          {args}
        </Text>
      ) : null}
      {entry.state === 'error' && entry.error ? (
        <Text color="red" wrap="truncate-end">
          {' '}
          — {entry.error.message}
        </Text>
      ) : null}
    </Box>
  );
}

/**
 * Settled entries render inside <Static> so Ink writes them once instead
 * of repainting the whole transcript on every frame. Everything from the
 * earliest live entry onward stays dynamic: concurrent tools can complete
 * out of order, so a running call may precede an already completed call.
 */
export function Transcript({ state }: { state: State }): React.ReactElement {
  const isLive = (entry: Entry): boolean =>
    ((entry.kind === 'assistant' || entry.kind === 'reasoning') &&
      !entry.settled) ||
    (entry.kind === 'tool' && entry.state === 'running');

  const firstLive = state.entries.findIndex(isLive);
  const split = firstLive === -1 ? state.entries.length : firstLive;

  const settled = state.entries.slice(0, split);
  const live = state.entries.slice(split);

  return (
    <Box flexShrink={0} flexDirection="column">
      <Static items={settled}>
        {(entry) => <EntryView key={entry.id} entry={entry} />}
      </Static>
      {live.map((entry) => (
        <EntryView key={entry.id} entry={entry} />
      ))}
    </Box>
  );
}

export function StatusBar({ state, elapsed }: { state: State; elapsed: number }) {
  if (state.status === 'starting') {
    return (
      <Box flexShrink={0} paddingX={1}>
        <Box flexShrink={0}>
          <Text color="yellow">
            <Spinner type="dots" />
          </Text>
        </Box>
        <Text dimColor wrap="truncate-end">
          {state.mcpStatus ? ` ${state.mcpStatus}` : ' starting agent…'}
        </Text>
      </Box>
    );
  }

  const busy =
    state.status === 'streaming' ||
    state.status === 'running' ||
    state.status === 'cancelling';

  return (
    <Box flexShrink={0} flexDirection="column" paddingX={1}>
      {busy ? (
        <Box>
          <Text color="yellow">
            <Spinner type="dots" />
          </Text>
          <Text dimColor>
            {state.status === 'cancelling' ? ' cancelling…' : ' working'} {elapsed.toFixed(1)}s
            {' · esc to cancel'}
          </Text>
        </Box>
      ) : null}
      <Box>
        <Text dimColor wrap="truncate-end">
          {state.model}
          {state.usage?.total_tokens ? ` · ${state.usage.total_tokens} tokens` : ''}
          {state.sessionId ? ` · ${state.sessionId.slice(0, 8)}` : ''}
        </Text>
      </Box>
    </Box>
  );
}

function Option({
  label,
  selected,
  color,
}: {
  label: string;
  selected: boolean;
  color: string;
}): React.ReactElement {
  return (
    <Box marginRight={2}>
      <Text color={selected ? color : undefined} dimColor={!selected} bold={selected}>
        {selected ? `❯ ${label}` : `  ${label}`}
      </Text>
    </Box>
  );
}

export function ApprovalPrompt({
  command,
  choice,
}: {
  command: string;
  choice: ApprovalChoice;
}): React.ReactElement {
  return (
    <Box
      marginTop={1}
      flexDirection="column"
      borderStyle="round"
      borderColor="yellow"
      paddingX={1}
    >
      <Text color="yellow" bold>
        Shell command requires approval
      </Text>
      <Box marginTop={1}>
        <Text>{command}</Text>
      </Box>
      <Box marginTop={1}>
        <Option label="Allow" selected={choice === 'allow'} color="green" />
        <Option label="Deny" selected={choice === 'deny'} color="red" />
      </Box>
      <Text dimColor>←/→ select · enter confirm · esc deny</Text>
    </Box>
  );
}
