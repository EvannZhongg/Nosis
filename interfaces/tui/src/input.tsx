import React from 'react';
import { Box, Text, useBoxMetrics, useCursor, useInput } from 'ink';
import type { DOMElement } from 'ink';
import stringWidth from 'string-width';
import { matchCommands } from './commands.js';
import type { Command } from './commands.js';

export type PromptProps = {
  value: string;
  onChange: (value: string) => void;
  onSubmit: (value: string) => void;
  /** False while a prompt elsewhere owns the keyboard. */
  focus: boolean;
  busy: boolean;
  /** True when the prompt grows upward from a fixed bottom edge. */
  docked?: boolean;
  placeholder?: string;
  /** Slash commands offered above the box while their name is being typed. */
  commands?: Command[];
};

/**
 * Where the caret sits inside the draft, in terminal cells.
 *
 * Wide glyphs (CJK, emoji) occupy two columns, so the column is a display
 * width rather than a character count — otherwise the cursor drifts left of
 * the text the further into a CJK line it goes.
 */
export function caretPoint(value: string, offset: number): { column: number; row: number } {
  const before = value.slice(0, offset).split('\n');
  return {
    column: stringWidth(before[before.length - 1]!),
    row: before.length - 1,
  };
}

export function caretPosition({
  metrics,
  caret,
  lineCount,
  docked,
}: {
  metrics: { left: number; top: number; height: number };
  caret: { column: number; row: number };
  lineCount: number;
  docked: boolean;
}): { x: number; y: number } {
  if (!docked) {
    return { x: metrics.left + 1 + caret.column, y: metrics.top + 1 + caret.row };
  }

  const rowsBelowCaret = lineCount - caret.row - 1;
  return {
    x: metrics.left + 1 + caret.column,
    // A docked prompt expands upward. Its previous and next measurements have
    // the same bottom edge, so this stays correct while useBoxMetrics catches
    // up after Ctrl+J inserts a new row.
    y: metrics.top + metrics.height - 2 - rowsBelowCaret,
  };
}

/**
 * The commands a draft can still become, listed directly above the input box.
 *
 * The rows are sibling nodes of the box rather than a wrapper around it: the
 * caret is placed from the box's own measured position, which stays absolute
 * only while the box's parent is the frame.
 */
function CommandMenu({
  commands,
  highlightedIndex,
}: {
  commands: Command[];
  highlightedIndex: number;
}): React.ReactElement {
  return (
    <Box flexShrink={0} flexDirection="column" marginTop={1} paddingX={1}>
      {commands.map((command, index) => {
        const highlighted = index === highlightedIndex;
        return (
          <Box key={command.name}>
            <Text
              color={highlighted ? 'cyan' : undefined}
              dimColor={!highlighted}
              bold={highlighted}
            >
              {highlighted ? '❯ ' : '  '}
              {command.name}
            </Text>
            <Text dimColor>  {command.description}</Text>
          </Box>
        );
      })}
    </Box>
  );
}

/**
 * Stays mounted while the agent works so the caller can submit steering.
 *
 * Top and bottom rules mark the input area; the dimmed border distinguishes
 * steering the active turn from starting a new one.
 *
 * The draft is edited here because the real terminal cursor has to sit on
 * the caret: an IME draws its preedit ("nihao"
 * on the way to "你好") wherever that cursor is, and Ink parks it below the
 * last rendered line unless told otherwise. Owning the caret offset is what
 * makes it placeable, and it also lets Enter insert a newline mid-draft.
 *
 * Newlines: Ctrl+J works in every terminal because it arrives as a literal
 * "\n". Shift+Enter is indistinguishable from Enter unless the terminal
 * speaks the kitty keyboard protocol (enabled in cli.tsx) and reports the
 * modifier.
 *
 * A slash at the start of the draft lists the commands in `commands` above the
 * box; ↑/↓ move the highlight and plain Enter runs it.
 */
