import React from 'react';
import { Box, Static, Text, useBoxMetrics } from 'ink';
import type { DOMElement } from 'ink';
import Spinner from 'ink-spinner';
import { formatArguments } from '@nosis/protocol';
import type { PermissionPreset, SessionSummary } from '@nosis/protocol';
import type { ApprovalChoice, Entry, State, UserQuestionState } from './state.js';

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

  const index = `[${entry.index}/${entry.count}] `;

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

type TextEntry = Extract<Entry, { kind: 'assistant' | 'reasoning' }>;

type TranscriptItem =
  | { id: string; kind: 'entry'; entry: Entry }
  | {
      id: string;
      kind: 'text-line';
      entryKind: TextEntry['kind'];
      text: string;
      first: boolean;
      timestamp?: string;
    };

function TextLineView({
  item,
}: {
  item: Extract<TranscriptItem, { kind: 'text-line' }>;
}): React.ReactElement {
  if (item.entryKind === 'reasoning') {
    return (
      <Box>
        <Text dimColor italic>
          {item.text || ' '}
        </Text>
      </Box>
    );
  }

  return (
    <Box marginTop={item.first ? 1 : 0}>
      <Text>{item.text || ' '}</Text>
      {item.timestamp ? <Text dimColor> {new Date(item.timestamp).toLocaleString()}</Text> : null}
    </Box>
  );
}

function TranscriptItemView({ item }: { item: TranscriptItem }): React.ReactElement {
  return item.kind === 'entry' ? <EntryView entry={item.entry} /> : <TextLineView item={item} />;
}

function textItems(entry: TextEntry): {
  settled: TranscriptItem[];
  live: TranscriptItem | null;
} {
  const lines = entry.text.split('\n');
  const settledCount = entry.settled ? lines.length : lines.length - 1;
  const settled = lines.slice(0, settledCount).map(
    (text, index): TranscriptItem => ({
      id: `${entry.id}:line:${index}`,
      kind: 'text-line',
      entryKind: entry.kind,
      text,
      first: index === 0,
      ...(entry.kind === 'assistant' &&
      entry.settled &&
      index === settledCount - 1 &&
      entry.timestamp_utc
        ? { timestamp: entry.timestamp_utc }
        : {}),
    }),
  );

  if (entry.settled || lines.at(-1) === '') return { settled, live: null };
  return {
    settled,
    live: {
      id: `${entry.id}:tail`,
      kind: 'text-line',
      entryKind: entry.kind,
      text: lines.at(-1)!,
      first: settledCount === 0,
    },
  };
}

