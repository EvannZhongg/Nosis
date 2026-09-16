import React, { useEffect, useMemo, useReducer, useRef, useState } from 'react';
import { Box, Text, useApp, useInput, useWindowSize } from 'ink';
import type { PermissionPreset } from '@nosis/protocol';
import { BridgeClient } from './bridge.js';
import { COMMANDS, findCommand } from './commands.js';
import { Prompt } from './input.js';
import { ApprovalPrompt, PermissionPrompt, StatusBar, Transcript, UserQuestionPrompt } from './renderer.js';
import { initialState, reducer } from './state.js';

export type AppProps = {
  python: string;
  workspace: string;
  sessionId: string | null;
  providerConfigPath: string;
  agentConfigPath: string;
};

/** Keep Windows paths unambiguous on the newline-delimited JSON channel. */
function protocolPath(path: string): string {
  return path.replaceAll('\\', '/');
}

export function App(props: AppProps): React.ReactElement {
  const { exit } = useApp();
  const { rows } = useWindowSize();
  const [state, dispatch] = useReducer(reducer, initialState);
  const [draft, setDraft] = useState('');
  const [questionDraft, setQuestionDraft] = useState('');
  const [permissionChoice, setPermissionChoice] = useState<PermissionPreset | null>(null);
  const [elapsed, setElapsed] = useState(0);
  const turnCounter = useRef(0);
  const steerCounter = useRef(0);
  const startedAt = useRef<number | null>(null);

  const bridge = useMemo(
    () =>
      new BridgeClient({
        python: props.python,
        cwd: props.workspace,
        onMessage: (message) => dispatch({ type: 'message', message }),
        onExit: (code) => {
          if (code === 0) exit();
          else dispatch({ type: 'exited', code });
        },
        onProtocolError: (error) =>
          dispatch({
            type: 'message',
            message: {
              type: 'fatal',
              error: { type: 'ProtocolError', message: error.message },
            },
          }),
      }),
    // Created once for the process lifetime.
    [],
  );

  useEffect(() => {
    bridge.send({
      type: 'start',
      workspace: protocolPath(props.workspace),
      session_id: props.sessionId,
      provider_config_path: protocolPath(props.providerConfigPath),
      agent_config_path: protocolPath(props.agentConfigPath),
    });
    return () => bridge.shutdown();
  }, [bridge]);

  const busy =
    state.status === 'streaming' ||
    state.status === 'running' ||
    state.status === 'cancelling';

  useEffect(() => {
    if (!busy) {
      startedAt.current = null;
      setElapsed(0);
      return;
    }
    if (startedAt.current === null) startedAt.current = Date.now();
    const timer = setInterval(() => {
      if (startedAt.current !== null) {
        setElapsed((Date.now() - startedAt.current) / 1000);
      }
    }, 100);
    return () => clearInterval(timer);
  }, [busy]);

  const answerApproval = (approved: boolean): void => {
    if (!state.approval) return;
    bridge.send({
      type: 'approval_response',
      request_id: state.approval.requestId,
      approved,
    });
    dispatch({ type: 'approval_resolved' });
  };

  const answerQuestion = (answer: { option_id: string } | { text: string }): void => {
    if (!state.question) return;
    bridge.send({
      type: 'user_question_response',
      request_id: state.question.requestId,
      ...answer,
    });
    setQuestionDraft('');
    dispatch({ type: 'question_resolved' });
  };

  const questionChoiceCount = state.question
    ? state.question.options.length + (state.question.allowFreeText ? 1 : 0)
    : 0;
  const freeTextSelected = Boolean(
    state.question?.allowFreeText
      && state.question.selectedIndex === state.question.options.length,
  );

  useInput(
    (_input, key) => {
      if (!state.question) return;
      if ((key.ctrl && _input === 'c') || key.escape) {
        if (state.turnId) bridge.cancel(state.turnId);
        dispatch({ type: 'cancelling' });
        return;
      }
      if (key.upArrow || (key.tab && key.shift)) {
        dispatch({
          type: 'question_choice',
          selectedIndex:
            (state.question.selectedIndex - 1 + questionChoiceCount) % questionChoiceCount,
        });
        return;
      }
      if (key.downArrow || key.tab) {
        dispatch({
          type: 'question_choice',
          selectedIndex: (state.question.selectedIndex + 1) % questionChoiceCount,
        });
        return;
      }
      if (key.return && !freeTextSelected) {
        const option = state.question.options[state.question.selectedIndex];
        if (option) answerQuestion({ option_id: option.id });
      }
    },
    { isActive: state.question !== null },
  );

  // Approval answers are handled here while the text input is unfocused.
  useInput(
    (_input, key) => {
      if (key.leftArrow || key.rightArrow || key.tab) {
        dispatch({
          type: 'approval_choice',
          choice: state.approval?.choice === 'allow' ? 'deny' : 'allow',
        });
        return;
      }
      if (key.return) {
        answerApproval(state.approval?.choice === 'allow');
        return;
      }
      if (key.escape) {
        answerApproval(false);
      }
    },
    { isActive: state.approval !== null },
  );

  useInput(
    (_input, key) => {
      if (key.upArrow || key.downArrow || key.tab || key.leftArrow || key.rightArrow) {
        setPermissionChoice((current) => current === 'full_access' ? 'ask_for_approval' : 'full_access');
        return;
      }
      if (key.return && permissionChoice) {
        bridge.send({ type: 'permission_set', preset: permissionChoice });
        setPermissionChoice(null);
        return;
      }
      if (key.escape) setPermissionChoice(null);
    },
    { isActive: permissionChoice !== null },
  );

  // Global keys. Ink delivers Ctrl+C as input, so cancelling is explicit.
  useInput(
    (input, key) => {
      if (key.ctrl && input === 'c') {
        if (busy) {
          if (state.turnId) bridge.cancel(state.turnId);
          dispatch({ type: 'cancelling' });
        } else if (draft === '') {
          exit();
        } else {
          setDraft('');
        }
        return;
      }

      if (key.escape) {
        if (busy) {
          if (state.turnId) bridge.cancel(state.turnId);
          dispatch({ type: 'cancelling' });
        } else {
          setDraft('');
        }
        return;
      }

      if (key.ctrl && input === 'd' && !busy && draft === '') exit();
    },
    { isActive: state.approval === null && state.question === null && permissionChoice === null },
  );

  const submit = (value: string): void => {
    const text = value.trim();
    if (text === '') return;
    setDraft('');
    // Commands are answered by the TUI, so the agent never sees them.
    const command = findCommand(text);
    if (command) {
      if (command.name === '/permissions') setPermissionChoice(state.permissionPreset);
      return;
    }
    if (state.status !== 'idle' && state.turnId !== null) {
      steerCounter.current += 1;
      dispatch({ type: 'steer_submitted' });
      bridge.send({
        type: 'user_steer',
        turn_id: state.turnId,
        steer_id: `steer-${steerCounter.current}`,
        text,
      });
      return;
    }
    sendTurn(text);
  };

  const sendTurn = (text: string): void => {
    turnCounter.current += 1;
    const turnId = `turn-${turnCounter.current}`;
    dispatch({ type: 'submit', turnId, text });
    bridge.send({ type: 'user_turn', turn_id: turnId, text });
  };

  return (
    <Box height={Math.max(1, rows - 1)} flexDirection="column">
      <Box flexGrow={1} flexShrink={1} flexDirection="column" overflowY="hidden">
        {state.status !== 'starting' && state.entries.length === 0 ? (
          <Box flexShrink={0}>
            <Text dimColor>Nosis · {state.workspace}</Text>
          </Box>
        ) : null}

        <Transcript state={state} />

        {state.approval ? (
          <Box flexShrink={0}>
            <ApprovalPrompt command={state.approval.command} choice={state.approval.choice} />
          </Box>
        ) : null}
        {state.question ? (
          <Box flexShrink={0}>
            <UserQuestionPrompt question={state.question} />
          </Box>
        ) : null}
        {permissionChoice ? (
          <Box flexShrink={0}>
            <PermissionPrompt selected={permissionChoice} />
          </Box>
        ) : null}
      </Box>

      {state.status === 'fatal' ? (
        <Box flexShrink={0} marginTop={1}>
          <Text dimColor>Press Ctrl+C to exit.</Text>
        </Box>
      ) : permissionChoice ? null : state.question && freeTextSelected ? (
        <Prompt
          value={questionDraft}
          onChange={setQuestionDraft}
          onSubmit={(value) => {
            const text = value.trim();
            if (text) answerQuestion({ text });
          }}
          focus
          busy={false}
          docked
          placeholder="输入自定义答案…"
        />
      ) : state.question ? null : (
        <Prompt
          value={draft}
          onChange={setDraft}
          onSubmit={submit}
          focus={state.approval === null && permissionChoice === null}
          busy={state.status !== 'idle'}
          docked
          commands={COMMANDS}
          placeholder={state.status === 'idle' ? undefined : 'steer the current turn…'}
        />
      )}

      <StatusBar state={state} elapsed={elapsed} />
    </Box>
  );
}
