"""Session binding, provider selection and persisted permission state."""

from pathlib import Path
from agent_core import JsonlSessionStore, Session, Workspace, PlanManager, PermissionController, PermissionPreset, CompositeToolPolicy
from .config import load_model_options
from .settings import EnvironmentReloader
from .callbacks import RuntimeCallbacks


class SessionRuntimeController:
    def __init__(self, config_directory: Path, environment: EnvironmentReloader,
                 callbacks: RuntimeCallbacks, approval_policy: CompositeToolPolicy) -> None:
        self._config_directory = config_directory
        self._config_path = config_directory / "provider_config.json"
        self._environment = environment
        self.callbacks = callbacks
        self.approval_policy = approval_policy
        self.session: Session | None = None
        self.store: JsonlSessionStore | None = None
        self.workspace: Workspace | None = None
        self.provider_name: str | None = None
        self.permissions: PermissionController | None = None
        self.plan: PlanManager | None = None
        self.sessions_directory = config_directory / "sessions"

    def open_session(self, workspace: Path, session_id: str | None = None,
                     provider: str | None = None) -> bool:
        config_path = self._config_path
        self._environment.reload()

        workspace = Workspace(workspace)
        default_provider, models = load_model_options(config_path)
        sessions_directory = self._config_directory / "sessions"
        self.sessions_directory = sessions_directory
        self.store = JsonlSessionStore(sessions_directory)
        stored_provider = (
            self.store.provider_for(str(session_id))
            if isinstance(session_id, str) and session_id
            else None
        )
        main_provider_name = (
            provider
            if isinstance(provider, str) and provider
            else stored_provider or default_provider
        )
        if main_provider_name not in models:
            raise ValueError(f"provider '{main_provider_name}' is not configured")

        resumed = isinstance(session_id, str) and bool(session_id)
        if resumed:
            bound_workspace = self.store.workspace_for(str(session_id))
            if bound_workspace:
                workspace = Workspace(Path(bound_workspace))
        self.session = (
            self.store.load(str(session_id), recover=False)
            if resumed
            else Session()
        )
        self.workspace = workspace
        # Runtime events are persisted as soon as they happen.
        self.store.bind_workspace(self.session.session_id, workspace.path)
        self.session.workspace = str(workspace.path)
        self.session.attach_journal_sink(
            lambda events: self.store.append_events(
                self.session.session_id, events, workspace=workspace.path
            )
        )
        self.session.recover()
        self.plan = PlanManager(
            self.session,
            self.callbacks.on_plan,
        )

        self.provider_name = main_provider_name
        self.permissions = PermissionController(
            self.session,
            self.approval_policy,
            self._persist_permission_preset,
        )
        return resumed

    def _persist_permission_preset(self, preset: PermissionPreset) -> None:
        if self.store is None or self.session is None or self.workspace is None:
            raise RuntimeError("session is not initialized")
        self.store.set_permission_preset(
            self.session.session_id,
            preset,
            self.workspace.path,
        )


    def change_provider(self, provider: str) -> None:
        assert self.session is not None and self.store is not None and self.workspace is not None
        self.store.set_provider(self.session.session_id, provider, self.workspace.path)
        self.provider_name = provider

    def change_workspace(self, workspace: Workspace) -> None:
        assert self.session is not None and self.store is not None
        self.store.bind_workspace(self.session.session_id, workspace.path)
        self.workspace = workspace
        self.session.workspace = str(workspace.path)
        self.session.attach_journal_sink(
            lambda events: self.store.append_events(
                self.session.session_id, events, workspace=workspace.path,
            )
        )
