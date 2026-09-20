"""The size budget every tool result shares.

A tool result reaches the model wrapped in the ``{"ok": ..., "output":
...}`` envelope that :meth:`ToolResult.to_content` builds, and the
Runtime spills anything longer than :data:`MAX_TOOL_RESULT_CHARS` to a
Session artifact.  A tool that bounds its own output against the raw
payload therefore measures the wrong thing: JSON escaping can only make
the payload longer, so the envelope crosses the threshold while the
payload still looks small enough.

The helpers here let a tool size its output the way the Runtime will,
which keeps a result the tool intends to return inline actually inline.
"""

import json


MAX_TOOL_RESULT_CHARS = 32 * 1024


def envelope_chars(output: object) -> int:
    """Return the length of *output* once wrapped in the result envelope."""
    return len(
        json.dumps({"ok": True, "output": output}, ensure_ascii=False)
    )


def output_fits(output: object) -> bool:
    """Whether *output* stays inline rather than spilling to an artifact."""
    return envelope_chars(output) <= MAX_TOOL_RESULT_CHARS


def escaped_length(text: str) -> int:
    """Return how many characters *text* occupies inside a JSON string.

    Escaping is additive over concatenation -- no escape sequence spans a
    character boundary -- so a caller may accumulate this per character
    and know the running total exactly, without re-serializing a growing
    candidate on every step.
    """
    return len(json.dumps(text, ensure_ascii=False)) - 2
