"""Platform computer capabilities assembled by the application Runtime."""

from __future__ import annotations

import re
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from mss import mss
from PIL import Image

from agent_core import JSONValue, Workspace


@dataclass(frozen=True)
class _Screen:
    left: int
    top: int
    width: int
    height: int
    primary: bool = False

    @classmethod
    def from_monitor(cls, monitor: dict[str, Any]) -> "_Screen":
        screen = cls(
            left=int(monitor["left"]),
            top=int(monitor["top"]),
            width=int(monitor["width"]),
            height=int(monitor["height"]),
            primary=bool(monitor.get("is_primary", False)),
        )
        if screen.width <= 0 or screen.height <= 0:
            raise RuntimeError("Desktop contains a display with invalid dimensions.")
        return screen

    def contains(self, x: int, y: int) -> bool:
        return (
            self.left <= x < self.left + self.width
            and self.top <= y < self.top + self.height
        )


@dataclass(frozen=True)
class _DesktopLayout:
    bounds: _Screen
    screens: tuple[_Screen, ...]

    @classmethod
    def from_monitors(cls, monitors: list[dict[str, Any]]) -> "_DesktopLayout":
        if not monitors:
            raise RuntimeError(
                "No local desktop is available. Start Nosis in a graphical desktop session."
            )
        bounds = _Screen.from_monitor(monitors[0])
        screens = tuple(_Screen.from_monitor(monitor) for monitor in monitors[1:])
        return cls(bounds=bounds, screens=screens or (bounds,))

    def input_point(self, x: int, y: int) -> tuple[int, int]:
        if not 0 <= x < self.bounds.width or not 0 <= y < self.bounds.height:
            raise ValueError(
                f"coordinates must be inside the latest screenshot "
                f"({self.bounds.width}x{self.bounds.height})"
            )
        input_x = self.bounds.left + x
        input_y = self.bounds.top + y
        if not any(screen.contains(input_x, input_y) for screen in self.screens):
            raise ValueError("coordinates fall outside every display in the virtual desktop")
        return input_x, input_y

    def interior_point(self, x: int, y: int) -> tuple[int, int]:
        screen = next(
            (candidate for candidate in self.screens if candidate.contains(x, y)),
            None,
        )
        if screen is None or screen.width < 3 or screen.height < 3:
            raise RuntimeError("cannot move the pointer away from the desktop fail-safe")
        return (
            min(max(x, screen.left + 1), screen.left + screen.width - 2),
            min(max(y, screen.top + 1), screen.top + screen.height - 2),
        )


