import json
import os
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


def initialize_default_configs(directory: Path) -> tuple[Path, ...]:
    created = []
    defaults = files("interfaces.bridge.defaults")
    for filename in DEFAULT_CONFIG_FILENAMES:
        path = directory / filename
        if path.exists():
            continue
        directory.mkdir(parents=True, exist_ok=True)
        path.write_text(
            defaults.joinpath(filename).read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        created.append(path)
    return tuple(created)


def _read_config(path: Path) -> tuple[str, dict[str, object], dict[str, object]]:
    """Return main provider, provider definitions, and subagent settings."""
    with path.open(encoding="utf-8") as file:
        data = json.load(file)

    main_agent = data.get("main_agent")
    if not isinstance(main_agent, dict):
        raise ValueError("config field 'main_agent' must be an object")
    provider = main_agent.get("provider")
    subagent = data.get("subagent", {})
    if not isinstance(subagent, dict):
        raise ValueError("config field 'subagent' must be an object")
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
    return provider, providers, subagent


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
    default, providers, _ = _read_config(path)
    return default, {
        name: _model_name(_provider_entry(providers, name), name)
        for name in providers
    }


def load_config_with_name(
    path: Path,
    provider: str | None = None,
    *,
    subagent: bool = False,
) -> tuple[str, ModelConfig]:
    default, providers, subagent_config = _read_config(path)
    if subagent:
        configured = subagent_config.get("provider", "")
        if not isinstance(configured, str):
            raise ValueError("config field 'subagent.provider' must be a string")
        if configured.strip():
            provider = configured.strip()
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
    subagent: bool = False,
) -> ModelConfig:
    return load_config_with_name(path, provider, subagent=subagent)[1]


def load_vision_config(
    path: Path,
    agent_provider: str,
    *,
    agent: str = "main_agent",
    inherited: ModelConfig | None = None,
) -> ModelConfig | None:
    """Resolve the vision provider for an agent role.

    An explicit ``<agent>.vision_provider`` always wins.  Subagents inherit
    the already-resolved main vision provider by default; otherwise the
    agent's own provider or the first configured vision-capable provider is
    selected.  Explicit providers are validated during startup.
    """
    with path.open(encoding="utf-8") as file:
        data = json.load(file)
    settings = data.get(agent, {})
    if not isinstance(settings, dict):
        raise ValueError(f"config field '{agent}' must be an object")
    configured = settings.get("vision_provider")
    if configured is not None and configured != "":
        if not isinstance(configured, str) or not configured.strip():
            raise ValueError(
                f"config field '{agent}.vision_provider' must be a non-empty string"
            )
        result = load_config(path, configured.strip())
        _ensure_vision_capable(result, configured.strip(), agent)
        return result

    if agent == "subagent" and inherited is not None:
        return inherited

    current = load_config(path, agent_provider)
    if _is_vision_capable(current):
        return current

    _, providers, _ = _read_config(path)
    for name, selected in providers.items():
        if name == agent_provider or not isinstance(selected, dict):
            continue
        if not _provider_credentials_available(selected):
            continue
        model = _model_name(selected, name)
        capabilities = LiteLLMProvider.capabilities_for_model(
            model,
            _optional_string(selected, "url", name),
        )
        if "image" in capabilities.input_modalities:
            return load_config(path, name)
    return None


def _is_vision_capable(config: ModelConfig) -> bool:
    return "image" in LiteLLMProvider.capabilities_for_model(
        config.model,
        config.url,
    ).input_modalities


def _ensure_vision_capable(
    config: ModelConfig,
    provider: str,
    agent: str,
) -> None:
    if not _is_vision_capable(config):
        raise ValueError(
            f"provider '{provider}' configured as {agent}.vision_provider "
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


def _provider_credentials_available(data: dict[str, object]) -> bool:
    key = data.get("key")
    if key is None:
        return True
    if not isinstance(key, str):
        return False
    if key.startswith("${") and key.endswith("}"):
        return bool(os.environ.get(key[2:-1]))
    return bool(key.strip())
