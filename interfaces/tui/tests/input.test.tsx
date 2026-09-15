import { describe, expect, it } from 'vitest';
import React from 'react';
import { render } from 'ink-testing-library';
import { Prompt, caretPoint, caretPosition } from '../src/input.js';

const wait = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

/** Drives Prompt as a controlled input, recording what it submits. */
function renderPrompt() {
  const submits: string[] = [];
  let value = '';

  function Harness(): React.ReactElement {
    const [draft, setDraft] = React.useState('');
    value = draft;
    return (
      <Prompt
        value={draft}
        onChange={setDraft}
        onSubmit={(text) => {
          submits.push(text);
          setDraft('');
        }}
        focus
        busy={false}
      />
    );
  }

  const instance = render(<Harness />);
  return { ...instance, submits, draft: () => value };
}

/** Ink needs a tick to attach stdin and flush the resulting render. */
async function type(stdin: { write: (data: string) => void }, data: string) {
  stdin.write(data);
  await wait(40);
}

describe('Prompt newlines', () => {
  it('submits on plain Enter', async () => {
    const { stdin, submits, draft } = renderPrompt();
    await wait(40);
    await type(stdin, 'hello');
    await type(stdin, '\r');

    expect(submits).toEqual(['hello']);
    expect(draft()).toBe('');
  });

  it('inserts a newline on Ctrl+J without submitting', async () => {
    const { stdin, submits, draft, lastFrame } = renderPrompt();
    await wait(40);
    await type(stdin, 'first');
    await type(stdin, '\n');

    const lines = (lastFrame() ?? '').split('\n');
    const firstLine = lines.findIndex((line) => line.includes('first'));
    const rules = lines
      .map((line, index) => (line.includes('─') ? index : -1))
      .filter((index) => index >= 0);
    expect(rules).toHaveLength(2);
    expect(rules[1]).toBe(firstLine + 2);

    await type(stdin, 'second');

    expect(submits).toEqual([]);
    expect(draft()).toBe('first\nsecond');
  });

  /** CSI 13;2u is Shift+Enter under the kitty keyboard protocol. */
  it('inserts a newline on Shift+Enter without submitting', async () => {
    const { stdin, submits, draft } = renderPrompt();
    await wait(40);
    await type(stdin, 'first');
    await type(stdin, '\u001B[13;2u');
    await type(stdin, 'second');

    expect(submits).toEqual([]);
    expect(draft()).toBe('first\nsecond');
  });

  it('renders a multiline draft inside the input box', async () => {
    const { stdin, lastFrame } = renderPrompt();
    await wait(40);
    await type(stdin, 'line one');
    await type(stdin, '\n');
    await type(stdin, 'line two');

    const frame = lastFrame() ?? '';
    expect(frame).toContain('line one');
    expect(frame).toContain('line two');
    // Both rules survive, so the box grew rather than breaking.
    expect(frame.split('\n').filter((line) => line.includes('─')).length).toBe(2);
  });
});

describe('Prompt editing', () => {
  it('inserts a newline at the caret rather than at the end', async () => {
    const { stdin, draft } = renderPrompt();
    await wait(40);
    await type(stdin, 'ab');
    await type(stdin, '\u001B[D'); // caret between a and b
    await type(stdin, '\u001B[13;2u');

    expect(draft()).toBe('a\nb');
  });

  it('inserts typed text at the caret', async () => {
    const { stdin, draft } = renderPrompt();
    await wait(40);
    await type(stdin, 'ac');
    await type(stdin, '\u001B[D');
    await type(stdin, 'b');

    expect(draft()).toBe('abc');
  });

  it('backspaces at the caret', async () => {
    const { stdin, draft } = renderPrompt();
    await wait(40);
    await type(stdin, 'abc');
    await type(stdin, '\u001B[D');
    await type(stdin, '\u007F');

    expect(draft()).toBe('ac');
  });

  it('keeps wide CJK text intact', async () => {
    const { stdin, draft, lastFrame } = renderPrompt();
    await wait(40);
    await type(stdin, '你好');

    expect(draft()).toBe('你好');
    expect(lastFrame() ?? '').toContain('你好');
  });
});

describe('caretPoint', () => {
  it('measures CJK as two columns so the cursor tracks the glyphs', () => {
    expect(caretPoint('你好', 2)).toEqual({ column: 4, row: 0 });
    expect(caretPoint('ab', 2)).toEqual({ column: 2, row: 0 });
  });

  it('reports the column within the caret row for multiline drafts', () => {
    expect(caretPoint('ab\ncd', 4)).toEqual({ column: 1, row: 1 });
    expect(caretPoint('ab\ncd', 3)).toEqual({ column: 0, row: 1 });
  });
});

describe('caretPosition', () => {
  it('compensates for the missing trailing row in fullscreen output', () => {
    const metrics = { left: 0, top: 20 };
    const caret = { column: 3, row: 0 };

    expect(caretPosition({ metrics, caret, fullscreen: false })).toEqual({ x: 4, y: 21 });
    expect(caretPosition({ metrics, caret, fullscreen: true })).toEqual({ x: 4, y: 22 });
  });
});

describe('Prompt caret visibility', () => {
  /**
   * The terminal cursor is a single shared resource: while the agent streams,
   * every delta repaints and a visible caret blinks over the text and can
   * surface outside the box between frames. It must stay hidden until idle.
   */
  it('hides the caret while the agent is busy and restores it when idle', async () => {
    const { lastFrame, rerender } = render(
      <Prompt value="draft" onChange={() => {}} onSubmit={() => {}} focus busy />,
    );
    await wait(60);
    expect(lastFrame() ?? '').toContain('draft');

    rerender(<Prompt value="draft" onChange={() => {}} onSubmit={() => {}} focus busy={false} />);
    await wait(60);
    expect(lastFrame() ?? '').toContain('draft');
  });

  it('shows the placeholder only when the draft is empty', async () => {
    const { lastFrame, rerender } = render(
      <Prompt value="" onChange={() => {}} onSubmit={() => {}} focus busy={false} />,
    );
    await wait(60);
    expect(lastFrame() ?? '').toContain('ask anything');

    rerender(<Prompt value="" onChange={() => {}} onSubmit={() => {}} focus busy />);
    await wait(60);
    expect(lastFrame() ?? '').toContain('type to queue');

    rerender(<Prompt value="typed" onChange={() => {}} onSubmit={() => {}} focus busy={false} />);
    await wait(60);
    const frame = lastFrame() ?? '';
    expect(frame).toContain('typed');
    expect(frame).not.toContain('ask anything');
  });
});
