"""Newline-delimited JSON adapter for the shared application runtime."""

import _thread
from itertools import count
from pathlib import Path
from queue import Queue
from threading import Lock, RLock, Thread
from typing import TextIO

from agent_core import AgentCancelled as Cancelled, AgentEvent, PermissionPreset, SchedulerService, message_to_dict, runtime_error_info
from agent_core.mcp.manager import McpServerStatus
from agent_runtime.host import RuntimeHost
from agent_runtime.callbacks import RuntimeCallbacks
from agent_runtime.attachments import parse_attachments
from agent_runtime.config import default_config_directory, load_model_options
from .protocol import (
    decode,
    encode,
    event_to_message,
    context_window_to_dict,
    runtime_state_message,
    runtime_failure_to_dict,
    plan_updated_message,
    session_ready_message,
    session_items_message,
    sessions_listed_message,
    settings_snapshot_message,
    settings_update_failed_message,
    usage_to_dict,
)
from .event_stream import EventStream


class Bridge:
    def __init__(self, stdin: TextIO, stdout: TextIO, *,
                 scheduler: SchedulerService | None = None,
                 start_scheduler: bool = True) -> None:
        self._stdin = stdin
        self._stdout = stdout
        self._stdout_lock = RLock()
        self._messages: Queue[dict[str, object] | BaseException | None] = Queue()
        self._reader: Thread | None = None
        self._reader_lock = Lock()
        self._router_lock = Lock()
        self._waiters: dict[str, Queue[dict[str, object] | BaseException]] = {}
        self._input_closed = False
        self._shutdown_requested = False
        self._session_opened = False
        self._interaction_ids = count(1)
        self._interaction_lock = Lock()
        self._approval: dict[str, object] | None = None
        self._question: dict[str, object] | None = None
        self._stream = EventStream()
        self._phase = "inactive"
        self._runtime_warnings: tuple[str, ...] = ()
        self._jobs: dict[str, dict[str, object]] = {}
        self._checkpoint_turn: str | None = None
        self.host = RuntimeHost(
            default_config_directory().resolve(),
            scheduler=scheduler,
            start_scheduler=start_scheduler,
            callbacks=RuntimeCallbacks(
                on_event=self._emit_agent_event,
                on_phase=lambda phase, warnings: self._emit_runtime_state(phase, runtime_warnings=warnings),
                on_plan=lambda snapshot: self.emit(**plan_updated_message(snapshot)),
                on_mcp_status=self._emit_mcp_status,
                request_permission=self.request_permission,
                ask_user=self.request_user_choice,
            ),
        )

    def emit(self, type: str, **fields: object) -> None:
        with self._stdout_lock:
            message = {"type": type, **fields}
            if type == "job_status":
                job_id = str(fields["job_id"])
                if fields["status"] in {"submitted", "running"}:
                    self._jobs[job_id] = {
                        key: fields[key] for key in ("job_id", "kind", "status")
                    }
                else:
                    self._jobs.pop(job_id, None)
            if type == "fatal":
                self._phase = "failed"
                self._approval = self._question = None
                self._jobs.clear()
            state = None
            if type == "runtime_state":
                state = message
            elif type in {
                "permission_changed", "provider_changed", "workspace_changed",
                "plan_updated", "context_window", "job_status", "fatal",
                "turn_completed", "turn_cancelled", "turn_failed",
            }:
                state = self._runtime_snapshot()
            items = None
            if type == "session_ready" or type in {
                "turn_completed", "turn_cancelled", "turn_failed",
            } or (type == "context_window" and self._checkpoint_turn == fields.get("turn_id")):
                session = self.host.sessions.session
                if session is not None:
                    items = [message_to_dict(item) for item in session.items]
                self._checkpoint_turn = None
            self._stdout.write(encode(self._stream.publish(
                message, state=state, items=items,
            )) + "\n")


    def read_message(self) -> dict[str, object] | None:
        """Return the next command routed by the sole stdin reader."""
        self._start_reader()
        message = self._messages.get()
        if isinstance(message, BaseException):
            raise message
        return message


    def _start_reader(self) -> None:
        with self._reader_lock:
            if self._reader is not None:
                return
            self._reader = Thread(
                target=self._read_stdin,
                name="bridge-input-reader",
                daemon=True,
            )
            self._reader.start()


    def _read_stdin(self) -> None:
        first_message = not self._session_opened
        while True:
            try:
                line = self._stdin.readline()
                if not line:
                    self._route_shutdown()
                    return
                stripped = line.strip()
                if stripped:
                    message = decode(stripped)
                    if first_message:
                        first_message = False
                        if message["type"] == "open_session":
                            self._messages.put(message)
                        else:
                            self._route_message(message)
                    else:
                        self._route_message(message)
                    if message["type"] == "shutdown":
                        return
            except ValueError as error:
                self._messages.put(error)
                self._route_shutdown(notify_commands=False)
                return


    def _route_message(self, message: dict[str, object]) -> None:
        message_type = message["type"]
        if message_type in {"approval_response", "user_question_response"}:
            request_id = message.get("request_id")
            if not isinstance(request_id, str):
                return
            with self._router_lock:
                waiter = self._waiters.get(request_id)
                if waiter is not None:
                    waiter.put(message)
            return
        if message_type == "permission_set" and not self._session_opened:
            self._messages.put(message)
            return
        if message_type == "permission_set":
            self._set_permission_preset(message)
            return
        if message_type == "user_steer":
            self._route_steer(message)
            return
        if message_type == "cancel":
            self._route_cancel(message)
            return
        if message_type == "shutdown":
            self._route_shutdown()
            return
        if message_type == "user_turn":
            turn_id = message.get("turn_id")
            if isinstance(turn_id, str):
                self.host.turns.enqueue(turn_id)
        self._messages.put(message)


    def _route_steer(self, message: dict[str, object]) -> None:
        steer_id = message.get("steer_id")
        turn_id = message.get("turn_id")
        text = message.get("text")
        if not isinstance(steer_id, str) or not steer_id:
            return
        accepted = (
            isinstance(turn_id, str) and isinstance(text, str) and bool(text.strip())
            and self.host.turns.steer(turn_id, steer_id, text.strip())
        )
        self.emit(
            "user_steer_received" if accepted else "user_steer_rejected",
            turn_id=turn_id,
            steer_id=steer_id,
            text=text if isinstance(text, str) else "",
        )


    def _route_cancel(self, message: dict[str, object]) -> None:
        turn_id = message.get("turn_id")
        cancelled = self.host.turns.cancel(turn_id)
        with self._router_lock:
            waiters = tuple(self._waiters.values()) if cancelled else ()
        for waiter in waiters:
            waiter.put(Cancelled())
        if cancelled:
            _thread.interrupt_main()


    def _route_shutdown(self, *, notify_commands: bool = True) -> None:
        with self._router_lock:
            self._input_closed = True
            self._shutdown_requested = True
            cancelled = self.host.turns.shutdown()
            waiters = tuple(self._waiters.values())
        for waiter in waiters:
            waiter.put(Cancelled())
        if cancelled:
            _thread.interrupt_main()
        if notify_commands:
            self._messages.put(None)


    def _register_interaction(
        self, request_id: str
    ) -> Queue[dict[str, object] | BaseException]:
        waiter: Queue[dict[str, object] | BaseException] = Queue()
        with self._router_lock:
            self._waiters[request_id] = waiter
            if (
                self._input_closed
                or (
                    self.host.turns.control is not None
                    and self.host.turns.control.cancelled
                )
            ):
                waiter.put(Cancelled())
        self._start_reader()
        return waiter


    def _unregister_interaction(self, request_id: str) -> None:
        with self._router_lock:
            self._waiters.pop(request_id, None)


    @staticmethod
    def _wait_for_interaction(
        waiter: Queue[dict[str, object] | BaseException],
    ) -> dict[str, object]:
        response = waiter.get()
        if isinstance(response, BaseException):
            raise response
        return response


    def request_permission(
        self,
        command: str,
        *,
        kind: str = "shell",
        server: str | None = None,
        tool_name: str | None = None,
    ) -> bool:
        # Serialized so concurrent tool calls queue their prompts instead of
        # racing for each other's answers; the user still answers one at a time.
        with self._interaction_lock:
            request_id = f"{self.host.turns.turn_id}:{next(self._interaction_ids)}"
            waiter = self._register_interaction(request_id)
            self._approval = {
                "type": "approval_request",
                "turn_id": self.host.turns.turn_id,
                "request_id": request_id,
                "command": command,
                "kind": kind,
                "server": server,
                "tool_name": tool_name,
            }
            self._question = None
            if self.host.sessions.session is not None:
                self._emit_runtime_state("waiting_approval")
            self.emit(**self._approval)
            try:
                message = self._wait_for_interaction(waiter)
                return bool(message.get("approved"))
            finally:
                self._unregister_interaction(request_id)
                self._approval = None
                if self.host.sessions.session is not None:
                    self._emit_runtime_state("running")


    def request_user_choice(
        self,
        question: str,
        options: list[dict[str, object]],
        allow_free_text: bool,
    ) -> object:
        with self._interaction_lock:
            request_id = f"{self.host.turns.turn_id}:{next(self._interaction_ids)}"
            waiter = self._register_interaction(request_id)
            self._approval = None
            self._question = {
                "type": "user_question",
                "turn_id": self.host.turns.turn_id,
                "request_id": request_id,
                "question": question,
                "options": options,
                "allow_free_text": allow_free_text,
            }
            if self.host.sessions.session is not None:
                self._emit_runtime_state("waiting_user")
            self.emit(**self._question)
            option_by_id = {
                str(option["id"]): option for option in options
            }
            try:
                while True:
                    message = self._wait_for_interaction(waiter)
                    option_id = message.get("option_id")
                    text = message.get("text")
                    if isinstance(option_id, str) and option_id in option_by_id:
                        option = option_by_id[option_id]
                        return {
                            "type": "option",
                            "id": option_id,
                            "label": option["label"],
                        }
                    if allow_free_text and isinstance(text, str) and text.strip():
                        return {"type": "text", "text": text.strip()}
            finally:
                self._unregister_interaction(request_id)
                self._question = None
                if self.host.sessions.session is not None:
                    self._emit_runtime_state("running")


    def _emit_runtime_state(
        self,
        phase: str,
        *,
        runtime_warnings: tuple[str, ...] = (),
    ) -> None:
        with self._stdout_lock:
            self._phase = phase
            self._runtime_warnings = runtime_warnings
            self.emit(**self._runtime_snapshot())

    def _runtime_snapshot(self) -> dict[str, object]:
        plane = self.host.planes.current
        return runtime_state_message(
            phase=self._phase,
            turn_id=self.host.turns.turn_id,
            approval=self._approval,
            question=self._question,
            provider=self.host.sessions.provider_name,
            permission_preset=(
                self.host.sessions.session.permission_preset.value
                if self.host.sessions.session is not None
                else PermissionPreset.ASK_FOR_APPROVAL.value
            ),
            context_window=(
                context_window_to_dict(plane.context_window)
                if plane is not None
                else None
            ),
            jobs=list(self._jobs.values()),
            runtime_warnings=self._runtime_warnings,
            plan=self.host.sessions.plan.snapshot if self.host.sessions.plan is not None else None,
            workspace=str(self.host.sessions.workspace.path) if self.host.sessions.workspace is not None else None,
        )


    def _emit_agent_event(self, event: AgentEvent, turn_id: str) -> None:
        self.emit(**event_to_message(event, turn_id))


    def _emit_sessions(self) -> None:
        """Answer ``list_sessions`` with this Workspace's stored Sessions."""
        if self.host.sessions.store is None or self.host.sessions.workspace is None:
            raise RuntimeError("received 'list_sessions' before 'open_session'")
        self.emit(
            **sessions_listed_message(
                self.host.sessions.store.list_workspace_sessions(self.host.sessions.workspace.path)
            )
        )


    def _emit_session_items(self) -> None:
        """Answer ``load_session`` with the conversation to render."""
        if self.host.sessions.session is None:
            raise RuntimeError("received 'load_session' before 'open_session'")
        self.emit(
            **session_items_message(
                [message_to_dict(item) for item in self.host.sessions.session.items]
            )
        )


    def _settings_request_id(
        self, message: dict[str, object]
    ) -> str | None:
        value = message.get("request_id")
        return value if isinstance(value, str) and value else None


    def _emit_settings(self, message: dict[str, object]) -> None:
        request_id = self._settings_request_id(message)
        try:
            self.emit(**settings_snapshot_message(
                self.host.settings.snapshot(), request_id
            ))
        except (OSError, ValueError) as error:
            self.emit(**settings_update_failed_message(error, request_id))


    def _save_provider_settings(self, message: dict[str, object]) -> None:
        request_id = self._settings_request_id(message)
        provider = message.get("provider")
        settings = message.get("settings")
        try:
            if not isinstance(provider, str) or not provider:
                raise ValueError("provider must be a non-empty string")
            revision = message.get("expected_revision")
            snapshot = self.host.settings.save_provider(
                provider,
                settings,
                revision if isinstance(revision, str) else None,
            )
            self.host.environment.reload()
            self.emit(**settings_snapshot_message(snapshot, request_id))
        except (OSError, ValueError) as error:
            self.emit(**settings_update_failed_message(error, request_id))


    def _save_agent_settings(self, message: dict[str, object]) -> None:
        request_id = self._settings_request_id(message)
        try:
            revision = message.get("expected_revision")
            snapshot = self.host.settings.save_agent(
                message.get("settings"),
                revision if isinstance(revision, str) else None,
            )
            self.emit(**settings_snapshot_message(snapshot, request_id))
        except (OSError, ValueError) as error:
            self.emit(**settings_update_failed_message(error, request_id))


    def _save_routing_settings(self, message: dict[str, object]) -> None:
        request_id = self._settings_request_id(message)
        try:
            revision = message.get("expected_revision")
            snapshot = self.host.settings.save_routing(
                message.get("settings"),
                revision if isinstance(revision, str) else None,
            )
            self.emit(**settings_snapshot_message(snapshot, request_id))
        except (OSError, ValueError) as error:
            self.emit(**settings_update_failed_message(error, request_id))


    def _emit_mcp_status(self, status: McpServerStatus) -> None:
        fields: dict[str, object] = {
            "server": status.server,
            "status": status.status,
        }
        if status.tool_count is not None:
            fields["tool_count"] = status.tool_count
        if status.error is not None:
            fields["error"] = status.error
        self.emit("mcp_server_status", **fields)


    def serve(self) -> None:
        opened = False
        while True:
            try:
                message = self.read_message()
            except ValueError as error:
                self.emit(
                    "fatal",
                    error={
                        "type": "ProtocolError",
                        "message": str(error),
                        "details": {},
                    },
                )
                raise SystemExit(1)

            if message is None or message["type"] == "shutdown":
                return
            if not opened:
                if message["type"] != "open_session":
                    self.emit(
                        "fatal",
                        error={
                            "type": "ProtocolError",
                            "message": "first message must be 'open_session'",
                            "details": {},
                        },
                    )
                    raise SystemExit(1)
                self.open_session(message)
                opened = True
            elif message["type"] == "user_turn":
                self.run_turn(message)
            elif message["type"] == "permission_set":
                self._set_permission_preset(message)
            elif message["type"] == "provider_set":
                self._set_provider(message)
            elif message["type"] == "workspace_set":
                self._set_workspace(message)
            elif message["type"] == "list_sessions":
                self._emit_sessions()
            elif message["type"] == "load_session":
                self._emit_session_items()
            elif message["type"] == "settings_get":
                self._emit_settings(message)
            elif message["type"] == "settings_provider_save":
                self._save_provider_settings(message)
            elif message["type"] == "settings_agent_save":
                self._save_agent_settings(message)
            elif message["type"] == "settings_routing_save":
                self._save_routing_settings(message)


    def open_session(self, message: dict[str, object]) -> None:
        resumed = self.host.open_session(
            Path(str(message["workspace"])),
            session_id=message.get("session_id"),
            provider=message.get("provider"),
        )
        state = self.host.sessions
        self._session_opened = True
        self.emit(**session_ready_message(
            session_id=state.session.session_id,
            workspace=str(state.workspace.path),
            provider=state.provider_name,
            model=load_model_options(self.host.config_directory / "provider_config.json")[1][state.provider_name],
            resumed=resumed,
            message_count=len(state.session.items),
            permission_preset=state.session.permission_preset.value,
        ))
        self._emit_runtime_state("inactive")

    def _set_permission_preset(self, message: dict[str, object]) -> None:
        preset = PermissionPreset(str(message.get("preset")))
        self.host.set_permission_preset(preset)
        self.emit("permission_changed", preset=preset.value)

    def _set_provider(self, message: dict[str, object]) -> None:
        provider = message.get("provider")
        if not isinstance(provider, str) or not provider:
            raise ValueError("provider must be a non-empty string")
        changed, model = self.host.change_provider(provider)
        self.emit("provider_changed", provider=provider, model=model)
        if changed:
            self._emit_runtime_state("inactive")

    def _set_workspace(self, message: dict[str, object]) -> None:
        value = message.get("workspace")
        if not isinstance(value, str) or not value.strip():
            raise ValueError("workspace must be a non-empty path")
        changed = self.host.change_workspace(Path(value.strip()))
        self.emit("workspace_changed", workspace=str(self.host.sessions.workspace.path))
        if changed:
            self._emit_runtime_state("inactive")

    def run_turn(self, message: dict[str, object]) -> None:
        turn_id = str(message["turn_id"])
        self._checkpoint_turn = turn_id
        result = self.host.run_turn(
            turn_id, str(message["text"]),
            attachments=lambda: parse_attachments(message.get("attachments"), self.host.sessions.workspace),
        )
        self._phase = "failed" if result.startup_failed else "idle"
        self._approval = self._question = None
        self._runtime_warnings = ()
        self._jobs.clear()
        if result.status == "failed":
            self.emit("turn_failed", turn_id=turn_id, error=runtime_failure_to_dict(result.error))
            if result.startup_failed:
                self._emit_runtime_state("failed")
        elif result.status == "cancelled":
            self.emit("turn_cancelled", turn_id=turn_id)
        elif not self._shutdown_requested:
            self.emit("turn_completed", turn_id=turn_id, usage=usage_to_dict(result.usage))

    def close(self) -> None:
        self._route_shutdown(notify_commands=False)
        self.host.close()
