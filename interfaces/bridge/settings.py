"""Shared configuration inspection and mutation for every interface."""

from __future__ import annotations

import json
import os
import re
import tempfile
from hashlib import sha256
from pathlib import Path
from threading import RLock

from dotenv import dotenv_values

from agent_core import DirectorySkillSource, SkillLoader, load_agent_config
from agent_core.tools import ROLE_TOOL_NAMES, TOOL_NAMES

from .config import load_model_options
from .plugins import PluginManager


_PROVIDER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_ENV_REFERENCE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


def _read_json(path: Path) -> dict[str, object]:
    with path.open(encoding="utf-8") as file:
        value = json.load(file)
    if not isinstance(value, dict):
        raise ValueError(f"configuration must be a JSON object: {path}")
    return value


def _atomic_text(path: Path, text: str, *, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_json(path: Path, value: dict[str, object]) -> None:
    _atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _dotenv_values(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    return {
        name: value
        for name, value in dotenv_values(path, interpolate=False).items()
        if value is not None
    }


def _update_dotenv(path: Path, name: str, value: str | None) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    pattern = re.compile(rf"^\s*(?:export\s+)?{re.escape(name)}\s*=")
    replacement = None if value is None else f"{name}={value}"
    output = []
    replaced = False
    for line in lines:
        if pattern.match(line):
            if not replaced and replacement is not None:
                output.append(replacement)
            replaced = True
        else:
            output.append(line)
    if not replaced and replacement is not None:
        output.append(replacement)
    _atomic_text(path, "\n".join(output) + ("\n" if output else ""), mode=0o600)


def configuration_fingerprint(directory: Path) -> str:
    digest = sha256()
    for filename in ("provider_config.json", "agent_config.json", ".env"):
        path = directory / filename
        digest.update(filename.encode())
        digest.update(b"\0")
        if path.is_file():
            digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


class EnvironmentReloader:
    """Apply the current .env file without leaving deleted values behind."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._original: dict[str, str | None] = {}
        self._loaded: set[str] = set()

    def reload(self) -> None:
        values = _dotenv_values(self.path)
        for name in self._loaded - set(values):
            original = self._original[name]
            if original is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = original
        for name, value in values.items():
            if name not in self._original:
                self._original[name] = os.environ.get(name)
            os.environ[name] = value
        self._loaded = set(values)

    @property
    def loaded_names(self) -> frozenset[str]:
        """Names currently injected from the managed dotenv file."""
        return frozenset(self._loaded)


class SettingsStore:
    def __init__(
        self,
        directory: Path,
        environment: EnvironmentReloader | None = None,
    ) -> None:
        self.directory = directory.expanduser().resolve()
        self.provider_path = self.directory / "provider_config.json"
        self.agent_path = self.directory / "agent_config.json"
        self.env_path = self.directory / ".env"
        self._lock = RLock()
        self._environment = environment or EnvironmentReloader(self.env_path)

    def model_options(self) -> tuple[str, dict[str, str]]:
        with self._lock:
            return load_model_options(self.provider_path)

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return self._snapshot()

    def _snapshot(self) -> dict[str, object]:
        self._environment.reload()
        provider_document = _read_json(self.provider_path)
        agent_document = _read_json(self.agent_path)
        default_provider, models = load_model_options(self.provider_path)
        agent_config = load_agent_config(self.agent_path)
        env = _dotenv_values(self.env_path)
        providers_value = provider_document.get("providers")
        assert isinstance(providers_value, dict)

        providers = []
        for name, model in models.items():
            raw = providers_value[name]
            assert isinstance(raw, dict)
            key_value = raw.get("key")
            reference = (
                _ENV_REFERENCE.fullmatch(key_value)
                if isinstance(key_value, str)
                else None
            )
            providers.append({
                "id": name,
                "model": model,
                "url": raw.get("url"),
                "max_context_tokens": raw.get("max_context_tokens"),
                "credential": {
                    "source": "env" if reference else "inline" if key_value else "none",
                    "env_name": reference.group(1) if reference else None,
                    "configured": (
                        bool(env.get(reference.group(1)) or os.environ.get(reference.group(1)))
                        if reference
                        else bool(key_value)
                    ),
                },
            })

        plugins = PluginManager.discover(self.directory / "plugins")
        skills = SkillLoader().load(
            (
                DirectorySkillSource(self.directory / "skills"),
                *plugins.skill_sources(),
            ),
            warnings=plugins.warnings,
        )
        plugin_mcp, plugin_mcp_warnings = plugins.load_mcp_servers()
        plugin_agents, plugin_agent_warnings = plugins.load_agents()

        skill_items = []
        for skill in skills:
            path = skill.directory / "SKILL.md"
            skill_items.append({
                "id": skill.identifier,
                "name": skill.name,
                "description": skill.description,
                "source": skill.namespace or "standalone",
                "path": str(path),
                "content": path.read_text(encoding="utf-8"),
            })

        plugin_items = [{
            "name": plugin.name,
            "version": plugin.version,
            "description": plugin.description,
            "enabled": plugin.enabled,
            "capabilities": list(plugin.capabilities),
            "path": str(plugin.root / "plugin.json"),
            "components": {
                "skills": [str(path) for path in plugin.components.skills],
                "mcp": [str(path) for path in plugin.components.mcp],
                "agents": [str(path) for path in plugin.components.agents],
            },
        } for plugin in plugins.plugins]

        mcp_servers = [
            self._mcp_snapshot(server, "agent_config")
            for server in agent_config.mcp.servers
        ] + [
            self._mcp_snapshot(server, server.namespace or "plugin")
            for server in plugin_mcp
        ]

        main_agent = provider_document.get("main_agent")
        subagent = provider_document.get("subagent", {})
        roles = provider_document.get("subagent_roles", {})
        assert isinstance(main_agent, dict)
        assert isinstance(subagent, dict)
        assert isinstance(roles, dict)

        return {
            "revision": configuration_fingerprint(self.directory),
            "config_directory": str(self.directory),
            "default_provider": default_provider,
            "providers": providers,
            "routing": {
                "main_agent": main_agent.get("provider"),
                "vision_provider": main_agent.get("vision_provider") or None,
                "subagent": subagent.get("provider") or None,
                "subagent_vision_provider": subagent.get("vision_provider") or None,
                "roles": roles,
            },
            "agent": {
                "max_same_tool_calls": agent_config.max_same_tool_calls,
                "output_reserve_tokens": agent_config.output_reserve_tokens,
                "max_generation_tokens": agent_config.max_generation_tokens,
                "workspace_instruction_files": list(agent_config.workspace_instruction_files),
                "tools": {name: agent_config.tools.is_enabled(name) for name in TOOL_NAMES},
                "context": {
                    "compression": {
                        "enabled": agent_config.context.enabled,
                        "trigger_ratio": agent_config.context.trigger_ratio,
                        "keep_recent_units": agent_config.context.keep_recent_units,
                    }
                },
                "memory": {
                    "enabled": agent_config.memory.enabled,
                    "global_max_tokens": agent_config.memory.global_max_tokens,
                    "workspace_max_tokens": agent_config.memory.workspace_max_tokens,
                },
                "subagent_roles": {
                    name: {
                        "enabled": role.enabled,
                        "description": role.description,
                        "tools": {tool: role.tools.is_enabled(tool) for tool in ROLE_TOOL_NAMES},
                    }
                    for name, role in agent_config.subagent_roles.items()
                },
                "mcp_enabled": agent_config.mcp.enabled,
            },
            "skills": skill_items,
            "plugins": plugin_items,
            "mcp_servers": mcp_servers,
            "plugin_agents": [{
                "name": agent.name,
                "description": agent.description,
                "model": agent.model,
                "source": str(agent.source),
            } for agent in plugin_agents],
            "warnings": list(skills.warnings) + list(plugin_mcp_warnings) + list(plugin_agent_warnings),
        }

    @staticmethod
    def _mcp_snapshot(server, source: str) -> dict[str, object]:
        return {
            "id": server.identifier,
            "source": source,
            "enabled": server.enabled,
            "transport": server.transport,
            "command": server.command,
            "args": list(server.args),
            "cwd": server.cwd,
            "url": server.url,
            "env_names": sorted(server.env),
            "header_names": sorted(server.headers),
            "startup_timeout_seconds": server.startup_timeout_seconds,
            "call_timeout_seconds": server.call_timeout_seconds,
        }

    def save_provider(
        self,
        provider_id: str,
        payload: object,
        expected_revision: str | None = None,
    ) -> dict[str, object]:
        with self._lock:
            self._check_revision(expected_revision)
            return self._save_provider(provider_id, payload)

    def _save_provider(self, provider_id: str, payload: object) -> dict[str, object]:
        if not _PROVIDER_NAME.fullmatch(provider_id):
            raise ValueError("provider id may contain letters, numbers, '.', '_' and '-'")
        if not isinstance(payload, dict):
            raise ValueError("provider settings must be an object")
        allowed = {"model", "url", "max_context_tokens", "api_key", "set_default"}
        unknown = set(payload) - allowed
        if unknown:
            raise ValueError(f"unknown provider setting(s): {', '.join(sorted(unknown))}")
        model = payload.get("model")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be a non-empty string")
        url = payload.get("url")
        if url is not None and (not isinstance(url, str) or not url.strip()):
            raise ValueError("url must be a non-empty string or null")
        context_limit = payload.get("max_context_tokens")
        if context_limit is not None and (
            isinstance(context_limit, bool) or not isinstance(context_limit, int) or context_limit < 1
        ):
            raise ValueError("max_context_tokens must be a positive integer or null")
        api_key = payload.get("api_key", {"action": "keep"})
        if not isinstance(api_key, dict) or api_key.get("action") not in {"keep", "set", "clear"}:
            raise ValueError("api_key.action must be keep, set, or clear")

        document = _read_json(self.provider_path)
        providers = document.get("providers")
        if not isinstance(providers, dict):
            raise ValueError("config field 'providers' must be an object")
        existing = providers.get(provider_id)
        if existing is not None and not isinstance(existing, dict):
            raise ValueError(f"provider '{provider_id}' must be an object")
        entry: dict[str, object] = dict(existing or {})
        entry["model"] = model.strip()
        if url is None:
            entry.pop("url", None)
        else:
            entry["url"] = url.strip()
        if context_limit is None:
            entry.pop("max_context_tokens", None)
        else:
            entry["max_context_tokens"] = context_limit

        action = api_key["action"]
        old_key = entry.get("key")
        old_reference = _ENV_REFERENCE.fullmatch(old_key) if isinstance(old_key, str) else None
        if action == "set":
            value = api_key.get("value")
            if not isinstance(value, str) or not value:
                raise ValueError("api_key.value must be a non-empty string")
            if "\n" in value or "\r" in value:
                raise ValueError("api_key.value must not contain line breaks")
            env_name = old_reference.group(1) if old_reference else self._provider_env_name(provider_id)
            entry["key"] = f"${{{env_name}}}"
        elif action == "clear":
            entry.pop("key", None)

        providers[provider_id] = entry
        if payload.get("set_default") is True:
            main_agent = document.get("main_agent")
            if not isinstance(main_agent, dict):
                raise ValueError("config field 'main_agent' must be an object")
            main_agent["provider"] = provider_id

        self._validate_provider_document(document)
        if action == "set":
            _update_dotenv(self.env_path, env_name, value)
        elif action == "clear" and old_reference:
            _update_dotenv(self.env_path, old_reference.group(1), None)
        _atomic_json(self.provider_path, document)
        self._environment.reload()
        return self._snapshot()

    def save_agent(
        self,
        payload: object,
        expected_revision: str | None = None,
    ) -> dict[str, object]:
        with self._lock:
            self._check_revision(expected_revision)
            return self._save_agent(payload)

    def save_routing(
        self,
        payload: object,
        expected_revision: str | None = None,
    ) -> dict[str, object]:
        with self._lock:
            self._check_revision(expected_revision)
            if not isinstance(payload, dict):
                raise ValueError("routing settings must be an object")
            allowed = {
                "main_agent", "vision_provider", "subagent",
                "subagent_vision_provider", "roles",
            }
            unknown = set(payload) - allowed
            if unknown:
                raise ValueError(f"unknown routing setting(s): {', '.join(sorted(unknown))}")
            document = _read_json(self.provider_path)
            main = document.get("main_agent")
            subagent = document.get("subagent")
            if not isinstance(main, dict) or not isinstance(subagent, dict):
                raise ValueError("provider routing blocks must be objects")
            main["provider"] = payload.get("main_agent")
            main["vision_provider"] = payload.get("vision_provider") or ""
            subagent["provider"] = payload.get("subagent") or ""
            subagent["vision_provider"] = payload.get("subagent_vision_provider") or ""
            roles = payload.get("roles", {})
            if not isinstance(roles, dict):
                raise ValueError("routing roles must be an object")
            document["subagent_roles"] = roles
            self._validate_provider_document(document)
            _atomic_json(self.provider_path, document)
            return self._snapshot()

    def _save_agent(self, payload: object) -> dict[str, object]:
        if not isinstance(payload, dict):
            raise ValueError("agent settings must be an object")
        allowed = {
            "max_same_tool_calls", "output_reserve_tokens", "max_generation_tokens",
            "workspace_instruction_files", "tools", "context", "memory", "subagent_roles", "mcp_enabled",
        }
        unknown = set(payload) - allowed
        if unknown:
            raise ValueError(f"unknown agent setting(s): {', '.join(sorted(unknown))}")
        document = _read_json(self.agent_path)
        for field in (
            "max_same_tool_calls", "output_reserve_tokens", "max_generation_tokens",
            "workspace_instruction_files", "context", "memory", "subagent_roles",
        ):
            if field in payload:
                document[field] = payload[field]
        if "tools" in payload:
            document["main_agent"] = {"tools": payload["tools"]}
        if "mcp_enabled" in payload:
            mcp = document.get("mcp")
            if not isinstance(mcp, dict):
                mcp = {"servers": {}}
            mcp["enabled"] = payload["mcp_enabled"]
            document["mcp"] = mcp
        self._validate_agent_document(document)
        _atomic_json(self.agent_path, document)
        return self._snapshot()

    def _check_revision(self, expected: str | None) -> None:
        if expected is not None and expected != configuration_fingerprint(self.directory):
            raise ValueError("configuration changed; reload settings before saving")

    def _validate_provider_document(self, document: dict[str, object]) -> None:
        unknown_top_level = set(document) - {
            "main_agent", "subagent", "subagent_roles", "providers"
        }
        if unknown_top_level:
            raise ValueError(
                f"unknown provider configuration field(s): {', '.join(sorted(unknown_top_level))}"
            )
        descriptor, name = tempfile.mkstemp(dir=self.directory, suffix=".json")
        path = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                json.dump(document, file)
            default, models = load_model_options(path)
            if default not in models:
                raise ValueError("default provider is not configured")
            providers = document.get("providers")
            assert isinstance(providers, dict)
            for provider, value in providers.items():
                if not isinstance(provider, str) or not _PROVIDER_NAME.fullmatch(provider):
                    raise ValueError(f"invalid provider id: {provider}")
                if not isinstance(value, dict):
                    raise ValueError(f"provider '{provider}' must be an object")
                unknown = set(value) - {"model", "url", "key", "max_context_tokens"}
                if unknown:
                    raise ValueError(f"unknown field(s) for provider '{provider}': {', '.join(sorted(unknown))}")
                url = value.get("url")
                key = value.get("key")
                limit = value.get("max_context_tokens")
                if url is not None and (not isinstance(url, str) or not url.strip()):
                    raise ValueError(f"provider '{provider}' url must be a non-empty string")
                if key is not None and (not isinstance(key, str) or not key.strip()):
                    raise ValueError(f"provider '{provider}' key must be a non-empty string")
                if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
                    raise ValueError(f"provider '{provider}' max_context_tokens must be positive")
            for label in ("main_agent", "subagent"):
                route = document.get(label, {})
                if not isinstance(route, dict):
                    raise ValueError(f"config field '{label}' must be an object")
                unknown = set(route) - {"provider", "vision_provider"}
                if unknown:
                    raise ValueError(f"unknown field(s) in '{label}': {', '.join(sorted(unknown))}")
                self._validate_routes(route, providers, label)
            roles = document.get("subagent_roles", {})
            if not isinstance(roles, dict):
                raise ValueError("config field 'subagent_roles' must be an object")
            for role, route in roles.items():
                if not isinstance(route, dict):
                    raise ValueError(f"config field 'subagent_roles.{role}' must be an object")
                unknown = set(route) - {"provider", "vision_provider"}
                if unknown:
                    raise ValueError(f"unknown field(s) in 'subagent_roles.{role}': {', '.join(sorted(unknown))}")
                self._validate_routes(route, providers, f"subagent_roles.{role}")
        finally:
            path.unlink(missing_ok=True)

    def _validate_agent_document(self, document: dict[str, object]) -> None:
        unknown = set(document) - {
            "max_same_tool_calls", "output_reserve_tokens", "max_generation_tokens",
            "workspace_instruction_files", "context", "memory", "main_agent", "subagent_roles", "mcp",
        }
        if unknown:
            raise ValueError(f"unknown agent configuration field(s): {', '.join(sorted(unknown))}")
        descriptor, name = tempfile.mkstemp(dir=self.directory, suffix=".json")
        path = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                json.dump(document, file)
            load_agent_config(path)
        finally:
            path.unlink(missing_ok=True)

    @staticmethod
    def _provider_env_name(provider_id: str) -> str:
        normalized = re.sub(r"[^A-Za-z0-9]+", "_", provider_id).strip("_").upper()
        return f"NOSIS_{normalized}_API_KEY"

    @staticmethod
    def _validate_routes(
        route: dict[str, object],
        providers: dict[str, object],
        path: str,
    ) -> None:
        for field in ("provider", "vision_provider"):
            value = route.get(field)
            if value in (None, ""):
                continue
            if not isinstance(value, str) or value not in providers:
                raise ValueError(f"config field '{path}.{field}' must name a configured provider")