class DesktopComputer:
    """One checked coordinate space shared by desktop screenshots and input."""

    def __init__(
        self,
        workspace: Workspace,
        *,
        input_enabled: bool = True,
        _screenshot_factory: Callable[[], Any] = mss,
        _pyautogui: Any | None = None,
    ) -> None:
        self._workspace = workspace
        self._screenshot_factory = _screenshot_factory
        self._lock = threading.RLock()
        self._layout: _DesktopLayout | None = None
        self._source_sizes: tuple[tuple[int, int], ...] | None = None
        self._pyautogui: Any | None = None
        self._held_buttons: set[str] = set()
        self._last_pointer_position: tuple[int, int] | None = None
        self._pending_clipboard: str | None = None

        # MSS establishes the native desktop coordinate space on Windows. It
        # must run before PyAutoGUI imports its older process-DPI setup.
        _, layout, source_sizes = self._capture_image()
        self._layout = layout
        self._source_sizes = source_sizes

        if input_enabled:
            if _pyautogui is None:
                import pyautogui

                _pyautogui = pyautogui
            self._pyautogui = _pyautogui
            self._check_input_geometry()
            self._check_layout()

    def capture(self, label: str) -> Path:
        with self._lock:
            image, _, _ = self._capture_image()
            self._check_input_geometry()
            return self._save(image, label)

    def control(self, arguments: dict[str, JSONValue]) -> Path:
        with self._lock:
            pyautogui = self._pyautogui
            if pyautogui is None:
                raise RuntimeError("computer input is unavailable")

            # A paste key event is queued asynchronously by the OS. Restore the
            # old clipboard only on the next action, after the target app had a
            # full model turn to consume it.
            self._restore_pending_clipboard()

            # Capture first so a display layout or scale change cannot silently
            # reuse coordinates from an older screenshot.
            self._capture_image()
            self._check_input_geometry()
            action = arguments.get("action")
            if action in {
                "click",
                "move",
                "drag",
                "mouse_down",
                "mouse_up",
                "type",
                "key",
                "hotkey",
                "scroll",
            }:
                self._recover_owned_failsafe(pyautogui)
                self._last_pointer_position = None
            if action in {"click", "move", "mouse_down", "mouse_up", "scroll"}:
                x, y = self._input_coordinates(arguments, "x", "y")
                if action == "click":
                    button = _button(arguments)
                    clicks = arguments.get("clicks", 1)
                    if (
                        not isinstance(clicks, int)
                        or isinstance(clicks, bool)
                        or not 1 <= clicks <= 3
                    ):
                        raise ValueError("click requires clicks between 1 and 3")
                    pyautogui.click(
                        x=x,
                        y=y,
                        button=button,
                        clicks=clicks,
                        duration=_duration(arguments, default=0.0),
                    )
                elif action == "move":
                    duration = _duration(arguments, default=0.0)
                    if len(self._held_buttons) == 1:
                        pyautogui.dragTo(
                            x,
                            y,
                            duration=duration,
                            button=next(iter(self._held_buttons)),
                            mouseDownUp=False,
                        )
                    elif self._held_buttons:
                        raise RuntimeError(
                            "move is ambiguous while multiple mouse buttons are held"
                        )
                    else:
                        pyautogui.moveTo(x, y, duration=duration)
                elif action == "mouse_down":
                    button = _button(arguments)
                    if button in self._held_buttons:
                        raise RuntimeError(f"mouse button is already held: {button}")
                    pyautogui.mouseDown(
                        x=x,
                        y=y,
                        button=button,
                        duration=_duration(arguments, default=0.0),
                    )
                    self._held_buttons.add(button)
                elif action == "mouse_up":
                    button = _button(arguments)
                    duration = _duration(arguments, default=0.0)
                    if button in self._held_buttons:
                        try:
                            pyautogui.dragTo(
                                x,
                                y,
                                duration=duration,
                                button=button,
                                mouseDownUp=False,
                            )
                        finally:
                            _release_mouse_button(pyautogui, button, x, y)
                            self._held_buttons.discard(button)
                    else:
                        pyautogui.moveTo(x, y, duration=duration)
                        _release_mouse_button(pyautogui, button, x, y)
                elif action == "scroll":
                    amount = arguments.get("amount")
                    if (
                        not isinstance(amount, int)
                        or isinstance(amount, bool)
                        or not -20 <= amount <= 20
                        or amount == 0
                    ):
                        raise ValueError(
                            "scroll requires a non-zero amount between -20 and 20"
                        )
                    pyautogui.moveTo(
                        x, y, duration=_duration(arguments, default=0.0)
                    )
                    _scroll_at(pyautogui, amount, x, y)
                self._last_pointer_position = (x, y)
            elif action == "drag":
                if self._held_buttons:
                    raise RuntimeError(
                        "drag is unavailable while a mouse button is already held"
                    )
                start_x, start_y = self._input_coordinates(arguments, "x", "y")
                end_x, end_y = self._input_coordinates(arguments, "to_x", "to_y")
                button = _button(arguments)
                pyautogui.moveTo(start_x, start_y)
                _press_mouse_button(pyautogui, button, start_x, start_y)
                self._held_buttons.add(button)
                try:
                    if _is_failsafe_point(pyautogui, (start_x, start_y)):
                        safe_x, safe_y = self._required_layout().interior_point(
                            start_x, start_y
                        )
                        _drag_pointer_direct(
                            pyautogui, safe_x, safe_y, button
                        )
                    pyautogui.dragTo(
                        end_x,
                        end_y,
                        duration=_duration(arguments, default=0.5),
                        button=button,
                        mouseDownUp=False,
                    )
                finally:
                    _release_mouse_button(pyautogui, button, end_x, end_y)
                    self._held_buttons.discard(button)
                self._last_pointer_position = (end_x, end_y)
            elif action == "type":
                text = arguments.get("text")
                if not isinstance(text, str):
                    raise ValueError("type requires text")
                self._pending_clipboard = _paste_text(
                    pyautogui, text, restore=False
                )
            elif action == "key":
                key = arguments.get("key")
                if not isinstance(key, str) or not key:
                    raise ValueError("key requires a non-empty key")
                pyautogui.press(*_normalize_hotkey_keys([key], pyautogui))
            elif action == "hotkey":
                keys = arguments.get("keys")
                if (
                    not isinstance(keys, list)
                    or not keys
                    or not all(isinstance(key, str) and key for key in keys)
                ):
                    raise ValueError("hotkey requires a non-empty keys array")
                pyautogui.hotkey(*_normalize_hotkey_keys(keys, pyautogui))
            elif action == "wait":
                seconds = arguments.get("seconds", 1.0)
                if (
                    not isinstance(seconds, (int, float))
                    or isinstance(seconds, bool)
                    or not 0 < seconds <= 10
                ):
                    raise ValueError("wait requires seconds greater than 0 and at most 10")
                time.sleep(seconds)
            else:
                raise ValueError("unsupported computer action")

            image, _, _ = self._capture_image()
            self._check_input_geometry()
            return self._save(image, "after-action")

    def close(self) -> None:
        with self._lock:
            try:
                self._restore_pending_clipboard()
            finally:
                pyautogui = self._pyautogui
                if pyautogui is not None:
                    for button in tuple(self._held_buttons):
                        _release_mouse_button(pyautogui, button)
                self._held_buttons.clear()

    def _restore_pending_clipboard(self) -> None:
        if self._pending_clipboard is None:
            return
        try:
            import pyperclip
        except ImportError as error:
            raise RuntimeError(
                "could not restore the clipboard after computer type"
            ) from error
        try:
            pyperclip.copy(self._pending_clipboard)
        except pyperclip.PyperclipException as error:
            raise RuntimeError(
                "could not restore the clipboard after computer type"
            ) from error
        self._pending_clipboard = None

    def _input_coordinates(
        self,
        arguments: dict[str, JSONValue],
        x_name: str,
        y_name: str,
    ) -> tuple[int, int]:
        layout = self._required_layout()
        return layout.input_point(
            _coordinate(arguments, x_name),
            _coordinate(arguments, y_name),
        )

    def _recover_owned_failsafe(self, pyautogui: Any) -> None:
        current = tuple(int(value) for value in pyautogui.position())
        if current != self._last_pointer_position:
            self._last_pointer_position = None
            return
        if not _is_failsafe_point(pyautogui, current):
            return
        safe_x, safe_y = self._required_layout().interior_point(*current)
        if self._held_buttons:
            if sys.platform == "darwin" and len(self._held_buttons) != 1:
                raise RuntimeError(
                    "cannot recover from fail-safe while multiple mouse buttons are held"
                )
            _drag_pointer_direct(
                pyautogui,
                safe_x,
                safe_y,
                next(iter(self._held_buttons)),
            )
        else:
            _move_pointer_direct(pyautogui, safe_x, safe_y)
        self._last_pointer_position = (safe_x, safe_y)

    def _capture_image(
        self,
    ) -> tuple[Image.Image, _DesktopLayout, tuple[tuple[int, int], ...]]:
        with self._screenshot_factory() as screenshots:
            monitors = [dict(monitor) for monitor in screenshots.monitors]
            layout = _DesktopLayout.from_monitors(monitors)
            self._assert_layout(layout)
            capture_monitors = monitors[1:] or monitors[:1]
            image = Image.new(
                "RGB", (layout.bounds.width, layout.bounds.height), "black"
            )
            source_sizes: list[tuple[int, int]] = []
            for screen, monitor in zip(layout.screens, capture_monitors, strict=True):
                shot = screenshots.grab(monitor)
                source_size = (int(shot.size[0]), int(shot.size[1]))
                source_sizes.append(source_size)
                display = Image.frombytes("RGB", source_size, shot.rgb)
                target_size = (screen.width, screen.height)
                if display.size != target_size:
                    display = display.resize(target_size, Image.Resampling.LANCZOS)
                image.paste(
                    display,
                    (
                        screen.left - layout.bounds.left,
                        screen.top - layout.bounds.top,
                    ),
                )

        sizes = tuple(source_sizes)
        if self._source_sizes is not None and sizes != self._source_sizes:
            raise RuntimeError(
                "Desktop screenshot scale changed. Start a new runtime session."
            )
        if self._read_layout() != layout:
            raise RuntimeError(
                "Desktop layout changed while capturing a screenshot. "
                "Start a new runtime session."
            )
        if image.size != (layout.bounds.width, layout.bounds.height):
            raise RuntimeError("Desktop screenshot and input dimensions do not match.")
        return image, layout, sizes

    def _read_layout(self) -> _DesktopLayout:
        with self._screenshot_factory() as screenshots:
            return _DesktopLayout.from_monitors(
                [dict(monitor) for monitor in screenshots.monitors]
            )

    def _check_layout(self) -> None:
        self._assert_layout(self._read_layout())

    def _assert_layout(self, layout: _DesktopLayout) -> None:
        if self._layout is not None and layout != self._layout:
            raise RuntimeError(
                "Desktop layout or resolution changed. Start a new runtime session."
            )

    def _check_input_geometry(self) -> None:
        pyautogui = self._pyautogui
        if pyautogui is None:
            return
        layout = self._required_layout()
        actual = tuple(int(value) for value in pyautogui.size())
        if sys.platform == "win32":
            primary = next(
                (screen for screen in layout.screens if screen.primary), None
            )
            if primary is None:
                raise RuntimeError("Desktop input could not identify the primary display.")
            expected = (primary.width, primary.height)
        elif sys.platform == "darwin":
            primary = next(
                (screen for screen in layout.screens if screen.contains(0, 0)), None
            )
            if primary is None:
                raise RuntimeError("Desktop input could not identify the main display.")
            expected = (primary.width, primary.height)
        else:
            expected = (layout.bounds.width, layout.bounds.height)
        if actual != expected:
            raise RuntimeError(
                "Desktop screenshot and input coordinate systems disagree: "
                f"screenshot expects {expected[0]}x{expected[1]}, "
                f"input reports {actual[0]}x{actual[1]}."
            )

    def _required_layout(self) -> _DesktopLayout:
        if self._layout is None:  # pragma: no cover - constructor establishes it
            raise RuntimeError("desktop layout is unavailable")
        return self._layout

    def _save(self, image: Image.Image, label: str) -> Path:
        directory = self._workspace.resolve_path(".nosis/attachments")
        directory.mkdir(parents=True, exist_ok=True)
        safe_label = re.sub(r"[^A-Za-z0-9_-]+", "-", label).strip("-")
        if not safe_label:
            safe_label = "computer"
        path = directory / f"{safe_label}-{uuid.uuid4().hex}.png"
        image.save(path, format="PNG")
        return path


