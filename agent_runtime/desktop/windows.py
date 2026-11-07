"""Native Windows desktop observation and input."""

import ctypes as ct
from ctypes import wintypes as wt
from pathlib import PureWindowsPath
from time import monotonic, sleep

from agent_core.tools.context import DesktopWindow


class _GUIThreadInfo(ct.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD), ("flags", wt.DWORD), ("hwndActive", wt.HWND),
        ("hwndFocus", wt.HWND), ("hwndCapture", wt.HWND), ("hwndMenuOwner", wt.HWND),
        ("hwndMoveSize", wt.HWND), ("hwndCaret", wt.HWND), ("rcCaret", wt.RECT),
    ]


class _MouseInput(ct.Structure):
    _fields_ = [
        ("dx", ct.c_int32), ("dy", ct.c_int32), ("mouseData", ct.c_uint32),
        ("dwFlags", ct.c_uint32), ("time", ct.c_uint32), ("dwExtraInfo", ct.c_size_t),
    ]


class _KeyboardInput(ct.Structure):
    _fields_ = [
        ("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", ct.c_uint32),
        ("time", ct.c_uint32), ("dwExtraInfo", ct.c_size_t),
    ]


class _InputData(ct.Union):
    _fields_ = [("mouse", _MouseInput), ("keyboard", _KeyboardInput)]


class _Input(ct.Structure):
    _anonymous_ = ("data",)
    _fields_ = [("type", ct.c_uint32), ("data", _InputData)]


def _bind(library, name, result, *arguments):
    function = getattr(library, name)
    function.argtypes = arguments
    function.restype = result
    return function


_ENUM_WINDOWS = ct.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM) if hasattr(ct, "WINFUNCTYPE") else None
_MOUSE_BUTTONS = {"left": (0x0002, 0x0004), "right": (0x0008, 0x0010), "middle": (0x0020, 0x0040)}
_VIRTUAL_DESKTOP_MOVE = 0x8000 | 0x4000 | 0x0001
_KEY_UP = 0x0002
_KEY_UNICODE = 0x0004
_KEY_SCANCODE = 0x0008
_KEY_EXTENDED = 0x0001
_VIRTUAL_KEYS = {
    "backspace": 0x08, "\b": 0x08, "tab": 0x09, "\t": 0x09, "clear": 0x0C,
    "enter": 0x0D, "return": 0x0D, "\n": 0x0D,
    "shift": 0xA0, "shiftleft": 0xA0, "shiftright": 0xA1,
    "ctrl": 0xA2, "ctrlleft": 0xA2, "ctrlright": 0xA3,
    "alt": 0xA4, "altleft": 0xA4, "altright": 0xA5,
    "pause": 0x13, "capslock": 0x14, "esc": 0x1B, "escape": 0x1B,
    "kana": 0x15, "hanguel": 0x15, "hangul": 0x15, "junja": 0x17, "final": 0x18,
    "hanja": 0x19, "kanji": 0x19, "convert": 0x1C, "nonconvert": 0x1D,
    "accept": 0x1E, "modechange": 0x1F, "space": 0x20, " ": 0x20, "pageup": 0x21, "pgup": 0x21, "pagedown": 0x22, "pgdn": 0x22,
    "end": 0x23, "home": 0x24, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "select": 0x29, "print": 0x2A, "execute": 0x2B,
    "printscreen": 0x2C, "prtsc": 0x2C, "prtscr": 0x2C, "prntscrn": 0x2C,
    "insert": 0x2D, "delete": 0x2E, "del": 0x2E, "help": 0x2F,
    "win": 0x5B, "super": 0x5B, "winleft": 0x5B, "winright": 0x5C, "apps": 0x5D, "sleep": 0x5F,
    "multiply": 0x6A, "add": 0x6B, "separator": 0x6C, "subtract": 0x6D, "decimal": 0x6E, "divide": 0x6F,
    "numlock": 0x90, "scrolllock": 0x91,
    "browserback": 0xA6, "browserforward": 0xA7, "browserrefresh": 0xA8, "browserstop": 0xA9,
    "browsersearch": 0xAA, "browserfavorites": 0xAB, "browserhome": 0xAC, "volumemute": 0xAD, "volumedown": 0xAE, "volumeup": 0xAF,
    "nexttrack": 0xB0, "prevtrack": 0xB1, "stop": 0xB2, "playpause": 0xB3,
    "launchmail": 0xB4, "launchmediaselect": 0xB5, "launchapp1": 0xB6, "launchapp2": 0xB7,
    **{f"f{n}": 0x6F + n for n in range(1, 25)},
    **{f"num{n}": 0x60 + n for n in range(10)},
}


