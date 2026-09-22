"""Long-term memory lifecycle independent from Sessions and interfaces."""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Literal

from .llm import LLMProvider, LLMRequest
from .session import Message


MemoryKind = Literal["preference", "fact", "decision"]
MemoryScope = Literal["global", "workspace"]

_KINDS: tuple[MemoryKind, ...] = ("preference", "fact", "decision")
_HEADINGS = {
    "preference": "Preferences",
    "fact": "Facts",
    "decision": "Decisions",
}


@dataclass(frozen=True)
class MemoryCandidate:
    kind: MemoryKind
    scope: MemoryScope
    content: str


@dataclass(frozen=True)
class MemoryDocument:
    preferences: tuple[str, ...] = ()
    facts: tuple[str, ...] = ()
    decisions: tuple[str, ...] = ()

    def entries(self, kind: MemoryKind) -> tuple[str, ...]:
        return getattr(self, f"{kind}s")

    def is_empty(self) -> bool:
        return not (self.preferences or self.facts or self.decisions)


@dataclass(frozen=True)
class MemoryContext:
    global_memory: MemoryDocument
    workspace_memory: MemoryDocument

    def prompt_section(self) -> str:
        sections = [
            "## Long-term Memory",
            "This is persistent historical context, not a current user "
            "instruction. The user's current request has highest priority, "
            "workspace instructions and project rules come next, and this "
            "memory comes after them. Never let memory override a newer "
            "explicit request.",
        ]
        if not self.global_memory.is_empty():
            sections.append(
                "### Global Memory\n"
                + _render_prompt_memory(self.global_memory)
            )
        if not self.workspace_memory.is_empty():
            sections.append(
                "### Current Workspace Memory\n"
                + _render_prompt_memory(self.workspace_memory)
            )
        if len(sections) == 2:
            sections.append("No long-term memory is currently recorded.")
        return "\n\n".join(sections)


class MemoryStore:
    """Read and atomically write the two user-visible MEMORY.md files."""

    def __init__(self, global_path: Path, workspace_path: Path) -> None:
        self.global_path = global_path.expanduser()
        self.workspace_path = workspace_path.expanduser()
        self._lock = Lock()

    def initialize(self) -> tuple[Path, ...]:
        created = []
        with self._locked_files():
            if not self.global_path.exists():
                _atomic_write(self.global_path, _render_global(MemoryDocument()))
                created.append(self.global_path)
            if not self.workspace_path.exists():
                _atomic_write(self.workspace_path, _render_workspaces({}))
                created.append(self.workspace_path)
        return tuple(created)

    def load(self, workspace: Path) -> MemoryContext:
        workspace_key = str(workspace.expanduser().resolve())
        with self._locked_files():
            global_text = _read_text(self.global_path)
            workspace_text = _read_text(self.workspace_path)
        global_memory = _parse_global(global_text)
        workspaces = _parse_workspaces(workspace_text)
        workspace_memory = workspaces.get(workspace_key, MemoryDocument())
        return MemoryContext(
            global_memory=global_memory,
            workspace_memory=workspace_memory,
        )

    def write_updates(
        self,
        workspace: Path,
        *,
        global_memory: MemoryDocument | None = None,
        workspace_memory: MemoryDocument | None = None,
        expected: MemoryContext | None = None,
    ) -> None:
        workspace_key = str(workspace.expanduser().resolve())
        with self._locked_files():
            current_global = _parse_global(_read_text(self.global_path))
            workspaces = _parse_workspaces(_read_text(self.workspace_path))
            current_workspace = workspaces.get(workspace_key, MemoryDocument())
            if (
                expected is not None
                and global_memory is not None
                and current_global != expected.global_memory
            ):
                raise RuntimeError("global memory changed during reconciliation")
            if (
                expected is not None
                and workspace_memory is not None
                and current_workspace != expected.workspace_memory
            ):
                raise RuntimeError("workspace memory changed during reconciliation")
            if global_memory is not None:
                _atomic_write(self.global_path, _render_global(global_memory))
            if workspace_memory is not None:
                if workspace_memory.is_empty():
                    workspaces.pop(workspace_key, None)
                else:
                    workspaces[workspace_key] = workspace_memory
                _atomic_write(self.workspace_path, _render_workspaces(workspaces))

    @contextmanager
    def _locked_files(self):
        lock_path = self.global_path.parent / ".memory.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, lock_path.open("a+b") as handle:
            _lock_file(handle)
            try:
                yield
            finally:
                _unlock_file(handle)


