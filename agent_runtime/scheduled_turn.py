"""An occurrence submitted to its Session Runtime."""

from concurrent.futures import Future
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


@dataclass
class ScheduledTurn:
    session_id: str
    workspace: Path
    turn_id: str
    text: str
    started: Callable[[], None]
    completion: Future[None] = field(default_factory=Future)
