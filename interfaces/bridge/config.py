import json
import os
import shutil
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from agent_core.providers import LiteLLMProvider


DEFAULT_CONFIG_FILENAMES = (
    "provider_config.json",
    "agent_config.json",
)


@dataclass(frozen=True)
class ModelConfig:
    model: str
    url: str | None
    key: str | None
    max_context_tokens: int | None = None


def default_config_directory() -> Path:
    return Path.home() / ".nosis"


def initialize_config_directory(directory: Path) -> tuple[Path, ...]:
    created = []
    directory.mkdir(parents=True, exist_ok=True)
    defaults = files("interfaces.bridge.defaults")
    for filename in DEFAULT_CONFIG_FILENAMES:
        path = directory / filename
        if path.exists():
            continue
        path.write_text(
            defaults.joinpath(filename).read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        created.append(path)

    skills_directory = directory / "skills"
    skills_directory.mkdir(exist_ok=True)
    packaged_skills = defaults.joinpath("skills")
    for skill in sorted(packaged_skills.iterdir(), key=lambda item: item.name):
        if not skill.is_dir() or not skill.joinpath("SKILL.md").is_file():
            continue
        destination = skills_directory / skill.name
        if destination.exists():
            continue
        _copy_resource_directory(skill, destination)
        created.append(destination)
    return tuple(created)


def _copy_resource_directory(source, destination: Path) -> None:
    destination.mkdir()
    for child in source.iterdir():
        target = destination / child.name
        if child.is_dir():
            _copy_resource_directory(child, target)
            continue
        with child.open("rb") as source_file, target.open("xb") as target_file:
            shutil.copyfileobj(source_file, target_file)


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
