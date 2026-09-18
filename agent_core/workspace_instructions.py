from dataclasses import dataclass


@dataclass(frozen=True)
class WorkspaceInstruction:
    label: str
    content: str | None


@dataclass(frozen=True)
class WorkspaceInstructions:
    documents: tuple[WorkspaceInstruction, ...]
    fingerprint: str

    def __bool__(self) -> bool:
        return any(document.content for document in self.documents)


__all__ = ["WorkspaceInstruction", "WorkspaceInstructions"]
