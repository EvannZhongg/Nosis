from hashlib import sha256
from pathlib import Path

from agent_core.workspace import Workspace
from agent_core.workspace_instructions import (
    WorkspaceInstruction,
    WorkspaceInstructions,
)


def load_workspace_instructions(
    config_directory: Path,
    workspace: Workspace,
    workspace_filenames: tuple[str, ...],
) -> WorkspaceInstructions:
    sources = (
        ("~/.nosis/AGENTS.md", config_directory / "AGENTS.md"),
        *(
            (f"<workspace>/{filename}", workspace.resolve_path(filename))
            for filename in workspace_filenames
        ),
    )
    fingerprint = sha256()
    documents = []
    for label, path in sources:
        fingerprint.update(label.encode("utf-8"))
        fingerprint.update(b"\0")
        fingerprint.update(str(path.resolve()).encode("utf-8"))
        fingerprint.update(b"\0")
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            fingerprint.update(b"missing\0")
            documents.append(WorkspaceInstruction(label=label, content=None))
            continue
        fingerprint.update(b"present\0")
        fingerprint.update(raw)
        fingerprint.update(b"\0")
        documents.append(
            WorkspaceInstruction(
                label=label,
                content=raw.decode("utf-8").strip(),
            )
        )
    return WorkspaceInstructions(
        documents=tuple(documents),
        fingerprint=fingerprint.hexdigest(),
    )


__all__ = ["load_workspace_instructions"]