export function Transcript({ state }: { state: State }): React.ReactElement {
  const items: TranscriptItem[] = [];
  let committableCount = 0;
  let reachedLiveEntry = false;

  for (const entry of state.entries) {
    if (reachedLiveEntry) {
      items.push({ id: entry.id, kind: 'entry', entry });
      continue;
    }

    if (entry.kind === 'assistant' || entry.kind === 'reasoning') {
      const text = textItems(entry);
      items.push(...text.settled);
      committableCount += text.settled.length;
      if (text.live) {
        items.push(text.live);
        reachedLiveEntry = true;
      }
      continue;
    }

    if (entry.kind === 'tool' && entry.state === 'running') {
      items.push({ id: entry.id, kind: 'entry', entry });
      reachedLiveEntry = true;
      continue;
    }

    items.push({ id: entry.id, kind: 'entry', entry });
    committableCount += 1;
  }

  const [committedCount, setCommittedCount] = React.useState(0);
  const viewportRef = React.useRef<DOMElement>(null);
  const contentRef = React.useRef<DOMElement>(null);
  const viewport = useBoxMetrics(viewportRef);
  const content = useBoxMetrics(contentRef);
  const overflowing =
    viewport.hasMeasured && content.hasMeasured && content.height > viewport.height;

  React.useEffect(() => {
    // Committed in one step rather than one item per render: a resumed
    // conversation arrives as a whole transcript, and a per-item cascade
    // walks into React's nested-update limit. Items only become committable
    // once they are settled, so no live entry is written early.
    if (overflowing && committedCount < committableCount) {
      setCommittedCount(committableCount);
    }
  }, [committableCount, committedCount, overflowing]);

  React.useEffect(() => {
    // A new conversation replaces the transcript. Following it keeps the
    // cursor on settled items: a stale one would commit the live turn to
    // Static, which writes it once and never updates it again.
    if (committedCount > committableCount) setCommittedCount(committableCount);
  }, [committableCount, committedCount]);

  const committed = items.slice(0, committedCount);
  const visible = items.slice(committedCount);

  return (
    <>
      <Static items={committed}>
        {(item) => <TranscriptItemView key={item.id} item={item} />}
      </Static>
      <Box
        ref={viewportRef}
        flexGrow={1}
        flexShrink={1}
        flexDirection="column"
        justifyContent={overflowing ? 'flex-end' : 'flex-start'}
        overflowY="hidden"
      >
        <Box ref={contentRef} flexShrink={0} flexDirection="column">
          {visible.map((item) => (
            <TranscriptItemView key={item.id} item={item} />
          ))}
        </Box>
      </Box>
    </>
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
  const model = `${state.model}${
    state.usage?.total_tokens ? ` · ${state.usage.total_tokens} tokens` : ''
  }${state.sessionId ? ` · ${state.sessionId.slice(0, 8)}` : ''}${
    state.contextWindow
      ? ` · Context window: ${state.contextWindow.input_tokens}/${state.contextWindow.max_input_tokens}`
      : ''
  }`;

  return (
    <Box flexShrink={0} paddingX={1}>
      {busy ? (
        <Box flexShrink={0}>
          <Text color="yellow">
            <Spinner type="dots" />
          </Text>
        </Box>
      ) : null}
      <Text dimColor wrap="truncate-end">
        {busy
          ? `${state.status === 'cancelling' ? ' cancelling…' : ' working'} ${elapsed.toFixed(
              1,
            )}s${state.pendingSteers ? ` · ${state.pendingSteers} steer pending` : ''} · esc to cancel · ${model}`
          : model}
      </Text>
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

export function PermissionPrompt({
  selected,
}: {
  selected: PermissionPreset;
}): React.ReactElement {
  return (
    <Box
      marginTop={1}
      flexDirection="column"
      borderStyle="round"
      borderColor="cyan"
      paddingX={1}
    >
      <Text color="cyan" bold>Permissions</Text>
      <Box marginTop={1} flexDirection="column">
        <Text color={selected === 'ask_for_approval' ? 'cyan' : undefined} dimColor={selected !== 'ask_for_approval'} bold={selected === 'ask_for_approval'}>
          {selected === 'ask_for_approval' ? '❯ ' : '  '}Ask for approval
        </Text>
        <Text dimColor>    Ask before Shell and configured MCP tool calls.</Text>
        <Text color={selected === 'full_access' ? 'cyan' : undefined} dimColor={selected !== 'full_access'} bold={selected === 'full_access'}>
          {selected === 'full_access' ? '❯ ' : '  '}Full Access
        </Text>
        <Text dimColor>    Skip approval and execute directly on the current host.</Text>
      </Box>
      <Text dimColor>↑/↓ select · enter confirm · esc close</Text>
    </Box>
  );
}

export function SessionPicker({
  sessions,
  selectedIndex,
}: {
  sessions: SessionSummary[];
  selectedIndex: number;
}): React.ReactElement {
  return (
    <Box
      marginTop={1}
      flexDirection="column"
      borderStyle="round"
      borderColor="cyan"
      paddingX={1}
    >
      <Text color="cyan" bold>Sessions</Text>
      <Box marginTop={1} flexDirection="column">
        {sessions.length === 0 ? (
          <Text dimColor>Loading…</Text>
        ) : (
          sessions.map((session, index) => {
            const selected = index === selectedIndex;
            return (
              <Box key={session.session_id}>
                <Text
                  color={selected ? 'cyan' : undefined}
                  dimColor={!selected}
                  bold={selected}
                  wrap="truncate-end"
                >
                  {selected ? '❯ ' : '  '}
                  {session.title}
                </Text>
              </Box>
            );
          })
        )}
      </Box>
      <Text dimColor>↑/↓ select · enter switch · esc close</Text>
    </Box>
  );
}

export function UserQuestionPrompt({
  question,
}: {
  question: UserQuestionState;
}): React.ReactElement {
  const choices = [
    ...question.options,
    ...(question.allowFreeText
      ? [{ id: '__free_text__', label: '其他答案', description: '输入自定义答案' }]
      : []),
  ];
  return (
    <Box
      marginTop={1}
      flexDirection="column"
      borderStyle="round"
      borderColor="cyan"
      paddingX={1}
    >
      <Text color="cyan" bold>{question.question}</Text>
      <Box marginTop={1} flexDirection="column">
        {choices.map((option, index) => {
          const selected = question.selectedIndex === index;
          return (
            <Box key={option.id} flexDirection="column">
              <Text color={selected ? 'cyan' : undefined} dimColor={!selected} bold={selected}>
                {selected ? '❯ ' : '  '}{option.label}
                {'recommended' in option && option.recommended ? '  推荐' : ''}
              </Text>
              {option.description ? <Text dimColor>    {option.description}</Text> : null}
            </Box>
          );
        })}
      </Box>
      <Text dimColor>↑/↓ select · enter confirm · esc cancel</Text>
    </Box>
  );
}
