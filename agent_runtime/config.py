import json
import os
import shutil
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from agent_core.memory import MemoryStore
from agent_core.providers import LiteLLMProvider


DEFAULT_CONFIG_FILENAMES = (
    "provider_config.json",
    "agent_config.json",
)
PROMPT_FILENAMES = (
    "Soul.md",
    "SubAgent.md",
    "Consolidator.md",
    "GlobalMemory.md",
    "WorkspaceMemory.md",
)


@dataclass(frozen=True)
class ModelConfig:
    model: str
    url: str | None
    key: str | None
    max_context_tokens: int | None = None


@dataclass(frozen=True)
class ImageGenerationConfig:
    provider: str
    model: str
    url: str | None
    key: str | None
    default_aspect_ratio: str | None = None
    default_image_size: str | None = None


@dataclass(frozen=True)
class PromptTemplates:
    system: str
    subagent: str
    consolidator: str
    global_memory: str
    workspace_memory: str


def default_config_directory() -> Path:
    return Path.home() / ".nosis"


def load_scratch_workspace_root(path: Path) -> Path:
    with path.open(encoding="utf-8") as file:
        document = json.load(file)
    if not isinstance(document, dict):
        raise ValueError(f"configuration must be a JSON object: {path}")
    value = document.get("scratch_workspace_root")
    if value is None:
        raise ValueError(
            "missing required config field 'scratch_workspace_root' in "
            f"{path}; add an absolute path such as "
            "'~/.nosis/workspaces/scratch'"
        )
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            "config field 'scratch_workspace_root' must be a non-empty "
            f"path in {path}"
        )
    root = Path(value.strip()).expanduser()
    if not root.is_absolute():
        raise ValueError(
            "config field 'scratch_workspace_root' must resolve to an "
            f"absolute path in {path}"
        )
    return root.resolve()


def memory_store(directory: Path) -> MemoryStore:
    return MemoryStore(
        directory / "MEMORY.md",
        directory / "sessions",
    )