def _coordinate(arguments: dict[str, JSONValue], name: str) -> int:
    value = arguments.get(name)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _duration(arguments: dict[str, JSONValue], *, default: float) -> float:
    value = arguments.get("duration", default)
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not 0 <= value <= 10
    ):
        raise ValueError("duration must be between 0 and 10 seconds")
    return float(value)


def _button(arguments: dict[str, JSONValue]) -> str:
    value = arguments.get("button", "left")
    if value not in {"left", "middle", "right"}:
        raise ValueError("button must be left, middle, or right")
    return str(value)


def _is_failsafe_point(pyautogui: Any, point: tuple[int, int]) -> bool:
    points = getattr(pyautogui, "FAILSAFE_POINTS", ())
    if not isinstance(points, (list, tuple, set, frozenset)):
        return False
    return point in {tuple(candidate) for candidate in points}


def _move_pointer_direct(pyautogui: Any, x: int, y: int) -> None:
    move = getattr(pyautogui.platformModule, "_moveTo", None)
    if not callable(move):  # pragma: no cover - all supported backends provide it
        raise RuntimeError("PyAutoGUI mouse movement backend is unavailable")
    move(x, y)


def _drag_pointer_direct(
    pyautogui: Any, x: int, y: int, button: str
) -> None:
    if sys.platform == "darwin":
        drag = getattr(pyautogui.platformModule, "_dragTo", None)
        if not callable(drag):  # pragma: no cover - PyAutoGUI provides it on macOS
            raise RuntimeError("PyAutoGUI mouse drag backend is unavailable")
        drag(x, y, button)
    else:
        _move_pointer_direct(pyautogui, x, y)