class WindowsDesktop:
    def __init__(self):
        user = ct.WinDLL("user32", use_last_error=True)
        kernel = ct.WinDLL("kernel32", use_last_error=True)
        dwm = ct.WinDLL("dwmapi", use_last_error=True)
        self._metric = _bind(user, "GetSystemMetrics", ct.c_int, ct.c_int)
        self._cursor = _bind(user, "GetCursorPos", wt.BOOL, ct.POINTER(wt.POINT))
        self._set_cursor = _bind(user, "SetCursorPos", wt.BOOL, ct.c_int, ct.c_int)
        self._send_input = _bind(user, "SendInput", wt.UINT, wt.UINT, ct.POINTER(_Input), ct.c_int)
        self._layout = _bind(user, "GetKeyboardLayout", wt.HKL, wt.DWORD)
        self._scan = _bind(user, "MapVirtualKeyExW", wt.UINT, wt.UINT, wt.UINT, wt.HKL)
        self._vk_for_char = _bind(user, "VkKeyScanExW", wt.SHORT, wt.WCHAR, wt.HKL)
        self._at_point = _bind(user, "WindowFromPoint", wt.HWND, wt.POINT)
        self._root = _bind(user, "GetAncestor", wt.HWND, wt.HWND, wt.UINT)
        self._foreground = _bind(user, "GetForegroundWindow", wt.HWND)
        self._gui_info = _bind(user, "GetGUIThreadInfo", wt.BOOL, wt.DWORD, ct.POINTER(_GUIThreadInfo))
        self._enumerate = _bind(user, "EnumWindows", wt.BOOL, _ENUM_WINDOWS, wt.LPARAM)
        self._visible = _bind(user, "IsWindowVisible", wt.BOOL, wt.HWND)
        self._minimized = _bind(user, "IsIconic", wt.BOOL, wt.HWND)
        self._title = _bind(user, "GetWindowTextW", ct.c_int, wt.HWND, wt.LPWSTR, ct.c_int)
        self._thread = _bind(user, "GetWindowThreadProcessId", wt.DWORD, wt.HWND, ct.POINTER(wt.DWORD))
        self._attribute = _bind(dwm, "DwmGetWindowAttribute", wt.LONG, wt.HWND, wt.DWORD, ct.c_void_p, wt.DWORD)
        self._open_process = _bind(kernel, "OpenProcess", wt.HANDLE, wt.DWORD, wt.BOOL, wt.DWORD)
        self._process_name = _bind(
            kernel, "QueryFullProcessImageNameW", wt.BOOL, wt.HANDLE, wt.DWORD,
            wt.LPWSTR, ct.POINTER(wt.DWORD),
        )
        self._close_handle = _bind(kernel, "CloseHandle", wt.BOOL, wt.HANDLE)
        self._pending_releases: list[_Input] = []

    def size(self) -> tuple[int, int]:
        return self._metric(0), self._metric(1)

    def position(self) -> tuple[int, int]:
        point = wt.POINT()
        if not self._cursor(ct.byref(point)):
            raise ct.WinError(ct.get_last_error())
        return point.x, point.y

    @property
    def failsafe_points(self) -> tuple[tuple[int, int], ...]:
        width, height = self.size()
        return (0, 0), (0, height - 1), (width - 1, 0), (width - 1, height - 1)

    def check_failsafe(self) -> None:
        if self.position() in self.failsafe_points:
            raise RuntimeError("Desktop fail-safe triggered at a screen corner")

    def move_direct(self, x: int, y: int, held: str | None = None) -> None:
        if not self._set_cursor(x, y):
            raise ct.WinError(ct.get_last_error())

    def move(self, x: int, y: int, duration: float, held: str | None = None) -> None:
        self.check_failsafe()
        if duration:
            start_x, start_y = self.position()
            start = monotonic()
            steps = max(1, int(duration * 60))
            for step in range(1, steps):
                sleep(max(0.0, start + duration * step / steps - monotonic()))
                self.check_failsafe()
                self.move_direct(
                    round(start_x + (x - start_x) * step / steps),
                    round(start_y + (y - start_y) * step / steps),
                )
            sleep(max(0.0, start + duration - monotonic()))
            self.check_failsafe()
        self.move_direct(x, y)

    def virtual_bounds(self) -> tuple[int, int, int, int]:
        return self._metric(76), self._metric(77), self._metric(78), self._metric(79)

    def _mouse_at(self, x: int, y: int, flags: int, data: int = 0) -> _Input:
        left, top, width, height = self.virtual_bounds()
        if not (0 <= x - left < width and 0 <= y - top < height):
            raise RuntimeError("Pointer target is outside the current Windows virtual desktop.")
        # Choose the center of the normalized interval that maps to the pixel.
        nx = ((x - left) * 65536 + 32768) // width
        ny = ((y - top) * 65536 + 32768) // height
        return _mouse(flags | _VIRTUAL_DESKTOP_MOVE, nx, ny, data)

    def _inject(self, events: list[_Input]) -> int:
        ct.set_last_error(0)
        return self._send_input(len(events), (_Input * len(events))(*events), ct.sizeof(_Input))

    def _send(self, events: list[_Input]) -> None:
        if not events:
            return
        count = self._inject(events)
        if count != len(events):
            error = ct.get_last_error()
            if count:
                self._release_partial_input(events[:count])
            raise RuntimeError(
                f"Windows inserted {count} of {len(events)} input events (system error {error})."
            )

    def _release_partial_input(self, inserted: list[_Input]) -> None:
        pressed: dict[tuple[int, int, int], _Input] = {}
        for event in inserted:
            if event.type == 1:
                key = event.keyboard
                flags = key.dwFlags & ~_KEY_UP
                identity = (key.wVk, key.wScan, flags)
                if key.dwFlags & _KEY_UP:
                    pressed.pop(identity, None)
                else:
                    pressed[identity] = _keyboard(key.wVk, key.wScan, flags | _KEY_UP)
            else:
                for down, up in _MOUSE_BUTTONS.values():
                    identity = (0, down, 0)
                    if event.mouse.dwFlags & up:
                        pressed.pop(identity, None)
                    elif event.mouse.dwFlags & down:
                        pressed[identity] = _mouse(up)
        self._pending_releases.extend(reversed(pressed.values()))
        self.release_pending_input()

    def release_pending_input(self) -> None:
        if self._pending_releases:
            count = self._inject(self._pending_releases)
            del self._pending_releases[:count]
            if self._pending_releases:
                raise RuntimeError("Windows rejected release of partially inserted input.")

    def click(self, x: int, y: int, button: str, clicks: int, duration: float) -> None:
        self.check_failsafe()
        if duration:
            self.move(x, y, duration)
        down, up = _MOUSE_BUTTONS[button]
        self._send([self._mouse_at(x, y, down), _mouse(up)] * clicks)

    def button_down(self, x: int, y: int, button: str, duration: float) -> None:
        self.check_failsafe()
        if duration:
            self.move(x, y, duration)
        self._send([self._mouse_at(x, y, _MOUSE_BUTTONS[button][0])])

    def button_up(self, button: str) -> None:
        self._send([_mouse(_MOUSE_BUTTONS[button][1])])

    def scroll(self, x: int, y: int, amount: int, duration: float) -> None:
        self.check_failsafe()
        if duration:
            self.move(x, y, duration)
        self._send([self._mouse_at(x, y, 0x0800, amount * 120)])

    def keys(self, keys: tuple[str, ...]) -> None:
        self.check_failsafe()
        thread = self._thread(self._foreground(), None)
        layout = self._layout(thread)
        virtual_keys: dict[int, None] = {}
        for key in keys:
            if key in _VIRTUAL_KEYS:
                virtual_keys[_VIRTUAL_KEYS[key]] = None
                continue
            translated = self._vk_for_char(key, layout) if len(key) == 1 and ord(key) <= 0xFFFF else -1
            if translated == -1 or translated & 0xF800:
                raise ValueError(f"unsupported computer key: {key}")
            for flag, modifier in ((0x0100, 0xA0), (0x0200, 0xA2), (0x0400, 0xA4)):
                if translated & flag:
                    virtual_keys[modifier] = None
            virtual_keys[translated & 0xFF] = None
        mapped = []
        for vk in virtual_keys:
            scan = self._scan(vk, 4, layout)  # MAPVK_VK_TO_VSC_EX
            prefix = scan >> 8
            if not scan or prefix == 0xE1:
                mapped.append((vk, 0, 0))
            else:
                flags = _KEY_SCANCODE | (_KEY_EXTENDED if prefix == 0xE0 else 0)
                mapped.append((0, scan & 0xFF, flags))
        self._send(
            [_keyboard(vk, scan, flags) for vk, scan, flags in mapped]
            + [_keyboard(vk, scan, flags | _KEY_UP) for vk, scan, flags in reversed(mapped)]
        )

    def type_text(self, text: str) -> None:
        self.check_failsafe()
        units = text.encode("utf-16-le")
        self._send([
            _keyboard(0, int.from_bytes(units[i:i + 2], "little"), _KEY_UNICODE | flags)
            for i in range(0, len(units), 2)
            for flags in (0, _KEY_UP)
        ])

    def window_at(self, x: int, y: int) -> DesktopWindow | None:
        hwnd = self._at_point(wt.POINT(x, y))
        return self._describe(self._root(hwnd, 2) if hwnd else None)  # GA_ROOT

    def pointer_capture(self) -> DesktopWindow | None:
        info = _GUIThreadInfo(cbSize=ct.sizeof(_GUIThreadInfo))
        if self._gui_info(0, ct.byref(info)) and info.hwndCapture:
            return self._describe(self._root(info.hwndCapture, 2))
        return None

    def foreground(self) -> DesktopWindow | None:
        return self._describe(self._foreground())

    def resolve_window(self, selector: str) -> DesktopWindow:
        if selector.casefold().startswith("hwnd:"):
            value = selector[5:]
            if not value.isascii() or not value.isdecimal() or len(value) > 20:
                raise ValueError("window handle must be hwnd:<decimal>")
            hwnd = int(value)
            if not 0 < hwnd < 1 << (ct.sizeof(wt.HWND) * 8):
                raise ValueError("window handle is out of range")
            window = self._describe(hwnd) if self._root(hwnd, 2) == hwnd else None
            if window is None or not self._is_visible(hwnd):
                raise ValueError("target must be a visible, non-minimized top-level window")
            return window

        matches: list[DesktopWindow] = []
        query = selector.casefold()

        @_ENUM_WINDOWS
        def visit(hwnd, _):
            if self._is_visible(hwnd):
                window = self._describe(hwnd)
                if window and window.title and (
                    query in window.title.casefold() or query in (window.process or "").casefold()
                ):
                    matches.append(window)
            return True

        if not self._enumerate(visit, 0):
            raise ct.WinError(ct.get_last_error())
        if not matches:
            raise ValueError(f"No visible window matches {selector!r}.")
        if len(matches) > 1:
            candidates = ", ".join(f"hwnd:{w.hwnd} {w.title} ({w.process or 'unknown'})" for w in matches[:5])
            raise ValueError(f"Window selector matches {len(matches)} windows: {candidates}")
        return matches[0]

    def window_rect(self, window: DesktopWindow) -> tuple[int, int, int, int]:
        pid = wt.DWORD()
        hwnd = window.hwnd
        if not self._thread(hwnd, ct.byref(pid)) or pid.value != window.pid or not self._is_visible(hwnd):
            raise ValueError("Target window is no longer available. Observe it again.")
        rect = wt.RECT()
        if self._attribute(hwnd, 9, ct.byref(rect), ct.sizeof(rect)) != 0:
            raise RuntimeError("Could not read the visible window bounds.")
        return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top

    def _is_visible(self, hwnd: int) -> bool:
        if not self._visible(hwnd) or self._minimized(hwnd):
            return False
        cloaked = wt.DWORD()
        self._attribute(hwnd, 14, ct.byref(cloaked), ct.sizeof(cloaked))
        return not cloaked.value

    def _describe(self, hwnd: int | None) -> DesktopWindow | None:
        if not hwnd:
            return None
        pid = wt.DWORD()
        if not self._thread(hwnd, ct.byref(pid)):
            return None
        title = ct.create_unicode_buffer(512)
        self._title(hwnd, title, len(title))
        name = None
        process = self._open_process(0x1000, False, pid.value)  # QUERY_LIMITED_INFORMATION
        if process:
            try:
                path = ct.create_unicode_buffer(32768)
                size = wt.DWORD(len(path))
                if self._process_name(process, 0, path, ct.byref(size)):
                    name = PureWindowsPath(path.value).name
            finally:
                self._close_handle(process)
        return DesktopWindow(hwnd, pid.value, title.value, name)


def _mouse(flags: int, x: int = 0, y: int = 0, data: int = 0) -> _Input:
    event = _Input(type=0)
    event.mouse = _MouseInput(x, y, data, flags, 0, 0)
    return event


def _keyboard(vk: int, scan: int, flags: int) -> _Input:
    event = _Input(type=1)
    event.keyboard = _KeyboardInput(vk, scan, flags, 0, 0)
    return event