def initialize_config_directory(directory: Path) -> tuple[Path, ...]:
    created = []
    directory.mkdir(parents=True, exist_ok=True)
    defaults = files("agent_runtime.defaults")
    for filename in DEFAULT_CONFIG_FILENAMES:
        path = directory / filename
        if path.exists():
            continue
        path.write_text(
            defaults.joinpath(filename).read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        created.append(path)

    instructions_path = directory / "AGENTS.md"
    if not instructions_path.exists():
        instructions_path.touch()
        created.append(instructions_path)

    created.extend(memory_store(directory).initialize())

    prompts_directory = directory / "prompts"
    prompts_directory.mkdir(exist_ok=True)
    packaged_prompts = defaults.joinpath("prompts_template")
    for filename in PROMPT_FILENAMES:
        path = prompts_directory / filename
        if path.exists():
            continue
        _copy_resource_file(packaged_prompts.joinpath(filename), path)
        created.append(path)

    skills_directory = directory / "skills"
    skills_directory.mkdir(exist_ok=True)
    plugins_directory = directory / "plugins"
    plugins_directory.mkdir(exist_ok=True)
    created.extend(
        _install_packaged_directories(
            defaults.joinpath("skills"),
            skills_directory,
            "SKILL.md",
        )
    )
    created.extend(
        _install_packaged_directories(
            defaults.joinpath("plugins"),
            plugins_directory,
            "plugin.json",
        )
    )

    return tuple(created)


def _install_packaged_directories(
    source,
    destination: Path,
    entrypoint: str,
) -> list[Path]:
    """Copy each packaged subdirectory that carries its entrypoint file."""
    installed = []
    for child in sorted(source.iterdir(), key=lambda item: item.name):
        if not child.is_dir() or not child.joinpath(entrypoint).is_file():
            continue
        target = destination / child.name
        if target.exists():
            continue
        _copy_resource_directory(child, target)
        installed.append(target)
    return installed


# Packaged directories are copied straight from the tree that holds them, which
# is the working tree for an editable install, so byte-code caches and Finder
# metadata can sit beside the real files. Neither belongs in installed defaults.
_SKIPPED_RESOURCE_NAMES = frozenset({"__pycache__", ".DS_Store"})


def _copy_resource_directory(source, destination: Path) -> None:
    destination.mkdir()
    for child in source.iterdir():
        if child.name in _SKIPPED_RESOURCE_NAMES:
            continue
        target = destination / child.name
        if child.is_dir():
            _copy_resource_directory(child, target)
            continue
        _copy_resource_file(child, target)


def _copy_resource_file(source, destination: Path) -> None:
    with source.open("rb") as source_file, destination.open("xb") as target_file:
        shutil.copyfileobj(source_file, target_file)


def load_prompt_templates(directory: Path) -> PromptTemplates:
    return PromptTemplates(
        system=(directory / "Soul.md").read_text(encoding="utf-8").strip(),
        subagent=(directory / "SubAgent.md").read_text(encoding="utf-8").strip(),
        consolidator=(
            (directory / "Consolidator.md").read_text(encoding="utf-8").strip()
        ),
        global_memory=(
            (directory / "GlobalMemory.md").read_text(encoding="utf-8").strip()
        ),
        workspace_memory=(
            (directory / "WorkspaceMemory.md").read_text(encoding="utf-8").strip()
        ),
    )


def _read_config(
    path: Path,
) -> tuple[str, dict[str, object], dict[str, object], dict[str, object], dict[str, object]]:
    """Return main provider, providers, main_agent, subagent, role overrides."""
    with path.open(encoding="utf-8") as file:
        data = json.load(file)

    main_agent = data.get("main_agent")
    if not isinstance(main_agent, dict):
        raise ValueError("config field 'main_agent' must be an object")
    provider = main_agent.get("provider")
    subagent = data.get("subagent", {})
    if not isinstance(subagent, dict):
        raise ValueError("config field 'subagent' must be an object")
    roles = data.get("subagent_roles", {})
    if not isinstance(roles, dict):
        raise ValueError("config field 'subagent_roles' must be an object")
    for name, settings in roles.items():
        if not isinstance(settings, dict):
            raise ValueError(
                f"config field 'subagent_roles.{name}' must be an object"
            )
    providers = data.get("providers")
    if not isinstance(provider, str) or not provider.strip():
        raise ValueError(
            "config field 'main_agent.provider' must be a non-empty string"
        )
    provider = provider.strip()
    if not isinstance(providers, dict):
        raise ValueError("config field 'providers' must be an object")
    if provider not in providers:
        raise ValueError(f"provider '{provider}' is not configured")
    return provider, providers, main_agent, subagent, roles


def configured_role_names(path: Path) -> frozenset[str]:
    """Return the role names that override a provider in this file."""
    return frozenset(_read_config(path)[4])


def _resolve_field(
    field: str,
    role: str | None,
    main_agent: dict[str, object],
    subagent: dict[str, object],
    roles: dict[str, object],
) -> str | None:
    """Resolve one provider field down the role → subagent → main chain.

    An empty string means "inherit", so the chain keeps walking instead of
    stopping at a key that merely exists. A role's own block is consulted
    only for that role; the main agent is the last word for everyone.
    """
    sources: list[tuple[str, dict[str, object]]] = []
    if role is not None:
        role_settings = roles.get(role)
        if isinstance(role_settings, dict):
            sources.append((f"subagent_roles.{role}", role_settings))
        sources.append(("subagent", subagent))
    sources.append(("main_agent", main_agent))
    for label, source in sources:
        value = source.get(field)
        if value is None or value == "":
            continue
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                f"config field '{label}.{field}' must be a non-empty string"
            )
        return value.strip()
    return None


def _provider_entry(
    providers: dict[str, object],
    provider: str,
) -> dict[str, object]:
    selected = providers.get(provider)
    if not isinstance(selected, dict):
        raise ValueError(f"provider '{provider}' is not configured")
    return selected


def _model_name(selected: dict[str, object], provider: str) -> str:
    model = selected.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ValueError(
            f"provider '{provider}' field 'model' must be a non-empty string"
        )
    return model.strip()


def load_model_options(path: Path) -> tuple[str, dict[str, str]]:
    """Return the default provider name and every provider's model name.

    Keys are deliberately not resolved: listing the choices must not
    require a key for providers the user is not using.
    """
    default, providers, _, _, _ = _read_config(path)
    return default, {
        name: _model_name(_provider_entry(providers, name), name)
        for name in providers
    }


