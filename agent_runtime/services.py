"""Process resources shared by otherwise independent session runtimes."""

from pathlib import Path
import json

from agent_core import SchedulerService

from .settings import EnvironmentReloader, SettingsStore


class RuntimeServices:
    def __init__(self, config_directory: Path, *,
                 scheduler: SchedulerService | None = None,
                 start_scheduler: bool = True) -> None:
        self.config_directory = config_directory.resolve()
        self.environment = EnvironmentReloader(self.config_directory / ".env")
        self.settings = SettingsStore(self.config_directory, self.environment)
        self._owns_scheduler = scheduler is None
        self.scheduler = scheduler or SchedulerService(
            self.config_directory / "schedule.jsonl",
        )
        if self._owns_scheduler:
            from .scheduled_runner import ScheduledTurnRunner

            self.scheduler.runner = ScheduledTurnRunner(
                self.config_directory, self.scheduler, services=self,
            )
            if start_scheduler:
                self.scheduler.start()

    def close(self) -> None:
        if self._owns_scheduler:
            self.scheduler.close()

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
