"""Own configured IM transports alongside a frontend's shared Session Host."""

from pathlib import Path

from agent_core import JsonlSessionStore, Workspace
from interfaces.session_host import ActiveSessionRegistry

from .bindings import Bindings
from .config import load_channels
from .controller import MobileController
from .feishu import FeishuTransport


class MobileService:
    def __init__(
        self,
        directory: Path,
        host: ActiveSessionRegistry,
        store: JsonlSessionStore,
        workspace: Workspace,
    ) -> None:
        self.channels = []
        bindings = Bindings(directory / "im_bindings.json")
        for config in load_channels(directory):
            transport = FeishuTransport(config)
            controller = MobileController(
                config.id, config.allowed_users, transport, host, store, bindings, workspace
            )
            self.channels.append((transport, controller))

    async def start(self) -> None:
        for transport, controller in self.channels:
            await transport.start(controller)

    async def close(self) -> None:
        for transport, controller in reversed(self.channels):
            try:
                await transport.close()
            finally:
                await controller.close()
