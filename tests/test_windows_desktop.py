import sys
import unittest
from unittest.mock import Mock, patch

from agent_core.tools.context import DesktopWindow
from agent_runtime.desktop.windows import WindowsDesktop


class WindowsDesktopInputTest(unittest.TestCase):
    def setUp(self) -> None:
        self.desktop = object.__new__(WindowsDesktop)
        self.desktop.check_failsafe = Mock()
        self.desktop._metric = {76: -1920, 77: -200, 78: 4480, 79: 1800}.__getitem__
        self.desktop._pending_releases = []
        self.desktop._inject = Mock(side_effect=len)

    def test_click_maps_negative_virtual_desktop_coordinates(self) -> None:
        self.desktop.click(-1920, -200, "right", 1, 0.0)
        events = self.desktop._inject.call_args.args[0]
        down, up = (event.mouse for event in events)
        self.assertEqual(down.dwFlags, 0xC009)
        self.assertEqual(up.dwFlags, 0x0010)
        self.assertEqual(down.dx * 4480 // 65536 - 1920, -1920)
        self.assertEqual(down.dy * 1800 // 65536 - 200, -200)

    def test_scroll_preserves_signed_wheel_detents_and_virtual_origin(self) -> None:
        self.desktop.scroll(2559, 1599, -3, 0.0)
        event = self.desktop._inject.call_args.args[0][0].mouse
        self.assertEqual(event.dwFlags, 0xC801)
        self.assertEqual(event.mouseData, (-360) & 0xFFFFFFFF)
        self.assertEqual(event.dx * 4480 // 65536 - 1920, 2559)
        self.assertEqual(event.dy * 1800 // 65536 - 200, 1599)

    def test_hotkey_uses_extended_scan_codes_and_releases_in_reverse_order(self) -> None:
        self.desktop._foreground = Mock(return_value=100)
        self.desktop._thread = Mock(return_value=200)
        self.desktop._layout = Mock(return_value=300)
        self.desktop._scan = Mock(side_effect=lambda vk, _, layout: {0xA3: 0xE01D, 0x27: 0xE04D}[vk])
        self.desktop.keys(("ctrlright", "right"))
        events = self.desktop._inject.call_args.args[0]
        self.assertEqual(
            [(key.wVk, key.wScan, key.dwFlags) for key in (event.keyboard for event in events)],
            [(0, 0x1D, 9), (0, 0x4D, 9), (0, 0x4D, 11), (0, 0x1D, 11)],
        )

    def test_unicode_text_sends_utf16_units_with_matching_releases(self) -> None:
        self.desktop.type_text("\u4e2d\U0001f680")
        events = self.desktop._inject.call_args.args[0]
        self.assertEqual(
            [(key.wScan, key.dwFlags) for key in (event.keyboard for event in events)],
            [(0x4E2D, 4), (0x4E2D, 6), (0xD83D, 4), (0xD83D, 6), (0xDE80, 4), (0xDE80, 6)],
        )

    def test_partial_click_tracks_release_until_windows_accepts_it(self) -> None:
        self.desktop._inject.side_effect = [1, 0, 1]
        with patch("agent_runtime.desktop.windows.ct.get_last_error", return_value=0, create=True):
            with self.assertRaisesRegex(RuntimeError, "rejected release"):
                self.desktop.click(0, 0, "left", 1, 0.0)
        self.assertEqual([event.mouse.dwFlags for event in self.desktop._pending_releases], [0x0004])
        self.desktop.release_pending_input()
        self.assertEqual(self.desktop._pending_releases, [])


@unittest.skipUnless(sys.platform == "win32", "Windows HWND enumeration")
class WindowsDesktopWindowTest(unittest.TestCase):
    def test_ambiguous_titles_list_candidates_without_guessing(self) -> None:
        desktop = object.__new__(WindowsDesktop)
        desktop._is_visible = lambda hwnd: hwnd != 3
        desktop._describe = lambda hwnd: DesktopWindow(hwnd, 5, f"Editor {hwnd}", "editor.exe")

        def enumerate_windows(callback, _):
            for hwnd in (1, 2, 3):
                callback(hwnd, 0)
            return True

        desktop._enumerate = enumerate_windows
        with self.assertRaisesRegex(ValueError, r"2 windows: hwnd:1 .*hwnd:2 "):
            desktop.resolve_window("editor.exe")
        self.assertEqual(desktop.resolve_window("Editor 2").hwnd, 2)
