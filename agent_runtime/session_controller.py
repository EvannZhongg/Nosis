"""Session binding, provider selection and persisted permission state."""

from contextlib import ExitStack
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
        self._journal = ExitStack()

    def close(self) -> None:
        if self.session is not None:
            self.session.attach_journal_sink(None)
        self._journal.close()

    def open_session(self, workspace: Path, session_id: str | None = None,
                     provider: str | None = None) -> bool:
        config_path = self._config_path
        self._environment.reload()
        self.close()
        self.session = None
        self.plan = None
        self.permissions = None
        self.provider_name = None

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
        self.workspace = workspace
        session = Session(str(session_id)) if resumed else Session()
        opening_id = session.session_id
        # Own the writer before loading state so recovery cannot race another
        # Runtime. A failed open leaves only the read-only browsing context.
        with ExitStack() as journal:
            sink = journal.enter_context(
                self.store.open_journal(opening_id, workspace.path)
            )
            if resumed:
                session = self.store.load(opening_id, recover=False)
            self.store.bind_workspace(opening_id, workspace.path)
            session.workspace = str(workspace.path)
            session.attach_journal_sink(sink)
            session.recover()
            self._journal = journal.pop_all()
            self.session = session
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
        self.close()
        try:
            self.store.bind_workspace(self.session.session_id, workspace.path)
            self.workspace = workspace
            self.session.workspace = str(workspace.path)
        finally:
            self.session.attach_journal_sink(
                self._journal.enter_context(
                    self.store.open_journal(self.session.session_id)
                )
            )