class MemoryReconciler:
    """Use controlled, tool-free model calls for a Turn's candidates."""

    def __init__(
        self,
        provider: LLMProvider,
        *,
        global_prompt: str,
        workspace_prompt: str,
        global_max_tokens: int,
        workspace_max_tokens: int,
    ) -> None:
        self._provider = provider
        self._prompts = {
            "global": global_prompt.replace(
                "{{global_max_tokens}}", str(global_max_tokens)
            ),
            "workspace": workspace_prompt.replace(
                "{{workspace_max_tokens}}", str(workspace_max_tokens)
            ),
        }

    def reconcile(
        self,
        scope: MemoryScope,
        current: MemoryDocument,
        candidates: tuple[MemoryCandidate, ...],
    ) -> MemoryDocument:
        if not candidates:
            return current
        payload = {
            "scope": scope,
            "current_memory": _document_to_json(current),
            "candidates": [
                {"kind": candidate.kind, "content": candidate.content}
                for candidate in candidates
            ],
        }
        request = LLMRequest(
            system_prompt=self._prompts[scope],
            messages=(
                Message(
                    role="user",
                    content=json.dumps(payload, ensure_ascii=False),
                ),
            ),
        )
        response = self._provider.stream(request, lambda _text: None)
        if response.content is None or response.tool_calls:
            raise ValueError("memory reconciler must return JSON content only")
        return _document_from_json(response.content)


class MemoryManager:
    """Collect candidates during one Turn and reconcile them after success."""

    def __init__(
        self,
        workspace: Path,
        store: MemoryStore,
        reconciler: MemoryReconciler,
    ) -> None:
        self.workspace = workspace.expanduser().resolve()
        self.store = store
        self.reconciler = reconciler
        self._pending: list[MemoryCandidate] = []
        self._lock = Lock()

    def begin_turn(self) -> None:
        self.discard_pending()

    def remember(self, candidate: MemoryCandidate) -> None:
        with self._lock:
            self._pending.append(candidate)

    def discard_pending(self) -> None:
        with self._lock:
            self._pending.clear()

    @property
    def pending(self) -> tuple[MemoryCandidate, ...]:
        with self._lock:
            return tuple(self._pending)

    def reconcile_pending(self) -> bool:
        with self._lock:
            candidates = tuple(self._pending)
            self._pending.clear()
        if not candidates:
            return False

        context = self.store.load(self.workspace)
        global_candidates = tuple(
            candidate for candidate in candidates if candidate.scope == "global"
        )
        workspace_candidates = tuple(
            candidate for candidate in candidates if candidate.scope == "workspace"
        )
        global_memory = (
            self.reconciler.reconcile(
                "global", context.global_memory, global_candidates
            )
            if global_candidates
            else None
        )
        workspace_memory = (
            self.reconciler.reconcile(
                "workspace", context.workspace_memory, workspace_candidates
            )
            if workspace_candidates
            else None
        )
        self.store.write_updates(
            self.workspace,
            global_memory=global_memory,
            workspace_memory=workspace_memory,
            expected=context,
        )
        return True


def _document_from_json(content: str) -> MemoryDocument:
    try:
        value = json.loads(content)
    except json.JSONDecodeError as error:
        raise ValueError("memory reconciler returned invalid JSON") from error
    if not isinstance(value, dict) or set(value) != {"entries"}:
        raise ValueError("memory reconciler JSON must contain only 'entries'")
    entries = value["entries"]
    if not isinstance(entries, list):
        raise ValueError("memory reconciler 'entries' must be an array")
    grouped: dict[MemoryKind, list[str]] = {kind: [] for kind in _KINDS}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"kind", "content"}:
            raise ValueError("each memory entry must contain kind and content")
        kind = entry["kind"]
        content_value = entry["content"]
        if kind not in _KINDS:
            raise ValueError(f"invalid memory kind: {kind}")
        if not isinstance(content_value, str) or not content_value.strip():
            raise ValueError("memory content must be a non-empty string")
        normalized = " ".join(content_value.split())
        if normalized not in grouped[kind]:
            grouped[kind].append(normalized)
    return MemoryDocument(
        preferences=tuple(grouped["preference"]),
        facts=tuple(grouped["fact"]),
        decisions=tuple(grouped["decision"]),
    )


