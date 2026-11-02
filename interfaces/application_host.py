"""Resident host for shared Bridge sessions and independent HTTP / IM adapters."""

import argparse
import asyncio
import errno
import logging
import os
import signal
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from agent_core import JsonlSessionStore, Workspace
from agent_runtime.config import default_config_directory, initialize_config_directory
from agent_runtime.settings import SettingsStore
from interfaces.mobile.service import MobileService
from interfaces.session_host import ActiveSessionRegistry


log = logging.getLogger(__name__)


@contextmanager
def application_lock(directory: Path) -> Iterator[None]:
    directory.mkdir(parents=True, exist_ok=True)
    # Keep the file in place: unlinking a locked file would let a second
    # process acquire a different inode while the first host is still alive.
    with (directory / "server.lock").open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno not in {errno.EACCES, errno.EAGAIN}:
                raise
            raise RuntimeError(
                f"Nosis server is already running for config directory: {directory}"
            ) from error
        # Closing the handle releases the OS lock, including on process exit.
        yield


class ApplicationHost:
    def __init__(
        self,
        workspace: Workspace,
        store: JsonlSessionStore,
        settings: SettingsStore,
    ) -> None:
        self.workspace = workspace
        self.store = store
        self.settings = settings
        self.active_sessions = ActiveSessionRegistry(store, workspace, settings.directory)

    async def _serve_mobile(self) -> None:
        mobile = MobileService(
            self.settings.directory, self.active_sessions, self.store, self.workspace,
        )
        try:
            await mobile.start()
            await asyncio.Future()
        finally:
            await mobile.close()

    async def _serve_http(self) -> None:
        from interfaces.gui.server import serve_http

        await serve_http(
            self.workspace, self.store, self.settings, self.active_sessions,
        )

    async def serve(self, *, http: bool = True) -> None:
        adapters: set[asyncio.Task[None]] = set()
        try:
            await self.active_sessions.start()
            adapters.add(asyncio.create_task(self._serve_mobile(), name="IM"))
            if http:
                adapters.add(asyncio.create_task(self._serve_http(), name="HTTP"))
            pending = adapters.copy()
            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    try:
                        task.result()
                    except (OSError, ValueError, RuntimeError, ImportError) as error:
                        log.error("Nosis %s adapter failed: %s", task.get_name(), error)
                        if not pending:
                            raise
        finally:
            for task in adapters:
                task.cancel()
            try:
                await asyncio.gather(*adapters, return_exceptions=True)
            finally:
                await self.active_sessions.close_all()


def run_application_host(
    workspace: Path, config_directory: Path, *, http: bool = True,
) -> None:
    directory = config_directory.expanduser().resolve()
    with application_lock(directory):
        initialize_config_directory(directory)
        selected_workspace = Workspace(workspace)
        host = ApplicationHost(
            selected_workspace,
            JsonlSessionStore(directory / "sessions"),
            SettingsStore(directory),
        )
        print(f"Workspace: {selected_workspace.path}")
        asyncio.run(host.serve(http=http))


def _interrupt(_signum: int, _frame: object) -> None:
    # Use asyncio.run's SIGINT handler so it cancels the host task first,
    # allowing adapters to finish shutdown before the event loop closes.
    signal.raise_signal(signal.SIGINT)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="nosis-server")
    parser.add_argument("--config-directory", type=Path, default=default_config_directory())
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--no-http", action="store_true", help="Run without the HTTP / WebSocket adapter")
    args = parser.parse_args(argv)
    signals = [signal.SIGTERM]
    if os.name == "nt":
        signals.append(signal.SIGBREAK)
    handlers = {sig: signal.signal(sig, _interrupt) for sig in signals}
    try:
        run_application_host(args.workspace, args.config_directory, http=not args.no_http)
    except KeyboardInterrupt:
        pass
    except (OSError, ValueError, RuntimeError, ImportError) as error:
        raise SystemExit(f"Failed to start Nosis server: {error}") from error
    finally:
        for sig, handler in handlers.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    main()
