import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agent_runtime.computer import (
    _normalize_hotkey_keys,
    _paste_text,
    desktop_computer_control,
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

    def test_desktop_control_passes_normalized_keys_to_pyautogui(self) -> None:
        pyautogui = Mock(KEYBOARD_KEYS={"ctrl", "v"})
        screenshot = Mock()
        screenshot.return_value = "unused"
        with patch.dict("sys.modules", {"pyautogui": pyautogui}):
            with patch("agent_runtime.computer.desktop_screenshot_capture", return_value=screenshot):
                control = desktop_computer_control(Mock())
                control({"action": "hotkey", "keys": ["Control", "V"]})

        pyautogui.hotkey.assert_called_once_with("ctrl", "v")

    def test_desktop_control_normalizes_single_key(self) -> None:
        pyautogui = Mock(KEYBOARD_KEYS={"enter"})
        screenshot = Mock(return_value="unused")
        with patch.dict("sys.modules", {"pyautogui": pyautogui}):
            with patch("agent_runtime.computer.desktop_screenshot_capture", return_value=screenshot):
                control = desktop_computer_control(Mock())
                control({"action": "key", "key": "Enter"})

        pyautogui.press.assert_called_once_with("enter")

    def test_desktop_control_rejects_unknown_single_key(self) -> None:
        pyautogui = Mock(KEYBOARD_KEYS={"enter"})
        screenshot = Mock(return_value="unused")
        with patch.dict("sys.modules", {"pyautogui": pyautogui}):
            with patch("agent_runtime.computer.desktop_screenshot_capture", return_value=screenshot):
                control = desktop_computer_control(Mock())
                with self.assertRaisesRegex(ValueError, "unsupported computer key: F13"):
                    control({"action": "key", "key": "F13"})

        pyautogui.press.assert_not_called()


if __name__ == "__main__":
    unittest.main()
