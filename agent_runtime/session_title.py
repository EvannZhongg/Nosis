"""One auxiliary naming task owned by the Session's execution plane."""

from functools import partial
import logging
from threading import Thread
from typing import Callable

from agent_core import AgentCancelled, JsonlSessionStore, Session, TurnControl
from agent_core.llm import LLMProvider
from agent_core.session_title import generate_session_title

logger = logging.getLogger(__name__)


class SessionTitleTask:
    def __init__(
        self, session: Session, store: JsonlSessionStore, provider: LLMProvider,
        prompt: str, on_title: Callable[[str, str, str], None],
    ) -> None:
        self._session = session
        self._store = store
        self._provider = provider
        self._prompt = prompt
        self._on_title = on_title
        self._control = TurnControl()
        self._thread: Thread | None = None

    def start(self, start: int, end: int, turn_id: str) -> None:
        if self._thread is not None or any(
            item.role == "assistant" for item in self._session.items[:start]
        ):
            return
        if self._store.title_source_for(self._session.session_id) != "initial":
            return
        messages = tuple(self._session.items[:end])

        def run() -> None:
            try:
                title = generate_session_title(
                    self._provider, self._prompt, messages,
                    self._control.raise_if_cancelled,
                    partial(
                        self._session.auxiliary_model_completed,
                        model=self._provider.model_name, turn_id=turn_id,
                    ),
                )
                self._control.raise_if_cancelled()
                if self._store.set_title(
                    self._session.session_id, title, "generated", expected_source="initial",
                ):
                    self._on_title(self._session.session_id, title, "generated")
            except AgentCancelled:
                pass
            except Exception:
                logger.exception("Session title generation failed for %s", self._session.session_id)

        self._thread = Thread(target=run, name="nosis-session-title", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._control.cancel()
        if self._thread is not None:
            self._thread.join()