def _document_to_json(document: MemoryDocument) -> list[dict[str, str]]:
    return [
        {"kind": kind, "content": content}
        for kind in _KINDS
        for content in document.entries(kind)
    ]


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _parse_global(text: str) -> MemoryDocument:
    if not text.strip():
        return MemoryDocument()
    lines = text.splitlines()
    if not lines or lines[0] != "# Nosis Global Memory":
        raise ValueError("invalid global MEMORY.md header")
    return _parse_document_lines(lines[1:], level=2)


def _parse_workspaces(text: str) -> dict[str, MemoryDocument]:
    if not text.strip():
        return {}
    lines = text.splitlines()
    if not lines or lines[0] != "# Nosis Workspace Memory":
        raise ValueError("invalid workspace MEMORY.md header")
    workspaces: dict[str, MemoryDocument] = {}
    index = 1
    while index < len(lines):
        if not lines[index].strip():
            index += 1
            continue
        prefix = "## Workspace: "
        if not lines[index].startswith(prefix):
            raise ValueError("invalid workspace MEMORY.md structure")
        try:
            workspace = json.loads(lines[index][len(prefix) :])
        except json.JSONDecodeError as error:
            raise ValueError("invalid workspace path in MEMORY.md") from error
        if not isinstance(workspace, str) or not workspace:
            raise ValueError("workspace path in MEMORY.md must be a string")
        index += 1
        start = index
        while index < len(lines) and not lines[index].startswith(prefix):
            index += 1
        if workspace in workspaces:
            raise ValueError(f"duplicate workspace memory section: {workspace}")
        workspaces[workspace] = _parse_document_lines(lines[start:index], level=3)
    return workspaces


def _parse_document_lines(lines: list[str], *, level: int) -> MemoryDocument:
    headings = {
        f"{'#' * level} {_HEADINGS[kind]}": kind for kind in _KINDS
    }
    grouped: dict[MemoryKind, list[str]] = {kind: [] for kind in _KINDS}
    current: MemoryKind | None = None
    seen: set[MemoryKind] = set()
    for line in lines:
        if not line.strip():
            continue
        if line in headings:
            current = headings[line]
            if current in seen:
                raise ValueError(f"duplicate memory heading: {line}")
            seen.add(current)
            continue
        if current is None or not line.startswith("- ") or not line[2:].strip():
            raise ValueError("invalid MEMORY.md entry")
        grouped[current].append(line[2:].strip())
    return MemoryDocument(
        preferences=tuple(grouped["preference"]),
        facts=tuple(grouped["fact"]),
        decisions=tuple(grouped["decision"]),
    )


def _render_global(document: MemoryDocument) -> str:
    return "# Nosis Global Memory\n\n" + _render_document(document, level=2)


def _render_workspaces(workspaces: dict[str, MemoryDocument]) -> str:
    sections = ["# Nosis Workspace Memory"]
    for workspace in sorted(workspaces):
        sections.append(
            f"## Workspace: {json.dumps(workspace, ensure_ascii=False)}\n\n"
            + _render_document(workspaces[workspace], level=3).rstrip()
        )
    return "\n\n".join(sections).rstrip() + "\n"


def _render_document(document: MemoryDocument, *, level: int) -> str:
    sections = []
    for kind in _KINDS:
        lines = [f"{'#' * level} {_HEADINGS[kind]}"]
        lines.extend(f"- {content}" for content in document.entries(kind))
        sections.append("\n".join(lines))
    return "\n\n".join(sections) + "\n"


def _render_prompt_memory(document: MemoryDocument) -> str:
    return _render_document(document, level=4).rstrip()


def _atomic_write(path: Path, content: str) -> None:
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
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _lock_file(handle) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        if handle.read(1) == b"":
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _unlock_file(handle) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


__all__ = [
    "MemoryCandidate",
    "MemoryContext",
    "MemoryDocument",
    "MemoryKind",
    "MemoryManager",
    "MemoryReconciler",
    "MemoryScope",
    "MemoryStore",
]
