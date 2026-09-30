import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from PIL import Image

from agent_core import Workspace
from agent_runtime.computer import (
    DesktopComputer,
    _normalize_hotkey_keys,
    _paste_text,
)


class PasteTextTest(unittest.TestCase):
    @patch("agent_runtime.computer.time.sleep")
    @patch("pyperclip.copy")
    @patch("pyperclip.paste", return_value="existing clipboard")
    def test_uses_ctrl_v_and_restores_clipboard_on_windows_and_linux(
        self, paste, copy, sleep
    ) -> None:
        for platform in ("win32", "linux"):
            with self.subTest(platform=platform):
                pyautogui = Mock()
                with patch.object(sys, "platform", platform):
                    _paste_text(pyautogui, "F:/Nosis/中文")

                pyautogui.hotkey.assert_called_once_with("ctrl", "v")
                copy.assert_any_call("F:/Nosis/中文")
                copy.assert_any_call("existing clipboard")
                sleep.assert_called_once_with(0.05)
                pyautogui.reset_mock()
                copy.reset_mock()
                sleep.reset_mock()

    @patch("agent_runtime.computer.time.sleep")
    @patch("pyperclip.copy")
    @patch("pyperclip.paste", return_value="existing clipboard")
    def test_uses_command_v_on_macos(self, paste, copy, sleep) -> None:
        pyautogui = Mock()
        with patch.object(sys, "platform", "darwin"):
            _paste_text(pyautogui, "こんにちは")

        pyautogui.hotkey.assert_called_once_with("command", "v")
        copy.assert_any_call("こんにちは")
        copy.assert_any_call("existing clipboard")

    @patch("pyperclip.copy")
    @patch("pyperclip.paste", return_value="existing clipboard")
    def test_restores_clipboard_when_paste_fails(self, paste, copy) -> None:
        pyautogui = Mock()
        pyautogui.hotkey.side_effect = RuntimeError("paste failed")

        with self.assertRaisesRegex(RuntimeError, "paste failed"):
            _paste_text(pyautogui, "text")

        self.assertEqual(copy.call_args_list[0].args, ("text",))
        self.assertEqual(copy.call_args_list[1].args, ("existing clipboard",))


class HotkeyNormalizationTest(unittest.TestCase):
    def test_normalizes_control_alias_and_case(self) -> None:
        pyautogui = Mock(KEYBOARD_KEYS={"ctrl", "shift", "c"})

        self.assertEqual(
            _normalize_hotkey_keys(["Control", "SHIFT", "C"], pyautogui),
            ("ctrl", "shift", "c"),
        )

    def test_rejects_unknown_key_before_pyautogui_can_ignore_it(self) -> None:
        pyautogui = Mock(KEYBOARD_KEYS={"ctrl", "v"})

        with self.assertRaisesRegex(ValueError, "unsupported computer key: ControlLeft"):
            _normalize_hotkey_keys(["ControlLeft", "v"], pyautogui)

    def test_rejects_key_without_a_current_platform_mapping(self) -> None:
        pyautogui = SimpleNamespace(
            KEYBOARD_KEYS=["ctrl", "command", "v"],
            platformModule=SimpleNamespace(
                keyboardMapping={"ctrl": 17, "command": None, "v": 86}
            ),
        )

        with self.assertRaisesRegex(ValueError, "unsupported computer key: Command"):
            _normalize_hotkey_keys(["Command", "v"], pyautogui)


class _FakeMss:
    def __init__(self, factory: "_FakeMssFactory") -> None:
        self._factory = factory
        self.monitors = [dict(monitor) for monitor in factory.monitors]

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def grab(self, monitor):
        scale = self._factory.scale
        width = monitor["width"] * scale
        height = monitor["height"] * scale
        color = (
            (monitor["left"] + 256) % 256,
            (monitor["top"] + 256) % 256,
            100,
        )
        image = Image.new("RGB", (width, height), color)
        return SimpleNamespace(size=image.size, rgb=image.tobytes())


class _FakeMssFactory:
    def __init__(self, monitors, *, scale=1) -> None:
        self.monitors = monitors
        self.scale = scale

    def __call__(self):
        return _FakeMss(self)


class DesktopComputerTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.workspace = Workspace(Path(directory.name))
        self.monitors = [
            {"left": -100, "top": -50, "width": 300, "height": 150},
            {"left": -100, "top": -50, "width": 100, "height": 100},
            {
                "left": 0,
                "top": 0,
                "width": 200,
                "height": 100,
                "is_primary": True,
            },
        ]
        self.screenshots = _FakeMssFactory(self.monitors, scale=2)
        self.pyautogui = Mock(KEYBOARD_KEYS={"enter", "ctrl", "v"})
        self.pyautogui.size.return_value = (200, 100)
        self.pyautogui.position.return_value = (50, 0)
        platform = patch("agent_runtime.computer.sys.platform", "win32")
        platform.start()
        self.addCleanup(platform.stop)

    def computer(self) -> DesktopComputer:
        return DesktopComputer(
            self.workspace,
            _screenshot_factory=self.screenshots,
            _pyautogui=self.pyautogui,
        )

    def test_normalizes_each_display_into_virtual_desktop_coordinates(self) -> None:
        computer = self.computer()

        path = computer.capture("desktop")

        with Image.open(path) as image:
            self.assertEqual(image.size, (300, 150))
            self.assertEqual(image.getpixel((10, 10)), (156, 206, 100))
            self.assertEqual(image.getpixel((150, 50)), (0, 0, 100))

    def test_maps_non_negative_screenshot_coordinates_to_negative_os_coordinates(self) -> None:
        computer = self.computer()

        computer.control({"action": "click", "x": 10, "y": 10})

        self.pyautogui.moveTo.assert_called_with(-90, -40, duration=0.0)
        self.pyautogui.click.assert_called_once_with(button="left", clicks=1)

    def test_rejects_union_area_that_is_not_on_a_display(self) -> None:
        computer = self.computer()

        with self.assertRaisesRegex(ValueError, "outside every display"):
            computer.control({"action": "click", "x": 10, "y": 120})

        self.pyautogui.click.assert_not_called()

    def test_drag_scroll_wait_and_held_mouse_actions_are_complete(self) -> None:
        computer = self.computer()
        with patch("agent_runtime.computer.time.sleep") as sleep:
            computer.control(
                {
                    "action": "drag",
                    "x": 10,
                    "y": 10,
                    "to_x": 150,
                    "to_y": 50,
                    "button": "right",
                    "duration": 0.75,
                }
            )
            computer.control({"action": "scroll", "x": 150, "y": 50, "amount": -3})
            computer.control({"action": "wait", "seconds": 1.25})
            computer.control(
                {"action": "mouse_down", "x": 150, "y": 50, "button": "left"}
            )
            computer.close()

        self.pyautogui.dragTo.assert_called_once_with(
            50, 0, duration=0.75, button="right"
        )
        self.pyautogui.scroll.assert_called_once_with(-3)
        sleep.assert_called_once_with(1.25)
        self.pyautogui.mouseDown.assert_called_once_with(button="left")
        self.pyautogui.platformModule._mouseUp.assert_called_once_with(50, 0, "left")
        self.assertIn(call(50, 0, duration=0.0), self.pyautogui.moveTo.call_args_list)

    def test_move_and_mouse_up_emit_drag_motion_while_button_is_held(self) -> None:
        computer = self.computer()

        computer.control({"action": "mouse_down", "x": 150, "y": 50})
        computer.control(
            {"action": "move", "x": 160, "y": 60, "duration": 0.25}
        )
        computer.control({"action": "mouse_up", "x": 170, "y": 70})

        self.assertEqual(
            self.pyautogui.dragTo.call_args_list,
            [
                call(
                    60,
                    10,
                    duration=0.25,
                    button="left",
                    mouseDownUp=False,
                ),
                call(
                    70,
                    20,
                    duration=0.0,
                    button="left",
                    mouseDownUp=False,
                ),
            ],
        )
        self.pyautogui.mouseUp.assert_called_once_with(button="left")

    def test_rejects_layout_changes_before_sending_input(self) -> None:
        computer = self.computer()
        self.screenshots.monitors = [
            {"left": 0, "top": 0, "width": 200, "height": 100},
            {
                "left": 0,
                "top": 0,
                "width": 200,
                "height": 100,
                "is_primary": True,
            },
        ]

        with self.assertRaisesRegex(RuntimeError, "layout or resolution changed"):
            computer.control({"action": "click", "x": 10, "y": 10})

        self.pyautogui.moveTo.assert_not_called()

    def test_rejects_screenshot_scale_changes(self) -> None:
        computer = self.computer()
        self.screenshots.scale = 1

        with self.assertRaisesRegex(RuntimeError, "screenshot scale changed"):
            computer.capture("changed")

    def test_startup_rejects_input_and_screenshot_dimension_mismatch(self) -> None:
        self.pyautogui.size.return_value = (100, 50)

        with self.assertRaisesRegex(RuntimeError, "coordinate systems disagree"):
            self.computer()

    def test_key_actions_validate_and_normalize_platform_keys(self) -> None:
        computer = self.computer()

        computer.control({"action": "key", "key": "Enter"})
        computer.control({"action": "hotkey", "keys": ["Control", "V"]})

        self.pyautogui.press.assert_called_once_with("enter")
        self.pyautogui.hotkey.assert_called_once_with("ctrl", "v")


if __name__ == "__main__":
    unittest.main()