def _press_mouse_button(
    pyautogui: Any, button: str, x: int | None = None, y: int | None = None
) -> None:
    if x is None or y is None:
        x, y = pyautogui.position()
    press = getattr(pyautogui.platformModule, "_mouseDown", None)
    if not callable(press):  # pragma: no cover - all supported backends provide it
        raise RuntimeError("PyAutoGUI mouse press backend is unavailable")
    press(x, y, button)


def _release_mouse_button(
    pyautogui: Any, button: str, x: int | None = None, y: int | None = None
) -> None:
    """Release held input during teardown without triggering corner fail-safe."""

    if x is None or y is None:
        x, y = pyautogui.position()
    release = getattr(pyautogui.platformModule, "_mouseUp", None)
    if not callable(release):  # pragma: no cover - all supported backends provide it
        raise RuntimeError("PyAutoGUI mouse release backend is unavailable")
    release(x, y, button)


def _scroll_at(pyautogui: Any, amount: int, x: int, y: int) -> None:
    scroll = getattr(pyautogui.platformModule, "_scroll", None)
    if not callable(scroll):  # pragma: no cover - all supported backends provide it
        raise RuntimeError("PyAutoGUI scroll backend is unavailable")
    scroll(amount * 120 if sys.platform == "win32" else amount, x, y)


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
            raise ValueError(f"unsupported computer key: {key}")
        normalized.append(candidate)
    return tuple(normalized)


def _paste_text(
    pyautogui: Any, text: str, *, restore: bool = True
) -> str | None:
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
    except BaseException:
        if not restore:
            try:
                pyperclip.copy(previous)
            except pyperclip.PyperclipException:
                pass
        raise
    finally:
        if restore:
            try:
                pyperclip.copy(previous)
            except pyperclip.PyperclipException:
                # The text was already delivered; failure to restore must not turn a
                # successful computer action into a failed one.
                pass
    return previous if not restore else None
