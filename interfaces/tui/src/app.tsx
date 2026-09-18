import React, { useEffect, useReducer, useRef, useState } from 'react';
import { Box, Text, useApp, useInput, useWindowSize } from 'ink';
import type { PermissionPreset, SessionSummary } from '@nosis/protocol';
import { BridgeClient } from './bridge.js';
import { COMMANDS, findCommand } from './commands.js';
import { Prompt } from './input.js';
import {
  ApprovalPrompt,
  PermissionPrompt,
  SessionPicker,
  StatusBar,
  Transcript,
  UserQuestionPrompt,
} from './renderer.js';
import { initialState, reducer } from './state.js';

export type AppProps = {
  python: string;
  workspace: string;
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
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [elapsed, setElapsed] = useState(0);
  const bridgeRef = useRef<BridgeClient | null>(null);
  const turnCounter = useRef(0);
  const steerCounter = useRef(0);
  const startedAt = useRef<number | null>(null);

  /**
   * One bridge per Session: opening binds lightweight Session state, while
   * Provider, MCP, Tools and Agent initialize on the first ordinary turn.
   *
   * Messages from a replaced process are dropped, so a turn it is still
   * unwinding cannot leak into the new transcript.
   */
  useEffect(() => {
    const client = new BridgeClient({
      python: props.python,
      cwd: props.workspace,
      onMessage: (message) => {
        if (bridgeRef.current === client) dispatch({ type: 'message', message });
      },
      onExit: (code) => {
        if (bridgeRef.current !== client) return;
        if (code === 0) exit();
        else dispatch({ type: 'exited', code });
      },
      onProtocolError: (error) => {
        if (bridgeRef.current !== client) return;
        dispatch({
          type: 'message',
          message: {
            type: 'fatal',
            error: { type: 'ProtocolError', message: error.message },
          },
        });
      },
    });
    bridgeRef.current = client;
    client.send({
      type: 'open_session',
      workspace: protocolPath(props.workspace),
      session_id: sessionId,
      provider_config_path: protocolPath(props.providerConfigPath),
      agent_config_path: protocolPath(props.agentConfigPath),
    });
    // The stored conversation is not part of `session_ready`: it is sent only when
    // asked, so a frontend that reads it elsewhere never pays for it.
    client.send({ type: 'load_session' });
    return () => {
      bridgeRef.current = null;
      client.shutdown();
    };
  }, [sessionId]);

  const busy =
    state.status === 'starting' ||
    state.status === 'streaming' ||
    state.status === 'running' ||
    state.status === 'cancelling';
  const inputBusy = state.status !== 'idle' && state.status !== 'runtime_failed';

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
    bridgeRef.current?.send({
      type: 'approval_response',
      request_id: state.approval.requestId,
      approved,
    });
    dispatch({ type: 'approval_resolved' });
  };

  const answerQuestion = (answer: { option_id: string } | { text: string }): void => {
    if (!state.question) return;
    bridgeRef.current?.send({
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
        if (state.turnId) bridgeRef.current?.cancel(state.turnId);
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
        bridgeRef.current?.send({ type: 'permission_set', preset: permissionChoice });
        setPermissionChoice(null);
        return;
      }
      if (key.escape) setPermissionChoice(null);
    },
    { isActive: permissionChoice !== null },
  );

  /** Hands the runtime to another conversation in this workspace. */
  const switchSession = (choice: SessionSummary): void => {
    // `restart` also closes the picker and empties the transcript.
    dispatch({ type: 'restart' });
    setSessionId(choice.session_id);
  };

  useInput(
    (_input, key) => {
      const sessions = state.sessions;
      if (sessions === null) return;
      if (key.escape) {
        dispatch({ type: 'sessions_closed' });
        return;
      }
      if (sessions.list.length === 0) return;
      if (key.upArrow || (key.tab && key.shift)) {
        dispatch({
          type: 'sessions_choice',
          selectedIndex:
            (sessions.selectedIndex - 1 + sessions.list.length) % sessions.list.length,
        });
        return;
      }
      if (key.downArrow || key.tab) {
        dispatch({
          type: 'sessions_choice',
          selectedIndex: (sessions.selectedIndex + 1) % sessions.list.length,
        });
        return;
      }
      if (key.return) {
        const choice = sessions.list[sessions.selectedIndex];
        if (choice) switchSession(choice);
      }
    },
    { isActive: state.sessions !== null },
  );

  // Global keys. Ink delivers Ctrl+C as input, so cancelling is explicit.
  useInput(
    (input, key) => {
      if (key.ctrl && input === 'c') {
        if (busy) {
          if (state.turnId) bridgeRef.current?.cancel(state.turnId);
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
          if (state.turnId) bridgeRef.current?.cancel(state.turnId);
          dispatch({ type: 'cancelling' });
        } else {
          setDraft('');
        }
        return;
      }

      if (key.ctrl && input === 'd' && !busy && draft === '') exit();
    },
    {
      isActive:
        state.approval === null && state.question === null && permissionChoice === null
        && state.sessions === null,
    },
  );

  const submit = (value: string): void => {
    const text = value.trim();
    if (text === '') return;
    setDraft('');
    // Commands are answered by the TUI, so the agent never sees them.
    const command = findCommand(text);
    if (command) {
      if (command.name === '/permissions') setPermissionChoice(state.permissionPreset);
      else if (command.name === '/sessions') {
        bridgeRef.current?.send({ type: 'list_sessions' });
        dispatch({ type: 'sessions_opened' });
      }
      return;
    }
    if (busy && state.turnId !== null) {
      steerCounter.current += 1;
      dispatch({ type: 'steer_submitted' });
      bridgeRef.current?.send({
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
    bridgeRef.current?.send({ type: 'user_turn', turn_id: turnId, text });
  };

  return (
    <Box height={Math.max(1, rows - 1)} flexDirection="column">
      <Box flexGrow={1} flexShrink={1} flexDirection="column" overflowY="hidden">
        {state.status !== 'opening' && state.entries.length === 0 ? (
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
        {state.sessions ? (
          <Box flexShrink={0}>
            <SessionPicker
              sessions={state.sessions.list}
              selectedIndex={state.sessions.selectedIndex}
            />
          </Box>
        ) : null}
      </Box>

      {state.status === 'fatal' ? (
        <Box flexShrink={0} marginTop={1}>
          <Text dimColor>Press Ctrl+C to exit.</Text>
        </Box>
      ) : permissionChoice || state.sessions ? null : state.question && freeTextSelected ? (
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
          focus={state.approval === null && permissionChoice === null && state.sessions === null}
          busy={inputBusy}
          docked
          commands={COMMANDS}
          placeholder={inputBusy ? 'steer the current turn…' : undefined}
        />
      )}

      <StatusBar state={state} elapsed={elapsed} />
    </Box>
  );
}
