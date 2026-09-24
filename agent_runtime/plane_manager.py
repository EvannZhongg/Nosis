"""Assembly, invalidation and resource ownership for one execution plane."""

from pathlib import Path
from agent_core import (
    Agent,
    AgentEvent,
    AgentCancelled as Cancelled,
    ContextWindowEvent,
    JsonlSessionStore,
    JobManager,
    JobStatusEvent,
    MemoryManager,
    MemoryReconciler,
    Session,
    CompositeToolPolicy,
    DirectorySkillSource,
    ExecutionAuthority,
    ExecutionRouter,
    ExecutionScope,
    FULL_ACCESS_AUTHORITY,
    McpConfig,
    McpApprovalPolicy,
    PermissionController,
    PermissionPreset,
    PlanManager,
    ShellApprovalPolicy,
    SkillLoader,
    SubagentRole,
    SubagentRoleRegistry,
    SubagentRuntime,
    HostCommandExecutor,
    SandboxedCommandExecutor,
    ToolExecutionContext,
    TurnControl,
    Workspace,
    WorkspaceInstructions,
    AttachmentPart,
    FilePart,
    ImagePart,
    UnsupportedImageError,
    Schedule,
    ScheduledRun,
    builtin_catalog,
    load_agent_config,
    merge_mcp_servers,
    message_to_dict,
    probe_image,
    runtime_error_info,
    vision_aware_tool_names,
    skill_aware_tool_names,
    platform_workspace_sandbox_backend,
    SchedulerService,
    WORKSPACE_ONLY_AUTHORITY,
)
from agent_core.prompting import render_system_prompt
from agent_core.path_utils import path_for_comparison
from agent_core.providers import LiteLLMImageGenerator, LiteLLMProvider
from agent_core.mcp.manager import McpClientManager, McpServerStatus
from agent_core.tools import ROLE_TOOL_NAMES

from .config import (
    configured_role_names,
    default_config_directory,
    load_config_with_name,
    load_image_generation_config,
    load_model_options,
    load_prompt_templates,
    load_vision_config,
    memory_store,
)
from .execution_plane import ExecutionPlane
from .instructions import load_workspace_instructions
from .plugins import PluginAgent, PluginManager
from .settings import EnvironmentReloader, SettingsStore, configuration_fingerprint
from .callbacks import RuntimeCallbacks
from .session_controller import SessionRuntimeController


