"""File-write calls shared by permission and Bridge protocol tests."""

from agent_core import ToolCall


def patch_text(body: str) -> str:
    return f"*** Begin Patch\n{body}\n*** End Patch"


def write_call(name: str) -> ToolCall:
    arguments = {
        "apply_patch": {"patch": patch_text("*** Update File: file.txt\n@@\n-old\n+new")},
        "edit_file": {"path": "file.txt", "old_text": "old", "new_text": "new"},
        "write_file": {"path": "file.txt", "content": "new\n", "overwrite": True},
    }
    return ToolCall("write", name, arguments[name])
