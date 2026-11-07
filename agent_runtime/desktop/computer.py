"""Platform computer capabilities assembled by the application Runtime."""

from __future__ import annotations

import re
import sys
import threading
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from mss import mss
from PIL import Image, ImageChops

from agent_core import JSONValue, Workspace
from agent_core.media import fit_image_size
from agent_core.tools.context import DesktopResult, DesktopWindow

from .pyautogui_input import PyAutoGUIInput
from .windows import WindowsDesktop


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


@dataclass(frozen=True)
class _Observation:
    path: str
    rect: _Screen
    window: DesktopWindow | None
    preview: Image.Image


_POINTER_ACTIONS = {"click", "move", "drag", "mouse_down", "mouse_up", "scroll"}
_KEY_ACTIONS = {"type", "key", "hotkey"}
_HOTKEY_ALIASES = {"control": "ctrl", "ctl": "ctrl"}
_InputBackend = WindowsDesktop | PyAutoGUIInput


class DesktopComputer:
    """One checked coordinate space shared by desktop screenshots and input."""

    def __init__(
        self,
        workspace: Workspace,
        *,
        input_enabled: bool = True,
        _screenshot_factory: Callable[[], Any] = mss,
        _pyautogui: Any | None = None,
        _windows: WindowsDesktop | None = None,
    ) -> None:
        self._workspace = workspace
        self._screenshot_factory = _screenshot_factory
        self._lock = threading.RLock()
        self._layout: _DesktopLayout | None = None
        self._source_sizes: tuple[tuple[int, int], ...] | None = None
        self._observation: _Observation | None = None
        self._input: _InputBackend | None = None
        self._held_buttons: set[str] = set()
        self._last_pointer_position: tuple[int, int] | None = None

        # Establish MSS's desktop DPI context before initializing input.
        _, layout, source_sizes, _ = self._capture_image()
        self._layout = layout
        self._source_sizes = source_sizes
        self._windows = _windows if _windows is not None else (
            WindowsDesktop() if sys.platform == "win32" and _pyautogui is None else None
        )

        if input_enabled:
            if self._windows is not None:
                self._input = self._windows
            else:
                if _pyautogui is None:
                    import pyautogui

                    _pyautogui = pyautogui
                self._input = PyAutoGUIInput(_pyautogui)
            self._check_input_geometry()
            self._check_layout()

    def capture(self, label: str, selector: str | None = None) -> DesktopResult:
        with self._lock:
            window = self._resolve_window(selector)
            image, _, _, rect = self._capture_image(window)
            self._check_input_geometry()
            return self._save_observation(image, label, rect, _preview(image), DesktopResult(window=window))

    def control(self, arguments: dict[str, JSONValue]) -> DesktopResult:
        with self._lock:
            backend = self._input
            if backend is None:
                raise RuntimeError("computer input is unavailable")

            screenshot = arguments.get("screenshot", True)
            if not isinstance(screenshot, bool):
                raise ValueError("screenshot must be a boolean")
            window = self._resolve_window(arguments.get("window"))
            observation = self._referenced_observation(arguments.get("observation"), window)
            before, _, _, rect = self._capture_image(window)
            self._check_input_geometry()
            if observation is not None and rect != observation.rect:
                raise ValueError("Window geometry changed. Observe the window again before sending input.")
            before_preview = _preview(before) if observation is not None or screenshot else None
            del before
            result = DesktopResult(window=window)
            if observation is not None:
                assert before_preview is not None
                result = replace(
                    result, observation_changed=_visually_changed(observation.preview, before_preview)
                )

            action = arguments.get("action")
            if action in _POINTER_ACTIONS or action in _KEY_ACTIONS:
                backend.release_pending_input()
                self._recover_owned_failsafe(backend)
            if action in _POINTER_ACTIONS:
                backend.check_failsafe()
                point = self._input_coordinates(arguments, rect, "x", "y")
                duration = _duration(arguments, default=0.5 if action == "drag" else 0.0)
                destination = (
                    self._input_coordinates(arguments, rect, "to_x", "to_y")
                    if action == "drag" else None
                )
                foreground_required = action == "scroll" and window is not None
                result = self._pointer_target(result, rect, point, destination, foreground_required)
                if window is not None and duration and action in {"click", "mouse_down", "scroll"}:
                    self._move(backend, point, duration)
                    duration = 0.0
                    result = self._pointer_target(result, rect, point, None, foreground_required)
                self._perform_pointer_action(backend, action, arguments, point, destination, duration)
            elif action in _KEY_ACTIONS:
                if window is not None:
                    self._require_current_rect(window, rect)
                foreground = self._windows.foreground() if self._windows else None
                if window is not None:
                    _require_window(window, foreground, "foreground")
                result = replace(result, foreground_before=foreground)
                if action == "type":
                    text = arguments.get("text")
                    if not isinstance(text, str):
                        raise ValueError("type requires text")
                    backend.type_text(text)
                else:
                    keys = [arguments.get("key")] if action == "key" else arguments.get("keys")
                    if not isinstance(keys, list) or not keys or not all(
                        isinstance(key, str) and key for key in keys
                    ):
                        raise ValueError(f"{action} requires a non-empty key or keys array")
                    backend.keys(tuple(_HOTKEY_ALIASES.get(key.casefold(), key.casefold()) for key in keys))
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

            if self._windows is not None and action != "wait":
                result = replace(result, foreground_after=self._windows.foreground())
            if not screenshot:
                return result
            try:
                after, _, _, after_rect = self._capture_image(window)
                self._check_input_geometry()
                after_preview = _preview(after)
                assert before_preview is not None
                if rect == after_rect:
                    result = replace(result, screen_changed=_visually_changed(before_preview, after_preview))
                return self._save_observation(after, "after-action", after_rect, after_preview, result)
            except (OSError, ValueError, RuntimeError) as error:
                return replace(result, observation_error=f"Action ran, but the screenshot failed: {error}")

    def _perform_pointer_action(
        self,
        backend: _InputBackend,
        action: str,
        arguments: dict[str, JSONValue],
        point: tuple[int, int],
        destination: tuple[int, int] | None,
        duration: float,
    ) -> None:
        x, y = point
        if action == "click":
            button = _button(arguments)
            clicks = arguments.get("clicks", 1)
            if not isinstance(clicks, int) or isinstance(clicks, bool) or not 1 <= clicks <= 3:
                raise ValueError("click requires clicks between 1 and 3")
            if button in self._held_buttons:
                raise RuntimeError(f"mouse button is already held: {button}")
            self._last_pointer_position = point
            backend.click(x, y, button, clicks, duration)
        elif action == "move":
            if len(self._held_buttons) > 1:
                raise RuntimeError("move is ambiguous while multiple mouse buttons are held")
            self._move(backend, point, duration, next(iter(self._held_buttons), None))
        elif action == "mouse_down":
            button = _button(arguments)
            if button in self._held_buttons:
                raise RuntimeError(f"mouse button is already held: {button}")
            self._last_pointer_position = point
            backend.button_down(x, y, button, duration)
            self._held_buttons.add(button)
        elif action == "mouse_up":
            button = _button(arguments)
            if button in self._held_buttons:
                try:
                    self._move(backend, point, duration, button)
                finally:
                    backend.button_up(button)
                    self._held_buttons.remove(button)
            else:
                raise RuntimeError(f"mouse button is not held: {button}")
        elif action == "scroll":
            amount = arguments.get("amount")
            if not isinstance(amount, int) or isinstance(amount, bool) or not -20 <= amount <= 20 or not amount:
                raise ValueError("scroll requires a non-zero amount between -20 and 20")
            self._last_pointer_position = point
            backend.scroll(x, y, amount, duration)
        elif action == "drag":
            assert destination is not None
            if self._held_buttons:
                raise RuntimeError("drag is unavailable while a mouse button is already held")
            button = _button(arguments)
            self._last_pointer_position = point
            backend.button_down(x, y, button, 0.0)
            self._held_buttons.add(button)
            try:
                self._recover_owned_failsafe(backend)
                self._move(backend, destination, duration, button)
            finally:
                backend.button_up(button)
                self._held_buttons.remove(button)

    def _pointer_target(
        self,
        result: DesktopResult,
        rect: _Screen,
        point: tuple[int, int],
        destination: tuple[int, int] | None,
        foreground_required: bool,
    ) -> DesktopResult:
        if self._windows is None:
            return result
        window = result.window
        at_point = self._windows.window_at(*point)
        at_destination = self._windows.window_at(*destination) if destination is not None else None
        foreground = self._windows.foreground() if foreground_required else None
        if window is not None:
            self._require_current_rect(window, rect)
            _require_window(window, at_point, "pointer location")
            capture = self._windows.pointer_capture()
            if capture is not None:
                _require_window(window, capture, "mouse capture")
            if destination is not None:
                _require_window(window, at_destination, "drag destination")
            if foreground_required:
                _require_window(window, foreground, "foreground")
        return replace(
            result, window_at_point=at_point, window_at_destination=at_destination,
            foreground_before=foreground,
        )

    def _require_current_rect(self, window: DesktopWindow, rect: _Screen) -> None:
        assert self._windows is not None
        if _Screen(*self._windows.window_rect(window)) != rect:
            raise ValueError("Window geometry changed. Observe the window again before sending input.")

    def _resolve_window(self, selector: JSONValue) -> DesktopWindow | None:
        if selector is None:
            return None
        if self._windows is None or not isinstance(selector, str) or not selector.strip():
            raise ValueError("window requires a supported, non-empty window selector")
        return self._windows.resolve_window(selector.strip())

    def _referenced_observation(
        self, path: JSONValue, window: DesktopWindow | None
    ) -> _Observation | None:
        if path is None:
            if window is not None:
                raise ValueError("Window-scoped actions require the latest screenshot path as observation.")
            return None
        observation = self._observation
        if observation is None or path != observation.path:
            raise ValueError("Observation is no longer current. Take a new screenshot.")
        if not _same_window(window, observation.window):
            raise ValueError("Observation belongs to a different coordinate scope. Take a new screenshot.")
        return observation

    def _input_coordinates(
        self, arguments: dict[str, JSONValue], rect: _Screen, x_name: str, y_name: str
    ) -> tuple[int, int]:
        return _DesktopLayout(rect, self._required_layout().screens).input_point(
            _coordinate(arguments, x_name), _coordinate(arguments, y_name)
        )

    def _move(
        self, backend: _InputBackend, point: tuple[int, int], duration: float, held: str | None = None
    ) -> None:
        self._last_pointer_position = point
        backend.move(*point, duration, held)

    def _recover_owned_failsafe(self, backend: _InputBackend) -> None:
        current = backend.position()
        if current != self._last_pointer_position:
            self._last_pointer_position = None
            return
        if current in backend.failsafe_points:
            safe_x, safe_y = self._required_layout().interior_point(*current)
            if sys.platform == "darwin" and len(self._held_buttons) > 1:
                raise RuntimeError("cannot recover from fail-safe while multiple mouse buttons are held")
            backend.move_direct(safe_x, safe_y, next(iter(self._held_buttons), None))
            self._last_pointer_position = (safe_x, safe_y)

    def release_input(self) -> None:
        with self._lock:
            backend = self._input
            if backend is None:
                return
            errors: list[Exception] = []
            for button in tuple(self._held_buttons):
                try:
                    backend.button_up(button)
                except Exception as error:
                    errors.append(error)
                else:
                    self._held_buttons.remove(button)
            try:
                backend.release_pending_input()
            except Exception as error:
                errors.append(error)
            if errors:
                raise RuntimeError(", ".join(str(error) for error in errors))

    def clear_observation(self) -> None:
        with self._lock:
            self._observation = None

    def _capture_image(
        self, window: DesktopWindow | None = None,
    ) -> tuple[Image.Image, _DesktopLayout, tuple[tuple[int, int], ...], _Screen]:
        with self._screenshot_factory() as screenshots:
            monitors = [dict(monitor) for monitor in screenshots.monitors]
            layout = _DesktopLayout.from_monitors(monitors)
            self._assert_layout(layout)
            if window is not None:
                assert self._windows is not None
                rect = _Screen(*self._windows.window_rect(window))
                right, bottom = rect.left + rect.width - 1, rect.top + rect.height - 1
                if rect.width <= 0 or rect.height <= 0 or not any(
                    screen.contains(rect.left, rect.top) and screen.contains(right, bottom)
                    for screen in layout.screens
                ):
                    raise ValueError("Window must be fully visible within a single display rectangle.")
                shot = screenshots.grab({
                    "left": rect.left, "top": rect.top, "width": rect.width, "height": rect.height,
                })
                if shot.size != (rect.width, rect.height):
                    raise RuntimeError("Window screenshot and input dimensions do not match.")
                if self._read_layout() != layout or _Screen(*self._windows.window_rect(window)) != rect:
                    raise RuntimeError("Window or display geometry changed during capture. Observe it again.")
                image = Image.frombytes("RGB", shot.size, shot.rgb)
                return image, layout, (), rect

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
        return image, layout, sizes, layout.bounds

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
        if self._input is None:
            return
        layout = self._required_layout()
        actual = self._input.size()
        if sys.platform == "win32":
            bounds = layout.bounds
            if self._windows is not None and self._windows.virtual_bounds() != (
                bounds.left, bounds.top, bounds.width, bounds.height,
            ):
                raise RuntimeError("Desktop screenshot and Windows virtual input bounds disagree.")
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

    def _save_observation(
        self, image: Image.Image, label: str, rect: _Screen, preview: Image.Image, result: DesktopResult
    ) -> DesktopResult:
        path = self._save(image, label)
        self._observation = _Observation(
            path.relative_to(self._workspace.path).as_posix(), rect, result.window, preview
        )
        return replace(result, screenshot=path, origin=(rect.left, rect.top))

    def _save(self, image: Image.Image, label: str) -> Path:
        directory = self._workspace.resolve_path(".nosis/attachments")
        directory.mkdir(parents=True, exist_ok=True)
        safe_label = re.sub(r"[^A-Za-z0-9_-]+", "-", label).strip("-")
        if not safe_label:
            safe_label = "computer"
        path = directory / f"{safe_label}-{uuid.uuid4().hex}.png"
        image.save(path, format="PNG")
        return path


