"""Desktop input for platforms that use PyAutoGUI."""

import sys
import time
from collections.abc import Callable


class PyAutoGUIInput:
    def __init__(self, pyautogui):
        self._api = pyautogui
        self._restore: Callable[[], None] | None = None

    def size(self) -> tuple[int, int]:
        return tuple(self._api.size())

    def position(self) -> tuple[int, int]:
        return tuple(self._api.position())

    @property
    def failsafe_points(self):
        return self._api.FAILSAFE_POINTS

    def check_failsafe(self) -> None:
        self._api.failSafeCheck()

    def move_direct(self, x: int, y: int, held: str | None = None) -> None:
        if held is not None and sys.platform == "darwin":
            self._api.platformModule._dragTo(x, y, held)
        else:
            self._api.platformModule._moveTo(x, y)

    def move(self, x: int, y: int, duration: float, held: str | None = None) -> None:
        if held is None:
            self._api.moveTo(x, y, duration=duration)
        else:
            self._api.dragTo(x, y, duration=duration, button=held, mouseDownUp=False)

    def click(self, x: int, y: int, button: str, clicks: int, duration: float) -> None:
        self._api.click(x=x, y=y, button=button, clicks=clicks, duration=duration)

    def button_down(self, x: int, y: int, button: str, duration: float) -> None:
        self._api.mouseDown(x=x, y=y, button=button, duration=duration)

    def button_up(self, button: str) -> None:
        # Release at the current cursor even when a corner aborted the action.
        self._api.platformModule._mouseUp(*self.position(), button)

    def scroll(self, x: int, y: int, amount: int, duration: float) -> None:
        self.move(x, y, duration)
        self._api.platformModule._scroll(amount, x, y)

    def keys(self, keys: tuple[str, ...]) -> None:
        mapping = self._api.platformModule.keyboardMapping
        for key in keys:
            if mapping.get(key) is None:
                raise ValueError(f"unsupported computer key: {key}")
        if len(keys) == 1:
            self._api.press(keys[0])
        else:
            self._api.hotkey(*keys)

    def type_text(self, text: str) -> None:
        if sys.platform == "darwin":
            self._restore = _mac_clipboard(text)
        else:
            import pyperclip

            previous = pyperclip.paste()
            pyperclip.copy(text)

            def restore() -> None:
                if pyperclip.paste() == text:
                    pyperclip.copy(previous)

            self._restore = restore

        try:
            self._api.hotkey("command" if sys.platform == "darwin" else "ctrl", "v")
            # Give the queued paste a chance to consume its contents.
            time.sleep(0.05)
        except BaseException:
            self.release_pending_input()
            raise

    def release_pending_input(self) -> None:
        restore, self._restore = self._restore, None
        if restore is not None:
            restore()


def _mac_clipboard(text: str) -> Callable[[], None]:
    from AppKit import NSPasteboard, NSPasteboardItem, NSPasteboardTypeString

    board = NSPasteboard.generalPasteboard()
    before = board.changeCount()
    items = board.pasteboardItems()
    if items is None:
        raise RuntimeError("Could not read the macOS clipboard.")
    snapshot = []
    for source in items:
        item = NSPasteboardItem.alloc().init()
        for kind in source.types():
            data = source.dataForType_(kind)
            if data is None or not item.setData_forType_(data, kind):
                raise RuntimeError("Could not preserve a macOS clipboard format.")
        snapshot.append(item)
    if board.changeCount() != before:
        raise RuntimeError("The macOS clipboard changed while preparing to paste.")

    owner = board.clearContents()

    def restore() -> None:
        if board.changeCount() == owner:
            board.clearContents()
            if snapshot and not board.writeObjects_(snapshot):
                raise RuntimeError("Could not restore the macOS clipboard.")

    try:
        if not board.setString_forType_(text, NSPasteboardTypeString):
            raise RuntimeError("Could not write text to the macOS clipboard.")
    except BaseException:
        restore()
        raise
    return restore