class ExecutionPlaneManager:
    def __init__(self, config_directory: Path, environment: EnvironmentReloader,
                 scheduler: SchedulerService, callbacks: RuntimeCallbacks,
                 execution_authority_limit: ExecutionAuthority) -> None:
        self._config_directory = config_directory
        self._config_path = config_directory / "provider_config.json"
        self._agent_config_path = config_directory / "agent_config.json"
        self._environment = environment
        self.scheduler = scheduler
        self.callbacks = callbacks
        self._execution_authority_limit = execution_authority_limit
        self.current: ExecutionPlane | None = None

    def matches(
        self,
        *,
        workspace: Workspace,
        provider_name: str,
        agent_config,
        configuration_fingerprint: str,
        instructions: WorkspaceInstructions,
    ) -> bool:
        return self.current is not None and self.current.matches(
            workspace=workspace,
            provider_name=provider_name,
            agent_config=agent_config,
            configuration_fingerprint=configuration_fingerprint,
            instructions=instructions,
        )

    def ensure(self, session: SessionRuntimeController) -> ExecutionPlane:
        if (
            session.session is None
            or session.store is None
            or session.workspace is None
            or session.sessions_directory is None
            or session.provider_name is None
        ):
            raise RuntimeError("received 'user_turn' before 'open_session'")

        self._environment.reload()
        agent_config = load_agent_config(self._agent_config_path)
        config_fingerprint = configuration_fingerprint(
            self._config_directory
        )
        instructions = load_workspace_instructions(
            self._config_directory,
            session.workspace,
            agent_config.workspace_instruction_files,
        )
        plane = self.current
        if self.matches(
            workspace=session.workspace,
            provider_name=session.provider_name,
            agent_config=agent_config,
            configuration_fingerprint=config_fingerprint,
            instructions=instructions,
        ):
            assert plane is not None
            return plane
        if plane is not None:
            self.close()

        self.callbacks.on_phase("starting", ())
        config_path = self._config_path
        workspace = session.workspace
        store = None
        memory_context = None
        if agent_config.memory.enabled:
            store = memory_store(self._config_directory)
            store.initialize(workspace.path)
            memory_context = store.load(workspace.path)
        _, config = load_config_with_name(config_path, session.provider_name)
        plugins = PluginManager.discover(
            self._config_directory / "plugins"
        )
        plugin_mcp_servers, mcp_warnings = (
            plugins.load_mcp_servers()
            if agent_config.mcp.enabled
            else ((), ())
        )
        plugin_agents, agent_warnings = plugins.load_agents()
        mcp_config = McpConfig(
            enabled=agent_config.mcp.enabled,
            servers=merge_mcp_servers(
                (agent_config.mcp.servers, plugin_mcp_servers)
            ),
        )
        mcp = McpClientManager(
            mcp_config,
            workspace.path,
            on_status=self.callbacks.on_mcp_status,
        )
        jobs: JobManager | None = None
        workspace_executor: SandboxedCommandExecutor | None = None
        host_executor: HostCommandExecutor | None = None
        execution_router: ExecutionRouter | None = None
        try:
            skills = SkillLoader().load(
                (
                    DirectorySkillSource(self._config_directory / "skills"),
                    *plugins.skill_sources(),
                )
            )
            prompts = load_prompt_templates(self._config_directory / "prompts")

            main_provider = LiteLLMProvider(
                model=config.model,
                base_url=config.url,
                api_key=config.key,
                max_context_tokens=config.max_context_tokens,
                media_root=workspace.path,
                request_timeout_seconds=(
                    agent_config.provider.request_timeout_seconds
                ),
                max_retries=agent_config.provider.max_retries,
            )
            image_generator = None
            if agent_config.tools.is_enabled("generate_image"):
                image_config = load_image_generation_config(config_path)
                if image_config is None:
                    raise ValueError(
                        "generate_image is enabled but provider_config.json "
                        "has no image_generation configuration"
                    )
                image_generator = LiteLLMImageGenerator(
                    model=image_config.model,
                    base_url=image_config.url,
                    api_key=image_config.key,
                    default_aspect_ratio=image_config.default_aspect_ratio,
                    default_image_size=image_config.default_image_size,
                )
            vision_provider = self._provider_for(
                load_vision_config(config_path), workspace, agent_config
            )
            memory = None
            if memory_context is not None:
                assert store is not None
                memory = MemoryManager(
                    workspace.path,
                    store,
                    MemoryReconciler(
                        main_provider,
                        global_prompt=prompts.global_memory,
                        workspace_prompt=prompts.workspace_memory,
                        global_max_tokens=agent_config.memory.global_max_tokens,
                        workspace_max_tokens=(
                            agent_config.memory.workspace_max_tokens
                        ),
                    ),
                )

            # One catalog of stateless Tool instances is shared by the main
            # Agent and by every sub-agent role.
            catalog = builtin_catalog()
            catalog = catalog.extend(mcp.start())

            if session.permissions is None:
                raise RuntimeError("session permissions are not initialized")
            jobs = JobManager(session.session)
            workspace_executor = SandboxedCommandExecutor(
                workspace.path,
                platform_workspace_sandbox_backend(),
                excluded_environment_names=self._environment.loaded_names,
            )
            host_executor = HostCommandExecutor(workspace.path)
            execution_router = ExecutionRouter(
                workspace_executor,
                host_executor,
                authority=lambda: session.permissions.authority.intersect(
                    self._execution_authority_limit
                ),
                host_tool=mcp.requires_approval,
            )
            jobs.set_update_callback(
                lambda update: self.callbacks.on_event(
                    JobStatusEvent(
                        job_id=update.job_id,
                        kind=update.kind,
                        status=update.status,
                    ),
                    update.turn_id,
                )
            )
            subagents = self._subagent_runtime(
                agent_config,
                catalog,
                config_path,
                workspace,
                session.permissions,
                prompts.subagent,
                prompts.consolidator,
                instructions,
                plugin_agents,
            )
            context = ToolExecutionContext(
                workspace=workspace,
                session=session.session,
                sessions_directory=session.sessions_directory,
                execution_router=execution_router,
                max_generation_tokens=agent_config.max_generation_tokens,
                vision_input=(
                    "image" in main_provider.capabilities.input_modalities
                ),
                vision_provider=vision_provider,
                image_generator=image_generator,
                mcp=mcp,
                subagents=subagents,
                jobs=jobs,
                skills=skills,
                plan=session.plan,
                ask_user=(
                    self.callbacks.ask_user
                ),
                scheduler=self.scheduler,
                memory=memory,
            )
            agent = Agent(
                provider=main_provider,
                session=session.session,
                system_prompt=render_system_prompt(
                    prompts.system,
                    workspace,
                    skills,
                    instructions=instructions,
                    memory=memory_context,
                ),
                consolidator_prompt=prompts.consolidator,
                config=agent_config,
                tools=catalog.select(
                    (
                        *skill_aware_tool_names(
                            vision_aware_tool_names(
                                agent_config.tools.enabled,
                                main_provider,
                                vision_provider,
                            ),
                            skills,
                        ),
                        "ask_user",
                        "update_plan",
                        *mcp.tool_names,
                        *(("remember",) if memory is not None else ()),
                    ),
                    context,
                    policy=session.permissions,
                ),
                context=context,
            )
            plane = ExecutionPlane(
                workspace=workspace,
                provider_name=session.provider_name,
                agent_config=agent_config,
                configuration_fingerprint=config_fingerprint,
                instructions=instructions,
                memory=memory,
                agent=agent,
                execution_router=execution_router,
                jobs=jobs,
                mcp=mcp,
                context_window=agent.context_window(),
                runtime_warnings=(
                    *plugins.warnings,
                    *mcp_warnings,
                    *agent_warnings,
                    *skills.warnings,
                ),
            )
        except BaseException:
            try:
                if jobs is not None:
                    jobs.close()
            finally:
                try:
                    mcp.close()
                finally:
                    if execution_router is not None:
                        execution_router.close()
                    else:
                        try:
                            if workspace_executor is not None:
                                workspace_executor.close()
                        finally:
                            if host_executor is not None:
                                host_executor.close()
            raise
        self.current = plane
        return plane

    def _provider_for(
        self,
        config,
        workspace: Workspace,
        agent_config,
    ) -> LiteLLMProvider | None:
        if config is None:
            return None
        return LiteLLMProvider(
            model=config.model,
            base_url=config.url,
            api_key=config.key,
            max_context_tokens=config.max_context_tokens,
            media_root=workspace.path,
            request_timeout_seconds=(
                agent_config.provider.request_timeout_seconds
            ),
            max_retries=agent_config.provider.max_retries,
        )


    def _subagent_runtime(
        self,
        agent_config,
        catalog,
        config_path: Path,
        workspace: Workspace,
        permission_controller: PermissionController,
        subagent_prompt_template: str,
        consolidator_prompt: str,
        workspace_instructions: WorkspaceInstructions,
        plugin_agents: tuple[PluginAgent, ...],
    ) -> SubagentRuntime | None:
        """Build the sub-agent runtime, or None when no role is configured."""
        if not agent_config.tools.is_enabled("subagent"):
            return None
        # A provider override for a role that does not exist is a typo, not
        # a silent no-op: the two config files must name the same roles.
        known_roles = set(agent_config.subagent_roles)
        known_roles.update(agent.name for agent in plugin_agents)
        unknown = configured_role_names(config_path) - known_roles
        if unknown:
            names = ", ".join(sorted(unknown))
            raise ValueError(
                "provider_config.json configures unknown subagent role(s): "
                f"{names}"
            )
        roles = [
            SubagentRole(
                name=name,
                description=role.description,
                tools=role.tools.enabled,
                provider=self._provider_for(
                    load_config_with_name(config_path, role=name)[1],
                    workspace,
                    agent_config,
                ),
                vision_provider=self._provider_for(
                    load_vision_config(config_path, role=name),
                    workspace,
                    agent_config,
                ),
            )
            for name, role in agent_config.subagent_roles.items()
            if role.enabled
        ]
        provider_names = set(load_model_options(config_path)[1])
        for agent in plugin_agents:
            declared_provider = (
                agent.model
                if agent.model not in {None, "inherit"}
                and agent.model in provider_names
                else None
            )
            provider_config = (
                load_config_with_name(config_path, declared_provider)[1]
                if declared_provider is not None
                else load_config_with_name(config_path, role=agent.name)[1]
            )
            roles.append(
                SubagentRole(
                    name=agent.name,
                    description=agent.description,
                    instructions=agent.instructions,
                    tools=(
                        ROLE_TOOL_NAMES
                        if agent.tools is None
                        else agent.tools
                    ),
                    provider=self._provider_for(
                        provider_config, workspace, agent_config
                    ),
                    vision_provider=self._provider_for(
                        load_vision_config(config_path, role=agent.name),
                        workspace,
                        agent_config,
                    ),
                )
            )
        registry = SubagentRoleRegistry(roles)
        if not registry:
            return None
        return SubagentRuntime(
            config=agent_config,
            catalog=catalog,
            roles=registry,
            subagent_prompt_template=subagent_prompt_template,
            consolidator_prompt=consolidator_prompt,
            workspace_instructions=workspace_instructions,
            tool_policy=permission_controller,
        )


    def close(self) -> None:
        plane = self.current
        self.current = None
        if plane is not None:
            plane.close()
