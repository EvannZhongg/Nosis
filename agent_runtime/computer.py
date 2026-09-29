"""Platform computer capabilities assembled by the application Runtime."""

from __future__ import annotations

import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from mss import mss
from mss.tools import to_png

from agent_core import JSONValue, Workspace


def desktop_screenshot_capture(workspace: Workspace) -> Callable[[str], Path]:
    """Build the cross-platform desktop screenshot dependency for Core."""

    def capture(label: str) -> Path:
        directory = workspace.resolve_path(".nosis/attachments")
        directory.mkdir(parents=True, exist_ok=True)
        safe_label = re.sub(r"[^A-Za-z0-9_-]+", "-", label).strip("-")
        if not safe_label:
            safe_label = "computer"
        path = directory / f"{safe_label}-{uuid.uuid4().hex}.png"
        with mss() as screenshots:
            shot = screenshots.grab(screenshots.monitors[0])
            to_png(shot.rgb, shot.size, output=str(path))
        return path

    return capture


def desktop_computer_control(
    workspace: Workspace,
) -> Callable[[dict[str, JSONValue]], Path]:
    """Build a cross-platform mouse/keyboard adapter with screenshot output."""

    screenshot = desktop_screenshot_capture(workspace)

    def control(arguments: dict[str, JSONValue]) -> Path:
        import pyautogui

        action = arguments.get("action")
        if action in {"click", "move"}:
            x, y = _coordinate(arguments, "x"), _coordinate(arguments, "y")
            if action == "move":
                pyautogui.moveTo(x, y)
            else:
                button = arguments.get("button", "left")
                clicks = arguments.get("clicks", 1)
                if button not in {"left", "middle", "right"} or not isinstance(clicks, int) or isinstance(clicks, bool) or not 1 <= clicks <= 3:
                    raise ValueError("click requires a valid button and clicks")
                pyautogui.click(x=x, y=y, button=button, clicks=clicks)
        elif action == "type":
            text = arguments.get("text")
            if not isinstance(text, str):
                raise ValueError("type requires text")
            _paste_text(pyautogui, text)
        elif action == "key":
            key = arguments.get("key")
            if not isinstance(key, str) or not key:
                raise ValueError("key requires a non-empty key")
            pyautogui.press(*_normalize_hotkey_keys([key], pyautogui))
        elif action == "hotkey":
            keys = arguments.get("keys")
            if not isinstance(keys, list) or not keys or not all(isinstance(key, str) and key for key in keys):
                raise ValueError("hotkey requires a non-empty keys array")
            pyautogui.hotkey(*_normalize_hotkey_keys(keys, pyautogui))
        elif action == "scroll":
            amount = arguments.get("amount")
            if not isinstance(amount, int) or isinstance(amount, bool) or not -20 <= amount <= 20 or amount == 0:
                raise ValueError("scroll requires a non-zero amount between -20 and 20")
            pyautogui.scroll(amount)
        else:
            raise ValueError("unsupported computer action")
        return screenshot("after-action")

    return control


def _coordinate(arguments: dict[str, JSONValue], name: str) -> int:
    value = arguments.get(name)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


_HOTKEY_ALIASES = {
    "control": "ctrl",
    "ctl": "ctrl",
}


def _normalize_hotkey_keys(keys: list[str], pyautogui: Any) -> tuple[str, ...]:
    """Map common key spellings to PyAutoGUI names and reject unknown keys."""

    normalized: list[str] = []
    mapping = getattr(getattr(pyautogui, "platformModule", None), "keyboardMapping", None)
    if isinstance(mapping, dict):
        supported = {key for key, code in mapping.items() if code is not None}
    else:
        try:
            supported = set(getattr(pyautogui, "KEYBOARD_KEYS", ()))
        except TypeError:
            # Test doubles and older wrappers may not expose the key catalog.
            supported = None
    for key in keys:
        candidate = key.lower() if len(key) == 1 else key.casefold()
        candidate = _HOTKEY_ALIASES.get(candidate, candidate)
        if supported is not None and candidate not in supported:
            raise ValueError(f"hotkey contains unsupported key: {key}")
        normalized.append(candidate)
    return tuple(normalized)


def _paste_text(pyautogui: Any, text: str) -> None:
    """Insert text as one committed paste, so an active IME cannot compose it."""

    try:
        import pyperclip
    except ImportError as error:  # pragma: no cover - dependency is installed with pyautogui
        raise RuntimeError("computer type requires the pyperclip clipboard backend") from error

    try:
        previous = pyperclip.paste()
        pyperclip.copy(text)
    except pyperclip.PyperclipException as error:
        raise RuntimeError(
            "computer type requires an available system clipboard backend"
        ) from error

    try:
        pyautogui.hotkey("command" if sys.platform == "darwin" else "ctrl", "v")
        # Some applications read the clipboard after the key event returns.
        time.sleep(0.05)
    finally:
        try:
            pyperclip.copy(previous)
        except pyperclip.PyperclipException:
            # The text was already delivered; failure to restore must not turn a
            # successful computer action into a failed one.
            pass