class DesktopCapability:
    """Initialize desktop capture and the platform input backend on first use."""

    def __init__(self, workspace: Workspace, *, input_enabled: bool) -> None:
        self._workspace = workspace
        self._input_enabled = input_enabled
        self._lock = threading.Lock()
        self._computer: DesktopComputer | None = None

    @property
    def supports_windows(self) -> bool:
        return sys.platform == "win32"

    def capture(self, label: str, window: str | None = None) -> DesktopResult:
        return self._desktop().capture(label, window)

    def control(self, arguments: dict[str, JSONValue]) -> DesktopResult:
        return self._desktop().control(arguments)

    def release_input(self) -> None:
        with self._lock:
            if self._computer is not None:
                self._computer.release_input()

    def clear_observation(self) -> None:
        with self._lock:
            if self._computer is not None:
                self._computer.clear_observation()

    def close(self) -> None:
        with self._lock:
            computer, self._computer = self._computer, None
        if computer is not None:
            computer.release_input()

    def _desktop(self) -> DesktopComputer:
        with self._lock:
            if self._computer is None:
                self._computer = DesktopComputer(
                    self._workspace,
                    input_enabled=self._input_enabled,
                )
            return self._computer


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


def _same_window(a: DesktopWindow | None, b: DesktopWindow | None) -> bool:
    if a is None or b is None:
        return a is b
    return (a.hwnd, a.pid) == (b.hwnd, b.pid)


def _require_window(expected: DesktopWindow, actual: DesktopWindow | None, location: str) -> None:
    if not _same_window(expected, actual):
        found = f"hwnd:{actual.hwnd} {actual.title}" if actual else "unknown"
        raise ValueError(f"Expected hwnd:{expected.hwnd} at {location}, found {found}. Observe again.")


def _preview(image: Image.Image) -> Image.Image:
    size = fit_image_size(*image.size, 256)
    return image if image.size == size else image.resize(size, Image.Resampling.BOX)


def _visually_changed(before: Image.Image, after: Image.Image) -> bool:
    # Count preview pixels whose largest channel difference exceeds 10.
    red, green, blue = ImageChops.difference(before, after).split()
    delta = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    return sum(delta.histogram()[11:]) > delta.width * delta.height * 0.0005
