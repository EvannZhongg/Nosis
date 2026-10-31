"""Local channel credentials and authorized messaging identities."""

import json
from dataclasses import dataclass, field
from pathlib import Path

from agent_runtime.settings import environment_reference, load_environment


@dataclass(frozen=True)
class ChannelConfig:
    id: str
    app_id: str
    app_secret: str = field(repr=False)
    allowed_users: tuple[str, ...] = ()


def load_channels(directory: Path) -> list[ChannelConfig]:
    path = directory / "im_channels.json"
    if not path.exists():
        return []
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or not isinstance(document.get("channels"), list):
        raise ValueError("im_channels.json requires a channels array")
    environment = load_environment(directory)
    result = []
    ids = set()
    apps = set()
    for entry in document["channels"]:
        if not isinstance(entry, dict) or entry.get("type") != "feishu":
            raise ValueError("channel type must be 'feishu'")
        if entry.get("enabled", True) is False:
            continue
        values = {}
        for key in ("id", "app_id", "app_secret"):
            value = entry.get(key)
            reference = environment_reference(value)
            if reference is not None:
                value = environment.get(reference)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"channel {key} must be a non-empty string")
            values[key] = value.strip()
        users = entry.get("allowed_users")
        if not isinstance(users, list) or not all(
            isinstance(user, str) and user.startswith("ou_") for user in users
        ):
            raise ValueError("allowed_users must be an array of Feishu open_ids")
        if values["id"] in ids or values["app_id"] in apps:
            raise ValueError("channel ids and app_ids must be unique")
        ids.add(values["id"])
        apps.add(values["app_id"])
        result.append(ChannelConfig(**values, allowed_users=tuple(users)))
    return result