def load_config_with_name(
    path: Path,
    provider: str | None = None,
    *,
    role: str | None = None,
) -> tuple[str, ModelConfig]:
    default, providers, _, subagent, roles = _read_config(path)
    # A role or the subagent default overrides the interface's choice; the
    # main agent's own provider is the caller's default, not an override.
    configured = _resolve_field(
        "provider", role, {}, subagent, roles
    )
    if configured is not None:
        provider = configured
    provider = default if provider is None else provider
    selected = _provider_entry(providers, provider)
    model = _model_name(selected, provider)

    url = _optional_string(selected, "url", provider)
    key = _optional_string(selected, "key", provider)
    max_context_tokens = _optional_positive_integer(
        selected,
        "max_context_tokens",
        provider,
    )
    if key and key.startswith("${") and key.endswith("}"):
        key_env = key[2:-1]
        if not key_env:
            raise ValueError(
                f"provider '{provider}' field 'key' has an empty "
                "environment variable reference"
            )
        key = os.environ.get(key_env)
        if not key:
            raise ValueError(
                f"environment variable '{key_env}' is required "
                f"for provider '{provider}'"
            )

    return provider, ModelConfig(
        model=model,
        url=url,
        key=key,
        max_context_tokens=max_context_tokens,
    )


def load_config(
    path: Path,
    provider: str | None = None,
    *,
    role: str | None = None,
) -> ModelConfig:
    return load_config_with_name(path, provider, role=role)[1]


def load_vision_config(
    path: Path,
    *,
    role: str | None = None,
) -> ModelConfig | None:
    """Resolve the vision provider for an agent, or None when unset.

    The provider is taken from ``vision_provider`` down the
    role → subagent → main_agent chain, and must be declared: routing a
    user's images to a second provider is a privacy and cost decision, so
    it is never inferred. ``None`` means this agent has no way to see an
    image, which leaves ``analyze_image`` unregistered.
    """
    _, _, main_agent, subagent, roles = _read_config(path)
    configured = _resolve_field(
        "vision_provider", role, main_agent, subagent, roles
    )
    if configured is None:
        return None
    result = load_config(path, configured)
    _ensure_vision_capable(result, configured)
    return result


def load_image_generation_config(
    path: Path,
) -> ImageGenerationConfig | None:
    """Load the separately routed image generator, when configured."""
    with path.open(encoding="utf-8") as file:
        document = json.load(file)
    if not isinstance(document, dict):
        raise ValueError(f"configuration must be a JSON object: {path}")
    value = document.get("image_generation")
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("config field 'image_generation' must be an object")
    unknown = set(value) - {
        "provider",
        "model",
        "default_aspect_ratio",
        "default_image_size",
    }
    if unknown:
        raise ValueError(
            "unknown field(s) in 'image_generation': "
            + ", ".join(sorted(unknown))
        )
    provider = value.get("provider")
    model = value.get("model")
    if not isinstance(provider, str) or not provider.strip():
        raise ValueError(
            "config field 'image_generation.provider' must be a non-empty string"
        )
    if not isinstance(model, str) or not model.strip():
        raise ValueError(
            "config field 'image_generation.model' must be a non-empty string"
        )
    provider = provider.strip()
    connection = load_config(path, provider)
    return ImageGenerationConfig(
        provider=provider,
        model=model.strip(),
        url=connection.url,
        key=connection.key,
        default_aspect_ratio=_optional_image_setting(
            value, "default_aspect_ratio"
        ),
        default_image_size=_optional_image_setting(
            value, "default_image_size"
        ),
    )


def _is_vision_capable(config: ModelConfig) -> bool:
    return "image" in LiteLLMProvider.capabilities_for_model(
        config.model,
        config.url,
    ).input_modalities


def _ensure_vision_capable(config: ModelConfig, provider: str) -> None:
    if not _is_vision_capable(config):
        raise ValueError(
            f"provider '{provider}' configured as vision_provider "
            "does not support image input"
        )


def _optional_string(
    data: dict[str, object],
    field: str,
    provider: str,
) -> str | None:
    value = data.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"provider '{provider}' field '{field}' must be a non-empty string"
        )
    return value.strip()


def _optional_image_setting(
    data: dict[str, object], field: str
) -> str | None:
    value = data.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"config field 'image_generation.{field}' must be a non-empty string"
        )
    return value.strip()


def _optional_positive_integer(
    data: dict[str, object],
    field: str,
    provider: str,
) -> int | None:
    value = data.get(field)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(
            f"provider '{provider}' field '{field}' must be a positive integer"
        )
    return value