export function Prompt({
  value,
  onChange,
  onSubmit,
  focus,
  busy,
  docked = false,
  placeholder,
  commands,
}: PromptProps): React.ReactElement {
  const boxRef = React.useRef<DOMElement>(null);
  const metrics = useBoxMetrics(boxRef);
  const { setCursorPosition } = useCursor();
  const [offset, setOffset] = React.useState(value.length);
  const [highlighted, setHighlighted] = React.useState(0);

  // Only the prompt that owns the keyboard offers commands: elsewhere the
  // arrow keys belong to the prompt that took the keyboard away.
  const suggestions = commands && focus ? matchCommands(value, commands) : [];
  const highlightedIndex =
    suggestions.length === 0 ? 0 : Math.min(highlighted, suggestions.length - 1);

  // Every edit changes which names match, so the highlight returns to the top.
  React.useEffect(() => setHighlighted(0), [value]);

  // The parent clears the draft on submit, so the caret follows the value.
  const caretOffset = Math.min(offset, value.length);

  const edit = (nextValue: string, nextOffset: number): void => {
    setOffset(nextOffset);
    if (nextValue !== value) onChange(nextValue);
  };

  useInput(
    (input, key) => {
      // Ctrl+C, Ctrl+D and Esc belong to the app-level handlers.
      if (key.ctrl || key.escape) return;

      if (suggestions.length > 0) {
        if (key.upArrow) {
          setHighlighted((current) => (current - 1 + suggestions.length) % suggestions.length);
          return;
        }
        if (key.downArrow) {
          setHighlighted((current) => (current + 1) % suggestions.length);
          return;
        }
        // Plain Enter runs the highlighted command; Shift/Alt+Enter still
        // inserts a line below.
        if (key.return && !key.shift && !key.meta) {
          onSubmit(suggestions[highlightedIndex]!.name);
          return;
        }
      }

      if (key.return) {
        // Shift+Enter (kitty protocol only) and Alt+Enter add a line instead
        // of sending. Plain Enter sends.
        if (key.shift || key.meta) {
          edit(
            value.slice(0, caretOffset) + '\n' + value.slice(caretOffset),
            caretOffset + 1,
          );
        } else {
          onSubmit(value);
        }
        return;
      }

      if (key.leftArrow) {
        setOffset(Math.max(0, caretOffset - 1));
        return;
      }
      if (key.rightArrow) {
        setOffset(Math.min(value.length, caretOffset + 1));
        return;
      }
      if (key.backspace || key.delete) {
        if (caretOffset > 0) {
          edit(value.slice(0, caretOffset - 1) + value.slice(caretOffset), caretOffset - 1);
        }
        return;
      }
      // These keys do not edit the current draft.
      if (key.upArrow || key.downArrow || key.tab) return;
      if (input === '') return;

      // Printable text, including a multi-character paste and the literal
      // "\n" that Ctrl+J delivers.
      edit(
        value.slice(0, caretOffset) + input + value.slice(caretOffset),
        caretOffset + input.length,
      );
    },
    { isActive: focus },
  );

  // Put the terminal's own cursor on the caret so IME preedit and the blinking
  // block appear inside the box. Metrics are relative to the parent column,
  // which is the frame origin, and the first row inside the box clears the top
  // rule and the horizontal padding.
  //
  // Set during render, not in an effect: useCursor publishes the position from
  // an insertion effect, which runs before layout and passive effects, so a
  // position stored later would only be applied on the following commit.
  //
  const caret = caretPoint(value, caretOffset);
  const showCaret = focus && metrics.hasMeasured;
  setCursorPosition(
    showCaret
      ? caretPosition({
          metrics,
          caret,
          lineCount: value.split('\n').length,
          docked,
        })
      : undefined,
  );

  // Ink collapses an empty final line in a Text node. Keep that row in the
  // layout so Ctrl+J moves the terminal cursor immediately, before another
  // character is typed.
  const renderedValue = value.endsWith('\n') ? `${value} ` : value;

  return (
    <>
      {suggestions.length > 0 ? (
        <CommandMenu commands={suggestions} highlightedIndex={highlightedIndex} />
      ) : null}

      <Box
        ref={boxRef}
        width="100%"
        flexShrink={0}
        // The menu carries the blank line that separates the input area from
        // the transcript while it is open.
        marginTop={suggestions.length > 0 ? 0 : 1}
        borderStyle="single"
        borderLeft={false}
        borderRight={false}
        borderColor="cyan"
        borderDimColor={busy}
        paddingX={1}
      >
        {value === '' ? (
          <Text dimColor>{placeholder ?? (busy ? 'steer the current turn…' : 'ask anything…')}</Text>
        ) : (
          <Text>{renderedValue}</Text>
        )}
      </Box>
    </>
  );
}
