"""Assembly, invalidation and resource ownership for one execution plane."""

from functools import partial
from pathlib import Path
from typing import Callable
from agent_core import (
    Agent,
    JobManager,
    JobStatusEvent,
    MemoryManager,
    MemoryReconciler,
    ExecutionAuthority,
    ExecutionRouter,
    McpConfig,
    PermissionController,
    SkillLoader,
    SubagentRole,
    SubagentRoleRegistry,
    SubagentRuntime,
    HostCommandExecutor,
    SandboxedCommandExecutor,
    ToolExecutionContext,
    Workspace,
    WorkspaceInstructions,
    builtin_catalog,
    load_agent_config,
    merge_mcp_servers,
    vision_aware_tool_names,
    skill_aware_tool_names,
    platform_workspace_sandbox_backend,
    SchedulerService,
    Session,
)
from agent_core.prompting import render_system_prompt
from agent_core.llm import LLMProvider
from agent_core.tools import ROLE_TOOL_NAMES

from .config import (
    configured_skill_sources,
    configured_role_names,
    load_config_with_name,
    load_fast_config,
    load_image_generation_config,
    load_model_options,
    load_prompt_templates,
    load_tool_settings,
    load_vision_config,
    memory_store,
)
from .execution_plane import ExecutionPlane
from .instructions import load_workspace_instructions
from .plugins import PluginAgent, PluginManager
from .role_tools import resolve_role_tools
from .settings import EnvironmentReloader, configuration_fingerprint
from .callbacks import RuntimeCallbacks
from .computer import DesktopComputer
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
        tool_settings = load_tool_settings(self._config_directory / "tool_config.json")
        config_fingerprint = configuration_fingerprint(
            self._config_directory
        )
        instructions = load_workspace_instructions(
            self._config_directory,
            session.workspace,
            agent_config.workspace_instruction_files,
        )
        plugins = PluginManager.discover(self._config_directory / "plugins")
        plugin_mcp_servers, mcp_warnings = (
            plugins.load_mcp_servers() if agent_config.mcp.enabled else ((), ())
        )
        mcp_config = McpConfig(
            enabled=agent_config.mcp.enabled,
            servers=merge_mcp_servers((agent_config.mcp.servers, plugin_mcp_servers)),
        )
        plane = self.current
        if plane is not None and plane.mcp.config == mcp_config and plane.matches(
            workspace=session.workspace,
            provider_name=session.provider_name,
            agent_config=agent_config,
            configuration_fingerprint=config_fingerprint,
            instructions=instructions,
        ):
            if plane.agent is None:
                self._bind_session(plane, session)
            return plane
        if plane is not None:
            self.close()

        self.callbacks.on_phase("starting", ())
        config_path = self._config_path
        workspace = session.workspace
        _, config = load_config_with_name(config_path, session.provider_name)
        plugin_agents, agent_warnings = plugins.load_agents()
        if agent_config.tools.is_enabled("subagent"):
            known_roles = set(agent_config.subagent_roles)
            known_roles.update(agent.name for agent in plugin_agents)
            unknown = configured_role_names(config_path) - known_roles
            if unknown:
                names = ", ".join(sorted(unknown))
                raise ValueError(
                    "provider_config.json configures unknown subagent role(s): "
                    f"{names}"
                )
        from agent_core.mcp.manager import McpClientManager

        mcp = McpClientManager(
            mcp_config, workspace.path,
            on_status=self.callbacks.on_mcp_status,
        )
        workspace_executor: SandboxedCommandExecutor | None = None
        host_executor: HostCommandExecutor | None = None
        execution_router: ExecutionRouter | None = None
        desktop: DesktopComputer | None = None
        try:
            mcp.start()
            skills = SkillLoader().load(
                (
                    *configured_skill_sources(
                        self._config_directory,
                        agent_config,
                        workspace.path,
                    ),
                    *plugins.skill_sources(),
                )
            )
            prompts = load_prompt_templates(self._config_directory / "prompts")

            main_provider = self._provider_for(config, workspace, agent_config)
            fast_config = (
                load_fast_config(config_path) if agent_config.memory.enabled else None
            )
            fast_provider = (
                self._provider_for(fast_config, workspace, agent_config)
                if fast_config is not None else main_provider
            )
            computer_tools = tuple(
                name
                for name in ("computer_screenshot", "computer_action")
                if agent_config.tools.is_enabled(name)
            )
            computer_tool_warnings = (
                (
                    "Configured computer tool(s) "
                    f"{', '.join(computer_tools)} are unavailable because "
                    "the main model does not support image input (visual input).",
                )
                if computer_tools
                and "image" not in main_provider.capabilities.input_modalities
                else ()
            )
            if (
                computer_tools
                and "image" in main_provider.capabilities.input_modalities
            ):
                desktop = DesktopComputer(
                    workspace,
                    input_enabled="computer_action" in computer_tools,
                )
            image_generator = None
            if agent_config.tools.is_enabled("generate_image"):
                from agent_core.providers import LiteLLMImageGenerator

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
            # One catalog of stateless Tool instances is shared by the main
            # Agent and by every sub-agent role.
            catalog = builtin_catalog()
            catalog = catalog.extend(mcp.wait_ready())
            role_tools = {}
            if agent_config.tools.is_enabled("subagent"):
                for name, role in agent_config.subagent_roles.items():
                    if role.enabled:
                        try:
                            role_tools[name] = resolve_role_tools(
                                role.tools.enabled, role.mcp, catalog, mcp,
                            )
                        except ValueError as error:
                            raise ValueError(f"subagent role '{name}': {error}") from error
            resolved_agents = []
            tool_warnings = []
            for agent in plugin_agents:
                try:
                    tools = resolve_role_tools(
                        ROLE_TOOL_NAMES if agent.tools is None else agent.tools,
                        {}, catalog, mcp,
                    )
                except ValueError as error:
                    tool_warnings.append(
                        f"Skipping Agent '{agent.name}' at '{agent.source}': "
                        f"{error}. Check MCP enablement, "
                        "server status, tools.enabled and the qualified tool name."
                    )
                    continue
                role_tools[agent.name] = tools
                resolved_agents.append(agent)

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
            plane = ExecutionPlane(
                workspace=workspace,
                provider_name=session.provider_name,
                agent_config=agent_config,
                tool_settings=tool_settings,
                configuration_fingerprint=config_fingerprint,
                instructions=instructions,
                provider=main_provider,
                fast_provider=fast_provider,
                vision_provider=vision_provider,
                image_generator=image_generator,
                catalog=catalog,
                skills=skills,
                prompts=prompts,
                plugin_agents=tuple(resolved_agents),
                computer_tools=computer_tools,
                execution_router=execution_router,
                mcp=mcp,
                role_tools=role_tools,
                computer=desktop,
                runtime_warnings=(
                    *plugins.warnings,
                    *mcp_warnings,
                    *agent_warnings,
                    *tool_warnings,
                    *skills.warnings,
                    *computer_tool_warnings,
                ),
            )
        except BaseException:
            try:
                if desktop is not None:
                    desktop.close()
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
        self._bind_session(plane, session)
        self.current = plane
        return plane

    def _bind_session(self, plane: ExecutionPlane, session: SessionRuntimeController) -> None:
        try:
            agent_config = plane.agent_config
            workspace = plane.workspace
            if session.permissions is None or session.session is None:
                raise RuntimeError("session permissions are not initialized")
            main_provider = plane.provider
            fast_provider = plane.fast_provider
            vision_provider = plane.vision_provider
            image_generator = plane.image_generator
            catalog = plane.catalog
            skills = plane.skills
            prompts = plane.prompts
            instructions = plane.instructions
            plugin_agents = plane.plugin_agents
            desktop = plane.computer
            computer_tools = plane.computer_tools
            mcp = plane.mcp
            execution_router = plane.execution_router
            config_path = self._config_path
            store = None
            memory_context = None
            if agent_config.memory.enabled:
                store = memory_store(self._config_directory)
                store.initialize(workspace.path)
                memory_context = store.load(workspace.path)
            memory = None

            def create_memory(
                owner: Session,
            ) -> MemoryManager:
                assert store is not None
                return MemoryManager(
                    workspace.path,
                    store,
                    MemoryReconciler(
                        fast_provider,
                        global_prompt=prompts.global_memory,
                        workspace_prompt=prompts.workspace_memory,
                        global_max_tokens=agent_config.memory.global_max_tokens,
                        workspace_max_tokens=agent_config.memory.workspace_max_tokens,
                        on_auxiliary_call=partial(
                            owner.auxiliary_model_completed, model=fast_provider.model_name,
                        ),
                    ),
                )

            if memory_context is not None:
                memory = create_memory(session.session)

            jobs = JobManager(session.session)
            plane.jobs = jobs
            jobs.set_update_callback(
                lambda update: self.callbacks.on_event(
                    JobStatusEvent(
                        job_id=update.job_id,
                        kind=update.kind,
                        status=update.status,
                        details=update.details,
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
                plane.role_tools,
                create_memory if store is not None else None,
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
                tool_settings=plane.tool_settings,
                screenshot=(
                    desktop.capture
                    if desktop is not None
                    and "computer_screenshot" in computer_tools
                    else None
                ),
                computer=(
                    desktop.control
                    if desktop is not None and "computer_action" in computer_tools
                    else None
                ),
            )
            tools = catalog.select(
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
            )
            agent = Agent(
                provider=main_provider,
                session=session.session,
                system_prompt=render_system_prompt(
                    prompts.system,
                    workspace,
                    skills,
                    instructions=instructions,
                    mcp_instructions=mcp.instructions_for(
                        tool.name for tool in tools.definitions
                    ),
                ),
                consolidator_prompt=prompts.consolidator,
                memory_context=memory_context,
                config=agent_config,
                tools=tools,
                context=context,
            )
            plane.memory = memory
            plane.agent = agent
            plane.context_window = agent.context_window()
        except BaseException:
            if self.current is plane:
                self.current = None
            plane.close()
            raise

    def unbind_session(self) -> None:
        if self.current is not None:
            self.current.unbind_session()

    def _provider_for(
        self,
        config,
        workspace: Workspace,
        agent_config,
    ) -> LLMProvider | None:
        if config is None:
            return None
        from agent_core.providers import LiteLLMProvider, OpenAIProvider

        arguments = dict(
            model=config.model,
            base_url=config.url,
            api_key=config.key,
            max_context_tokens=config.max_context_tokens,
            media_root=workspace.path,
            request_timeout_seconds=(
                agent_config.provider.request_timeout_seconds
            ),
            max_retries=agent_config.provider.max_retries,
            reasoning_effort=config.reasoning_effort,
            reasoning_parameters=config.reasoning_parameters,
        )
        if config.api is not None:
            return OpenAIProvider(**arguments, api=config.api)
        route, separator, wire_model = config.model.partition("/")
        if not separator or route == "openai":
            return OpenAIProvider(
                **arguments, wire_model=wire_model if separator else config.model,
            )
        return LiteLLMProvider(**arguments)


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
        role_tools: dict[str, tuple[str, ...]],
        memory_factory: Callable[[Session], MemoryManager] | None,
    ) -> SubagentRuntime | None:
        """Build the sub-agent runtime, or None when no role is configured."""
        if not agent_config.tools.is_enabled("subagent"):
            return None
        roles = [
            SubagentRole(
                name=name,
                description=role.description,
                tools=role_tools[name],
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
                    tools=role_tools[agent.name],
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
            memory_factory=memory_factory,
        )


    def close(self) -> None:
        plane = self.current
        self.current = None
        if plane is not None:
            plane.close()
