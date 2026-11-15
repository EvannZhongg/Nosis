"""Process resources shared by Session Runtimes."""

from pathlib import Path
import json
from contextlib import contextmanager
from threading import RLock
from typing import Callable

from agent_core import SchedulerService

from .settings import EnvironmentReloader, SettingsStore
from .scheduled_turn import ScheduledTurn
from .session_controller import SessionRuntimeController


class RuntimeServices:
    def __init__(self, config_directory: Path, *,
                 scheduler: SchedulerService | None = None) -> None:
        self.config_directory = config_directory.resolve()
        self.environment = EnvironmentReloader(self.config_directory / ".env")
        self.settings = SettingsStore(self.config_directory, self.environment)
        self._owns_scheduler = scheduler is None
        self.scheduler = scheduler or SchedulerService(
            self.config_directory / "schedule.jsonl",
        )
        self._scheduler_started = False
        self.session_lock = RLock()
        self.sessions: dict[str, SessionRuntimeController] = {}

    @contextmanager
    def session_permissions(self, session_id: str, caller: SessionRuntimeController):
        with self.session_lock:
            owner = self.sessions.get(session_id, caller)
            with owner.session_permissions(session_id) as controller:
                yield controller

    def start_scheduling(self, submit: Callable[[ScheduledTurn], None]) -> None:
        def dispatch(schedule, run, started):
            turn = ScheduledTurn(
                run.session_id, Path(schedule.workspace), run.turn_id,
                schedule.action.prompt, started,
            )
            submit(turn)
            return turn.completion

        self.scheduler.runner = dispatch
        self.scheduler.start()
        self._scheduler_started = True

    def close(self) -> None:
        if self._owns_scheduler or self._scheduler_started:
            self.scheduler.close()
            self._scheduler_started = False

    def warmup(self) -> None:
        """Load the default provider's code and vocabulary before accepting sessions."""
        from agent_core.providers import OpenAIProvider

        from .config import ModelConfig, load_model_options

        self.environment.reload()
        path = self.config_directory / "provider_config.json"
        default, models = load_model_options(path)
        document = json.loads(path.read_text(encoding="utf-8"))
        # Warming code must work before the user has configured credentials.
        config = ModelConfig(models[default], None, None, api=document["providers"][default].get("api"))
        if config.provider_class is OpenAIProvider:
            OpenAIProvider.warm(config.model, config.api)
