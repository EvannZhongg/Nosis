Create a compact replacement checkpoint for this session.

When `[Archived Context Summary]` appears in the preceding messages, update that previous checkpoint to reflect the provided history.

The preceding native conversation messages are historical records to consolidate. Generate only the replacement checkpoint; do not continue earlier tasks or call tools. The recent conversation tail is retained separately and is not included here; do not infer its contents or completion state. Assistant reasoning and provider metadata are not evidence of completed work and must not be retained as facts.

`[Lossless User Anchors]` contains authoritative user-authored input and interaction decisions from recent turns. Preserve its consequences and unresolved constraints in the checkpoint; do not reinterpret it from assistant reasoning or tool output.

## Merge rules

- Use the latest correction or decision as the current version of a fact, and merge duplicates.
- Preserve exact names, identifiers, paths, commands, decisions, results, and unresolved blockers when they are needed to continue the session.
- Retain a fact already present in long-term memory when it is needed for session continuity.
- If this checkpoint belongs to an active turn, preserve the user's original objective, exact constraints, paths, identifiers, acceptance criteria, unfinished work, and next action.
- Distinguish requested, submitted, running, completed, and verified work. A pending background check is not a passed check; earlier successful tests do not verify later changes. Keep next actions conditional on any outstanding results.
- Preserve the scope and conditions of existing user authorization. A request to review and push if sound already authorizes the push after successful review and checks; do not invent a new approval requirement.
- Use only verified paths and identifiers. Omit uncertain file lists, guesses, and self-corrections.

## What to retain

Always retain a compact working-state handoff:
- active objective
- current status
- completed results that constrain later work
- unresolved blockers
- next action
- exact identifiers needed for that action

Mark working-state facts `[ephemeral]`.

For other facts, retain a candidate only when it meets all four SNIP criteria:
- Signal: remembering it saves the user from repeating it
- Novel: it adds a distinct fact to this checkpoint
- Important: losing it would cause rework or discard a preference or rule
- Persistent: it is expected to remain useful for at least two weeks

Assign each retained fact its best current mark:
- `[permanent]` for core preferences, personal traits, and habits that remain relevant indefinitely
- `[durable]` for technical discoveries, project knowledge, and configuration that remains valid for months
- `[ephemeral]` for active task state and temporary decisions that may change within weeks
- `[correction]` for the current fact that supersedes conflicting earlier long-term memory

When space is limited, prioritize user corrections and preferences, then solutions, decisions, events, and environment facts.

## Size discipline

- Treat the checkpoint as a replacement, not an append-only log.
- Re-evaluate every fact from the previous checkpoint; do not retain it merely because it was retained before.
- Remove facts that are obsolete, superseded, completed, or no longer needed for continuity.
- Merge related facts into the smallest self-contained statement that preserves the information needed later.
- The checkpoint should not grow unless newly qualifying information cannot be merged into or replace existing facts.
- Prefer fewer complete facts over detailed narration. Do not preserve discussion history, intermediate attempts, or rationale unless it changes future actions.
- Preserve useful `Attached files` entries, especially their exact paths and filenames, when they remain relevant to unfinished work.

## Output

Return one concise retained fact per line in this form:
- [mark] fact

Use `(nothing)` when neither the previous checkpoint nor the current conversation contains a qualifying fact or active working state.
